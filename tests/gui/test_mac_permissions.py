"""Unit tests for sayzo_agent.gui.setup.mac_permissions.

Runs on any platform — the OS-specific APIs (sounddevice, audio-tap,
desktop-notifier) are all mocked. The module under test also short-circuits
when ``sys.platform != 'darwin'``, so we patch that at module level for the
macOS-path tests.
"""
from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from sayzo_agent.gui.setup import mac_permissions


@pytest.fixture(autouse=True)
def _reset_notifier_singleton():
    """Each test gets a fresh DesktopNotifierSync singleton so mocked
    backends don't leak between cases."""
    mac_permissions._NOTIFIER = None
    mac_permissions._NOTIFIER_INIT_FAILED = False
    yield
    mac_permissions._NOTIFIER = None
    mac_permissions._NOTIFIER_INIT_FAILED = False


# ---------------------------------------------------------------------------
# prompt_microphone
# ---------------------------------------------------------------------------


class _FakePortAudioError(Exception):
    pass


def _fake_sounddevice(open_raises: Exception | None = None) -> SimpleNamespace:
    """Build a minimal sounddevice-shaped namespace the helper can import."""

    class _Stream:
        def __init__(self, *_a, **_kw):
            if open_raises is not None:
                raise open_raises

        def start(self):
            pass

        def stop(self):
            pass

        def close(self):
            pass

    return SimpleNamespace(InputStream=_Stream, PortAudioError=_FakePortAudioError)


def test_prompt_microphone_returns_true_when_stream_opens():
    fake_sd = _fake_sounddevice()
    with patch("sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"), patch.dict(
        "sys.modules", {"sounddevice": fake_sd}
    ), patch("sayzo_agent.gui.setup.mac_permissions.time.sleep"):
        assert mac_permissions.prompt_microphone() is True


def test_prompt_microphone_returns_false_on_portaudio_error():
    fake_sd = _fake_sounddevice(open_raises=_FakePortAudioError("denied"))
    with patch("sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"), patch.dict(
        "sys.modules", {"sounddevice": fake_sd}
    ), patch("sayzo_agent.gui.setup.mac_permissions.time.sleep"):
        assert mac_permissions.prompt_microphone() is False


def test_prompt_microphone_returns_none_on_unexpected_error():
    fake_sd = _fake_sounddevice(open_raises=RuntimeError("something else"))
    with patch("sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"), patch.dict(
        "sys.modules", {"sounddevice": fake_sd}
    ), patch("sayzo_agent.gui.setup.mac_permissions.time.sleep"):
        assert mac_permissions.prompt_microphone() is None


def test_prompt_microphone_returns_none_on_non_darwin():
    with patch("sayzo_agent.gui.setup.mac_permissions.sys.platform", "win32"):
        assert mac_permissions.prompt_microphone() is None


# ---------------------------------------------------------------------------
# prompt_audio_capture
# ---------------------------------------------------------------------------


def test_prompt_audio_capture_returns_true_on_timeout():
    """audio-tap still running at timeout means it cleared the permission
    gate — treat as granted."""
    def raise_timeout(*_a, **kw):
        raise subprocess.TimeoutExpired(cmd=_a[0], timeout=kw.get("timeout", 1.5))

    with patch("sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"), patch(
        "sayzo_agent.capture.system_mac._find_audio_tap", return_value="/fake/audio-tap"
    ), patch(
        "sayzo_agent.gui.setup.mac_permissions.subprocess.run", side_effect=raise_timeout
    ):
        assert mac_permissions.prompt_audio_capture() is True


def test_prompt_audio_capture_returns_false_on_exit_77():
    result = subprocess.CompletedProcess(args=[], returncode=77, stdout=b"", stderr=b"")
    with patch("sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"), patch(
        "sayzo_agent.capture.system_mac._find_audio_tap", return_value="/fake/audio-tap"
    ), patch("sayzo_agent.gui.setup.mac_permissions.subprocess.run", return_value=result):
        assert mac_permissions.prompt_audio_capture() is False


def test_prompt_audio_capture_returns_true_on_exit_0():
    result = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")
    with patch("sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"), patch(
        "sayzo_agent.capture.system_mac._find_audio_tap", return_value="/fake/audio-tap"
    ), patch("sayzo_agent.gui.setup.mac_permissions.subprocess.run", return_value=result):
        assert mac_permissions.prompt_audio_capture() is True


def test_prompt_audio_capture_returns_none_on_unknown_exit():
    result = subprocess.CompletedProcess(args=[], returncode=42, stdout=b"", stderr=b"")
    with patch("sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"), patch(
        "sayzo_agent.capture.system_mac._find_audio_tap", return_value="/fake/audio-tap"
    ), patch("sayzo_agent.gui.setup.mac_permissions.subprocess.run", return_value=result):
        assert mac_permissions.prompt_audio_capture() is None


def test_prompt_audio_capture_returns_none_when_binary_missing():
    with patch("sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"), patch(
        "sayzo_agent.capture.system_mac._find_audio_tap",
        side_effect=FileNotFoundError("no binary"),
    ):
        assert mac_permissions.prompt_audio_capture() is None


def test_prompt_audio_capture_returns_none_on_non_darwin():
    with patch("sayzo_agent.gui.setup.mac_permissions.sys.platform", "win32"):
        assert mac_permissions.prompt_audio_capture() is None


# ---------------------------------------------------------------------------
# prompt_notifications
# ---------------------------------------------------------------------------


def _patch_notifier(authorise_return: bool | Exception):
    """Return a context manager that patches DesktopNotifierSync in sys.modules
    with a backend whose request_authorisation returns ``authorise_return`` (or
    raises it, if it's an Exception)."""
    fake = MagicMock()
    if isinstance(authorise_return, Exception):
        fake.request_authorisation.side_effect = authorise_return
    else:
        fake.request_authorisation.return_value = authorise_return
    module = SimpleNamespace(DesktopNotifierSync=MagicMock(return_value=fake))
    return patch.dict("sys.modules", {"desktop_notifier.sync": module}), fake


def test_prompt_notifications_returns_true_when_granted():
    sys_modules_patch, _ = _patch_notifier(True)
    with patch("sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"), sys_modules_patch:
        assert mac_permissions.prompt_notifications() is True


def test_prompt_notifications_returns_false_when_denied():
    sys_modules_patch, _ = _patch_notifier(False)
    with patch("sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"), sys_modules_patch:
        assert mac_permissions.prompt_notifications() is False


def test_prompt_notifications_returns_none_on_backend_error():
    sys_modules_patch, _ = _patch_notifier(RuntimeError("boom"))
    with patch("sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"), sys_modules_patch:
        assert mac_permissions.prompt_notifications() is None


def test_prompt_notifications_returns_none_on_non_darwin():
    with patch("sayzo_agent.gui.setup.mac_permissions.sys.platform", "win32"):
        assert mac_permissions.prompt_notifications() is None


def test_prompt_notifications_returns_none_when_init_fails():
    """If DesktopNotifierSync construction throws, the helper must swallow
    and return None (never raise back into the bridge)."""
    failing_ctor = MagicMock(side_effect=RuntimeError("no backend"))
    module = SimpleNamespace(DesktopNotifierSync=failing_ctor)
    with patch(
        "sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"
    ), patch.dict("sys.modules", {"desktop_notifier.sync": module}):
        assert mac_permissions.prompt_notifications() is None


# ---------------------------------------------------------------------------
# open_* helpers
# ---------------------------------------------------------------------------


def test_open_mic_settings_spawns_open_on_darwin():
    with patch(
        "sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"
    ), patch("sayzo_agent.gui.setup.mac_permissions.subprocess.Popen") as popen:
        mac_permissions.open_mic_settings()
    assert popen.call_count == 1
    args = popen.call_args.args[0]
    assert args[0] == "open"
    assert "Privacy_Microphone" in args[1]


def test_open_audio_capture_settings_uses_audio_capture_uri():
    with patch(
        "sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"
    ), patch("sayzo_agent.gui.setup.mac_permissions.subprocess.Popen") as popen:
        mac_permissions.open_audio_capture_settings()
    args = popen.call_args.args[0]
    assert "Privacy_AudioCapture" in args[1]


def test_open_notification_settings_uses_notifications_uri():
    with patch(
        "sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"
    ), patch("sayzo_agent.gui.setup.mac_permissions.subprocess.Popen") as popen:
        mac_permissions.open_notification_settings()
    args = popen.call_args.args[0]
    assert "Notifications-Settings" in args[1]


def test_open_helpers_are_noop_on_non_darwin():
    with patch(
        "sayzo_agent.gui.setup.mac_permissions.sys.platform", "win32"
    ), patch("sayzo_agent.gui.setup.mac_permissions.subprocess.Popen") as popen:
        mac_permissions.open_mic_settings()
        mac_permissions.open_audio_capture_settings()
        mac_permissions.open_notification_settings()
    assert popen.called is False


def test_open_swallows_oserror():
    """subprocess.Popen can raise OSError if ``open`` is unavailable — we
    must not propagate, since the helpers are invoked from the JS bridge."""
    with patch(
        "sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"
    ), patch(
        "sayzo_agent.gui.setup.mac_permissions.subprocess.Popen",
        side_effect=OSError("no open binary"),
    ):
        # Should not raise.
        mac_permissions.open_mic_settings()
