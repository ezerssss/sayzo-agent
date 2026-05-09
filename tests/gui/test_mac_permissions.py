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


def _fake_avfoundation(
    *,
    status: int,
    completion_grants: bool | None = None,
    completion_delay_secs: float = 0.0,
    raise_on_request: Exception | None = None,
) -> SimpleNamespace:
    """Build a minimal AVFoundation-shaped module the helper can import.

    Args:
        status: initial value returned by ``authorizationStatusForMediaType_``.
            0=NotDetermined, 1=Restricted, 2=Denied, 3=Authorized.
        completion_grants: True/False fires the completion with that
            decision; None simulates the dialog never appearing.
        completion_delay_secs: dialog-think-time before the completion
            fires; 0 means "instant".
        raise_on_request: if set, ``requestAccessForMediaType_completionHandler_``
            raises this instead of dispatching.
    """
    import threading as _threading
    import time as _time

    state: dict = {"status": status}
    captured: dict = {"status_queries": 0, "request_calls": 0, "last_handler": None}

    class _AVCaptureDevice:
        @staticmethod
        def authorizationStatusForMediaType_(media_type):
            captured["status_queries"] += 1
            captured["last_query"] = media_type
            return state["status"]

        @staticmethod
        def requestAccessForMediaType_completionHandler_(media_type, handler):
            captured["request_calls"] += 1
            captured["last_handler"] = handler
            if raise_on_request is not None:
                raise raise_on_request
            if completion_grants is None:
                return

            def _fire():
                if completion_delay_secs > 0:
                    _time.sleep(completion_delay_secs)
                handler(completion_grants)

            # Mirror real AVFoundation's background-queue dispatch.
            _threading.Thread(target=_fire, daemon=True).start()

    return SimpleNamespace(
        AVCaptureDevice=_AVCaptureDevice,
        AVMediaTypeAudio="soun",
        _captured=captured,
    )


def _patch_av(fake_av, *, request_timeout_secs: float = 0.5):
    """Bundle the patches a prompt_microphone test needs."""
    return [
        patch("sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"),
        patch.dict("sys.modules", {"AVFoundation": fake_av}),
        patch(
            "sayzo_agent.gui.setup.mac_permissions._tccutil_reset_service",
            return_value=True,
        ),
        patch(
            "sayzo_agent.gui.setup.mac_permissions._log_bundle_info_plist_once",
            return_value=None,
        ),
        patch(
            "sayzo_agent.gui.setup.mac_permissions._TCC_REQUEST_TIMEOUT_SECS",
            request_timeout_secs,
        ),
    ]


def test_prompt_microphone_returns_true_when_already_authorized():
    """Status == 3 (Authorized): no dialog fired, returns True directly.
    Critical: must not even call requestAccess, since that would risk
    replaying a permission flow on an already-granted bundle."""
    fake_av = _fake_avfoundation(status=3)
    patches = _patch_av(fake_av)
    _enter_all(patches)
    try:
        result = mac_permissions.prompt_microphone()
        assert result.granted is True
        assert result.stale_tcc_likely is False
    finally:
        _exit_all(patches)
    # requestAccess MUST NOT have been called on the early-return path.
    assert fake_av._captured["request_calls"] == 0


def test_prompt_microphone_returns_false_when_previously_denied():
    """Status == 2 (Denied): no dialog re-fires, returns False without
    calling requestAccess."""
    fake_av = _fake_avfoundation(status=2)
    patches = _patch_av(fake_av)
    _enter_all(patches)
    try:
        result = mac_permissions.prompt_microphone()
        assert result.granted is False
        assert result.stale_tcc_likely is False
    finally:
        _exit_all(patches)
    assert fake_av._captured["request_calls"] == 0


def test_prompt_microphone_returns_false_when_restricted():
    """Status == 1 (Restricted, e.g. MDM/parental controls): returns False."""
    fake_av = _fake_avfoundation(status=1)
    patches = _patch_av(fake_av)
    _enter_all(patches)
    try:
        result = mac_permissions.prompt_microphone()
        assert result.granted is False
        assert result.stale_tcc_likely is False
    finally:
        _exit_all(patches)


def test_prompt_microphone_returns_true_when_completion_grants():
    """Happy path: NotDetermined → requestAccess → completion fires True."""
    fake_av = _fake_avfoundation(
        status=0, completion_grants=True, completion_delay_secs=0.05
    )
    patches = _patch_av(fake_av, request_timeout_secs=2.0)
    _enter_all(patches)
    try:
        result = mac_permissions.prompt_microphone()
        assert result.granted is True
        assert result.stale_tcc_likely is False
    finally:
        _exit_all(patches)
    # requestAccess MUST have been called — that's what triggers the dialog.
    assert fake_av._captured["request_calls"] == 1


def test_prompt_microphone_returns_false_when_completion_denies():
    """User clicks Don't Allow → completion fires False, no stale-TCC flag."""
    fake_av = _fake_avfoundation(
        status=0, completion_grants=False, completion_delay_secs=0.05
    )
    patches = _patch_av(fake_av, request_timeout_secs=2.0)
    _enter_all(patches)
    try:
        result = mac_permissions.prompt_microphone()
        assert result.granted is False
        assert result.stale_tcc_likely is False
    finally:
        _exit_all(patches)
    assert fake_av._captured["request_calls"] == 1


def test_prompt_microphone_flags_stale_tcc_when_completion_never_fires():
    """Completion never fires within timeout → granted=None, stale_tcc_likely=True."""
    fake_av = _fake_avfoundation(status=0, completion_grants=None)
    patches = _patch_av(fake_av, request_timeout_secs=0.3)
    _enter_all(patches)
    try:
        result = mac_permissions.prompt_microphone()
        assert result.granted is None
        assert result.stale_tcc_likely is True
    finally:
        _exit_all(patches)
    # requestAccess WAS called — important for users to SEE that we tried.
    assert fake_av._captured["request_calls"] == 1


def test_prompt_microphone_returns_none_when_request_access_raises():
    """AVFoundation raises (e.g. PyObjC bridge error) → None without stale-TCC flag."""
    fake_av = _fake_avfoundation(
        status=0, raise_on_request=RuntimeError("AV bridge failure")
    )
    patches = _patch_av(fake_av, request_timeout_secs=0.5)
    _enter_all(patches)
    try:
        result = mac_permissions.prompt_microphone()
        assert result.granted is None
        assert result.stale_tcc_likely is False
    finally:
        _exit_all(patches)


def test_prompt_microphone_returns_none_on_unexpected_status():
    """Defensive: an enum value Apple hasn't documented yet (e.g. 99)
    must not be silently bucketed as granted/denied."""
    fake_av = _fake_avfoundation(status=99)
    patches = _patch_av(fake_av)
    _enter_all(patches)
    try:
        assert mac_permissions.prompt_microphone().granted is None
    finally:
        _exit_all(patches)


def test_prompt_microphone_returns_none_when_avfoundation_unavailable():
    """Dev machine without pyobjc-framework-AVFoundation: log warn,
    return None — never raise back into the bridge."""
    failing_module = SimpleNamespace()
    with patch(
        "sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"
    ), patch.dict("sys.modules", {"AVFoundation": failing_module}):
        result = mac_permissions.prompt_microphone()
        assert result.granted is None
        assert result.stale_tcc_likely is False


def test_prompt_microphone_returns_none_on_non_darwin():
    with patch("sayzo_agent.gui.setup.mac_permissions.sys.platform", "win32"):
        result = mac_permissions.prompt_microphone()
        assert result.granted is None
        assert result.stale_tcc_likely is False


# ---------------------------------------------------------------------------
# prompt_audio_capture
# ---------------------------------------------------------------------------


class _FakeAudioTapProc:
    """Stand-in for subprocess.Popen([audio-tap]). Drives the helper through
    a scripted stderr stream + exit code so tests cover all decision branches
    without spawning a real binary.
    """

    def __init__(
        self,
        *,
        stderr_lines: list[str],
        exit_code: int | None,
        line_delay_secs: float = 0.0,
    ) -> None:
        self.pid = 4242
        self._stderr_lines = list(stderr_lines)
        self._exit_code = exit_code
        self._line_delay = line_delay_secs
        self.stderr = self  # iterable for `for line in proc.stderr:`
        self.stdout = None
        self._terminated = False
        self._exited_event = __import__("threading").Event()
        # Mark exited synchronously when an exit_code is provided; the helper
        # may call wait() before consuming stderr.
        if exit_code is not None and not stderr_lines:
            self._exited_event.set()

    def __iter__(self):
        return self

    def __next__(self):
        if not self._stderr_lines:
            # EOF on stderr — also flips us to "exited" if we have a code.
            if self._exit_code is not None:
                self._exited_event.set()
            raise StopIteration
        if self._line_delay:
            import time as _t
            _t.sleep(self._line_delay)
        return self._stderr_lines.pop(0)

    def poll(self):
        return self._exit_code if self._exited_event.is_set() else None

    def wait(self, timeout: float | None = None):
        if self._exited_event.wait(timeout=timeout):
            return self._exit_code if self._exit_code is not None else 0
        raise subprocess.TimeoutExpired(cmd="audio-tap", timeout=timeout)

    def terminate(self):
        self._terminated = True
        # Simulate the binary exiting promptly on SIGTERM.
        if self._exit_code is None:
            self._exit_code = 0
        self._exited_event.set()

    def kill(self):
        self.terminate()


def _patch_for_audio_tap(fake_proc: _FakeAudioTapProc | OSError | None):
    """Context-manager helper bundling all the patches the audio-tap probe
    needs. Pass a fake proc to simulate spawn success, an OSError to simulate
    spawn failure, or None to leave subprocess.Popen unpatched (used by the
    binary-missing path)."""
    patches = [
        patch("sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"),
        patch(
            "sayzo_agent.capture.system_mac._find_audio_tap",
            return_value="/fake/audio-tap",
        ),
    ]
    if isinstance(fake_proc, OSError):
        patches.append(
            patch(
                "sayzo_agent.gui.setup.mac_permissions.subprocess.Popen",
                side_effect=fake_proc,
            )
        )
    elif fake_proc is not None:
        patches.append(
            patch(
                "sayzo_agent.gui.setup.mac_permissions.subprocess.Popen",
                return_value=fake_proc,
            )
        )
    return patches


def _enter_all(patches):
    """tiny helper: enter a list of patch context managers in one block."""
    return [p.start() for p in patches]


def _exit_all(patches):
    for p in patches:
        p.stop()


def test_prompt_audio_capture_returns_true_on_success_line():
    """Granted path: stderr emits 'capturing system audio …', the helper
    sees it, kills the still-running probe, and returns True."""
    fake = _FakeAudioTapProc(
        stderr_lines=[
            "audio-tap: using global tap\n",
            "audio-tap: capturing system audio (native 48000 Hz ...)\n",
        ],
        exit_code=None,  # Real binary keeps running until we SIGTERM it.
    )
    patches = _patch_for_audio_tap(fake)
    _enter_all(patches)
    try:
        result = mac_permissions.prompt_audio_capture()
        assert result.granted is True
        assert result.stale_tcc_likely is False
        assert fake._terminated  # Helper SIGTERM'd the still-running binary.
    finally:
        _exit_all(patches)


def test_prompt_audio_capture_returns_false_on_exit_77():
    """Denied path with a real human click delay: stderr emits the
    'AudioHardwareCreateProcessTap failed' line and the binary exits with
    77 after the user clicks Don't Allow. The helper returns False with
    stale_tcc_likely=False since the elapsed time exceeds the threshold —
    a real click can't happen sub-500 ms.
    """
    fake = _FakeAudioTapProc(
        stderr_lines=[
            "audio-tap: AudioHardwareCreateProcessTap failed (OSStatus -1719).\n",
        ],
        exit_code=77,
        # Hold the dialog open past the 500 ms stale-TCC threshold.
        line_delay_secs=0.6,
    )
    patches = _patch_for_audio_tap(fake)
    _enter_all(patches)
    try:
        result = mac_permissions.prompt_audio_capture()
        assert result.granted is False
        assert result.stale_tcc_likely is False
    finally:
        _exit_all(patches)


def test_prompt_audio_capture_flags_stale_tcc_on_fast_silent_deny():
    """Stale-TCC fingerprint: pre-v2.6.0 ad-hoc-signed audio-tap left
    a TCC entry whose code-requirement no longer matches the current
    Developer-ID-signed binary. macOS silently denies without ever
    presenting a dialog, so the binary exits 77 in milliseconds. The
    helper flags stale_tcc_likely so the GUI can show the targeted
    "remove from System Settings → Audio Capture, then retry" copy
    instead of the misleading "open Settings, turn it on" message.
    """
    fake = _FakeAudioTapProc(
        stderr_lines=[
            "audio-tap: AudioHardwareCreateProcessTap failed (OSStatus -1719).\n",
        ],
        exit_code=77,
        line_delay_secs=0.0,  # Instant deny — no dialog.
    )
    patches = _patch_for_audio_tap(fake)
    _enter_all(patches)
    try:
        result = mac_permissions.prompt_audio_capture()
        assert result.granted is False
        assert result.stale_tcc_likely is True
    finally:
        _exit_all(patches)


def test_prompt_audio_capture_returns_none_when_dialog_times_out():
    """Inconclusive: no decisive stderr, binary still alive when the
    bridge timeout fires. Helper terminates the probe and returns None."""
    fake = _FakeAudioTapProc(
        stderr_lines=[],  # Empty stderr → reader blocks until EOF/SIGTERM.
        exit_code=None,
        # Slow drip so reader blocks even if we ever add lines.
        line_delay_secs=10.0,
    )
    patches = _patch_for_audio_tap(fake)
    _enter_all(patches)
    try:
        with patch(
            "sayzo_agent.gui.setup.mac_permissions._TCC_REQUEST_TIMEOUT_SECS",
            0.05,
        ):
            result = mac_permissions.prompt_audio_capture()
            assert result.granted is None
            assert result.stale_tcc_likely is False
        assert fake._terminated
    finally:
        _exit_all(patches)


def test_prompt_audio_capture_returns_none_on_unexpected_exit():
    """Binary exits non-zero non-77 (e.g. SIGABRT from Gatekeeper on a
    managed Mac) before any decisive stderr. Treat as inconclusive."""
    fake = _FakeAudioTapProc(
        stderr_lines=[
            "audio-tap: dyld error\n",
        ],
        exit_code=-6,  # SIGABRT
    )
    patches = _patch_for_audio_tap(fake)
    _enter_all(patches)
    try:
        result = mac_permissions.prompt_audio_capture()
        assert result.granted is None
        assert result.stale_tcc_likely is False
    finally:
        _exit_all(patches)


def test_prompt_audio_capture_returns_none_when_binary_missing():
    with patch(
        "sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"
    ), patch(
        "sayzo_agent.capture.system_mac._find_audio_tap",
        side_effect=FileNotFoundError("no binary"),
    ):
        result = mac_permissions.prompt_audio_capture()
        assert result.granted is None
        assert result.stale_tcc_likely is False


def test_prompt_audio_capture_returns_none_on_spawn_failure():
    patches = _patch_for_audio_tap(OSError("permission denied"))
    _enter_all(patches)
    try:
        result = mac_permissions.prompt_audio_capture()
        assert result.granted is None
        assert result.stale_tcc_likely is False
    finally:
        _exit_all(patches)


def test_prompt_audio_capture_returns_none_on_non_darwin():
    with patch("sayzo_agent.gui.setup.mac_permissions.sys.platform", "win32"):
        result = mac_permissions.prompt_audio_capture()
        assert result.granted is None
        assert result.stale_tcc_likely is False


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
        result = mac_permissions.prompt_notifications()
        assert result.granted is True
        assert result.stale_tcc_likely is False


def test_prompt_notifications_returns_false_when_denied_slowly():
    """Real human-click denial: stale_tcc_likely stays False because the
    elapsed time exceeds the 500 ms threshold."""
    fake = MagicMock()

    def _slow_deny():
        import time as _t
        _t.sleep(0.6)
        return False

    fake.request_authorisation.side_effect = _slow_deny
    module = SimpleNamespace(DesktopNotifierSync=MagicMock(return_value=fake))
    with patch(
        "sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"
    ), patch.dict("sys.modules", {"desktop_notifier.sync": module}):
        result = mac_permissions.prompt_notifications()
        assert result.granted is False
        assert result.stale_tcc_likely is False


def test_prompt_notifications_flags_stale_tcc_on_fast_silent_deny():
    """Stale UNN entry from a previous Sayzo install with a different
    signing identity silently denies without UI. request_authorisation
    returns False instantly. The helper flags stale_tcc_likely so the
    bridge payload exposes it (the React Notifications screen still falls
    back to the existing waiting-state polling, since the System Settings
    Notifications toggle DOES re-record under the new CR — but the flag
    is plumbed through for diagnostics + future refinement)."""
    sys_modules_patch, _ = _patch_notifier(False)
    with patch("sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"), sys_modules_patch:
        result = mac_permissions.prompt_notifications()
        assert result.granted is False
        assert result.stale_tcc_likely is True


def test_prompt_notifications_returns_none_on_backend_error():
    sys_modules_patch, _ = _patch_notifier(RuntimeError("boom"))
    with patch("sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"), sys_modules_patch:
        result = mac_permissions.prompt_notifications()
        assert result.granted is None
        assert result.stale_tcc_likely is False


def test_prompt_notifications_returns_none_on_non_darwin():
    with patch("sayzo_agent.gui.setup.mac_permissions.sys.platform", "win32"):
        result = mac_permissions.prompt_notifications()
        assert result.granted is None
        assert result.stale_tcc_likely is False


def test_prompt_notifications_returns_none_when_init_fails():
    """If DesktopNotifierSync construction throws, the helper must swallow
    and return None (never raise back into the bridge)."""
    failing_ctor = MagicMock(side_effect=RuntimeError("no backend"))
    module = SimpleNamespace(DesktopNotifierSync=failing_ctor)
    with patch(
        "sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"
    ), patch.dict("sys.modules", {"desktop_notifier.sync": module}):
        result = mac_permissions.prompt_notifications()
        assert result.granted is None
        assert result.stale_tcc_likely is False


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


# ---------------------------------------------------------------------------
# _tccutil_reset_service + relaunch_app + Info.plist diagnostic
#
# These power the "Reset & Restart Sayzo" recovery flow. The flow is the
# only path that resolves a stale-TCC silent-deny without sending the user
# to Terminal: macOS hides CR-mismatched orphan entries from
# Privacy & Security, so any "remove from the list" instruction is a dead
# end. tccutil clears the entry; relaunch is required because AVFoundation
# caches the per-process NotDetermined→Denied transition (Apple-confirmed
# behavior — see _tccutil_reset_service docstring for the source).
# ---------------------------------------------------------------------------


def test_tccutil_reset_service_runs_correct_command_and_returns_true_on_rc0():
    completed = subprocess.CompletedProcess(
        args=["tccutil", "reset", "Microphone", "com.sayzo.agent"],
        returncode=0,
        stdout="",
        stderr="",
    )
    with patch(
        "sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"
    ), patch(
        "sayzo_agent.gui.setup.mac_permissions.subprocess.run",
        return_value=completed,
    ) as run:
        ok = mac_permissions._tccutil_reset_service("Microphone")
    assert ok is True
    args = run.call_args.args[0]
    assert args == ["tccutil", "reset", "Microphone", "com.sayzo.agent"]
    # Must run with capture_output so stdout/stderr can be logged on
    # failure without leaking to the user's terminal.
    assert run.call_args.kwargs["capture_output"] is True
    # text=True so stdout/stderr arrive as strings for log formatting.
    assert run.call_args.kwargs["text"] is True


def test_tccutil_reset_service_returns_false_on_nonzero_rc():
    """A non-zero exit means tccutil rejected the service name or bundle
    id. We want a False return so the recovery flow can decide whether to
    still attempt the relaunch (it does — AVFoundation re-reading from a
    fresh process is useful even when the explicit reset didn't fire)."""
    completed = subprocess.CompletedProcess(
        args=["tccutil", "reset", "Microphone", "com.sayzo.agent"],
        returncode=1,
        stdout="",
        stderr="tccutil: Failed to reset Microphone\n",
    )
    with patch(
        "sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"
    ), patch(
        "sayzo_agent.gui.setup.mac_permissions.subprocess.run",
        return_value=completed,
    ):
        assert mac_permissions._tccutil_reset_service("Microphone") is False


def test_tccutil_reset_service_returns_false_on_oserror():
    """tccutil missing from PATH (managed Mac with stripped CLI tools)
    must return False without raising — the recovery flow falls through
    to relaunch_app, which is still useful on its own."""
    with patch(
        "sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"
    ), patch(
        "sayzo_agent.gui.setup.mac_permissions.subprocess.run",
        side_effect=FileNotFoundError("no tccutil"),
    ):
        assert mac_permissions._tccutil_reset_service("Microphone") is False


def test_tccutil_reset_service_is_noop_on_non_darwin():
    with patch(
        "sayzo_agent.gui.setup.mac_permissions.sys.platform", "win32"
    ), patch(
        "sayzo_agent.gui.setup.mac_permissions.subprocess.run"
    ) as run:
        assert mac_permissions._tccutil_reset_service("Microphone") is False
    assert run.called is False


def test_tccutil_reset_service_uses_audio_capture_service_name():
    """Apple's `man tccutil` does not document AudioCapture explicitly —
    the service name comes from the canonical `insidegui/AudioCap` sample.
    Lock the wire string so a future rename in the constants block can't
    silently regress the audio-tap recovery path."""
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="", stderr=""
    )
    with patch(
        "sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"
    ), patch(
        "sayzo_agent.gui.setup.mac_permissions.subprocess.run",
        return_value=completed,
    ) as run:
        mac_permissions._tccutil_reset_service(
            mac_permissions._TCC_SERVICE_AUDIO_CAPTURE
        )
    assert run.call_args.args[0][2] == "AudioCapture"


def test_relaunch_app_spawns_open_n_against_bundle_then_exits():
    """relaunch_app must (a) Popen `open -n /path/to/Sayzo.app` so a fresh
    instance is detached from this process, and (b) os._exit(0) so the
    current process dies before kernel-locked single-instance arbitration
    runs against the new launch."""
    fake_exe = "/Applications/Sayzo.app/Contents/MacOS/sayzo-agent"

    class _FakePath:
        def __init__(self, p, suffix=None):
            self._p = p
            self.suffix = suffix or ""

        def resolve(self):
            return self

        @property
        def parents(self):
            return [
                _FakePath(
                    "/Applications/Sayzo.app/Contents/MacOS",
                ),
                _FakePath("/Applications/Sayzo.app/Contents"),
                _FakePath("/Applications/Sayzo.app", suffix=".app"),
                _FakePath("/Applications"),
            ]

        def exists(self):
            return True

        def __str__(self):
            return self._p

    with patch(
        "sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"
    ), patch(
        "sayzo_agent.gui.setup.mac_permissions.sys.executable", fake_exe
    ), patch(
        "pathlib.Path",
        lambda p: _FakePath(p) if isinstance(p, str) else p,
    ), patch(
        "sayzo_agent.gui.setup.mac_permissions.subprocess.Popen"
    ) as popen, patch("os._exit") as exit_call:
        mac_permissions.relaunch_app()

    # `open -n <bundle>` to launch a new detached instance.
    args = popen.call_args.args[0]
    assert args[0] == "open"
    assert args[1] == "-n"
    assert args[2] == "/Applications/Sayzo.app"
    assert popen.call_args.kwargs.get("start_new_session") is True
    # Must hard-exit with code 0; non-zero would taint kernel exit
    # bookkeeping and noisy crash reports for what is a normal handoff.
    exit_call.assert_called_once_with(0)


def test_relaunch_app_still_exits_when_no_app_bundle_above_executable():
    """Source-run dev builds (non-frozen) won't have an .app bundle above
    sys.executable. We must still hard-exit so the user isn't left with
    a wedged window — they re-launch manually via the dev script."""
    fake_exe = "/usr/bin/python3"

    class _FakePath:
        def __init__(self, p, suffix=""):
            self._p = p
            self.suffix = suffix

        def resolve(self):
            return self

        @property
        def parents(self):
            return [_FakePath("/usr/bin"), _FakePath("/usr")]

        def exists(self):
            return False

        def __str__(self):
            return self._p

    with patch(
        "sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"
    ), patch(
        "sayzo_agent.gui.setup.mac_permissions.sys.executable", fake_exe
    ), patch(
        "pathlib.Path",
        lambda p: _FakePath(p) if isinstance(p, str) else p,
    ), patch(
        "sayzo_agent.gui.setup.mac_permissions.subprocess.Popen"
    ) as popen, patch("os._exit") as exit_call:
        mac_permissions.relaunch_app()
    assert popen.called is False
    exit_call.assert_called_once_with(0)


def test_relaunch_app_is_noop_on_non_darwin():
    """Windows/Linux: don't try to `open -n`. The function exists for the
    cross-platform call sites in bridge.py; we want a clean no-op."""
    with patch(
        "sayzo_agent.gui.setup.mac_permissions.sys.platform", "win32"
    ), patch(
        "sayzo_agent.gui.setup.mac_permissions.subprocess.Popen"
    ) as popen, patch("os._exit") as exit_call:
        mac_permissions.relaunch_app()
    assert popen.called is False
    assert exit_call.called is False


def test_log_bundle_info_plist_logs_usage_descriptions(caplog):
    """The diagnostic must surface presence/absence of the three TCC
    usage-description keys. If any of them are MISSING after a build,
    the support thread tells us which one — without this log line a
    silent-deny bug looks identical to a stale-CR bug."""
    info = {
        "CFBundleIdentifier": "com.sayzo.agent",
        "CFBundleExecutable": "sayzo-agent",
        "LSUIElement": True,
        "NSMicrophoneUsageDescription": "Sayzo opens the microphone…",
        "NSAudioCaptureUsageDescription": "So Sayzo can hear the other…",
        # NSAppleEventsUsageDescription deliberately omitted to verify
        # the helper reports MISSING for absent keys.
    }
    fake_bundle = MagicMock()
    fake_bundle.infoDictionary.return_value = info
    fake_bundle.bundlePath.return_value = "/Applications/Sayzo.app"
    fake_nsbundle = MagicMock()
    fake_nsbundle.mainBundle.return_value = fake_bundle
    fake_foundation = SimpleNamespace(NSBundle=fake_nsbundle)

    # Reset the one-shot guard so the test sees a fresh log.
    mac_permissions._log_bundle_info_plist_once._done = False  # type: ignore[attr-defined]

    import logging as _logging
    caplog.set_level(_logging.INFO, logger="sayzo_agent.gui.setup.mac_permissions")
    with patch(
        "sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"
    ), patch.dict("sys.modules", {"Foundation": fake_foundation}):
        mac_permissions._log_bundle_info_plist_once()

    text = " ".join(r.message for r in caplog.records)
    assert "com.sayzo.agent" in text
    assert "NSMicrophoneUsageDescription=present" in text
    assert "NSAudioCaptureUsageDescription=present" in text
    # The omitted key surfaces explicitly so a future regression where a
    # build drops a key is loud, not silent.
    assert "NSAppleEventsUsageDescription=MISSING" in text


def test_log_bundle_info_plist_runs_only_once_per_process():
    """The helper is meant to be cheap to call from prompt_microphone /
    prompt_audio_capture without re-logging the same payload on every
    user click. Lock the one-shot semantics so a future refactor doesn't
    accidentally turn it into a per-call log line."""
    info = {"CFBundleIdentifier": "com.sayzo.agent"}
    fake_bundle = MagicMock()
    fake_bundle.infoDictionary.return_value = info
    fake_bundle.bundlePath.return_value = "/Applications/Sayzo.app"
    fake_nsbundle = MagicMock()
    fake_nsbundle.mainBundle.return_value = fake_bundle
    fake_foundation = SimpleNamespace(NSBundle=fake_nsbundle)
    # Pretend it's never run in this process.
    mac_permissions._log_bundle_info_plist_once._done = False  # type: ignore[attr-defined]

    with patch(
        "sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"
    ), patch.dict("sys.modules", {"Foundation": fake_foundation}):
        mac_permissions._log_bundle_info_plist_once()
        mac_permissions._log_bundle_info_plist_once()
        mac_permissions._log_bundle_info_plist_once()

    # mainBundle() called exactly once across three calls — the second
    # and third are short-circuited by the `_done` guard.
    assert fake_nsbundle.mainBundle.call_count == 1


def test_log_bundle_info_plist_is_noop_on_non_darwin():
    """The diagnostic is macOS-only. On Windows/Linux it must short-circuit
    before importing Foundation (which doesn't exist there)."""
    mac_permissions._log_bundle_info_plist_once._done = False  # type: ignore[attr-defined]
    fake_nsbundle = MagicMock()
    with patch(
        "sayzo_agent.gui.setup.mac_permissions.sys.platform", "win32"
    ), patch.dict(
        "sys.modules", {"Foundation": SimpleNamespace(NSBundle=fake_nsbundle)}
    ):
        mac_permissions._log_bundle_info_plist_once()
    assert fake_nsbundle.mainBundle.called is False


# ---------------------------------------------------------------------------
# gather_tcc_diagnostic_text + copy_diagnostic_to_clipboard
#
# The "Copy diagnostic info" button on the stale_tcc recovery screen runs
# this end-to-end. Tests guard the structure (so a future regression
# can't silently drop a section a support engineer is reading) plus the
# pbcopy contract.
# ---------------------------------------------------------------------------


def _fake_cfg(tmp_path):
    """Minimal cfg-shaped object exposing the only attribute the
    diagnostic touches: ``logs_dir`` (Path)."""
    return SimpleNamespace(logs_dir=tmp_path)


def test_gather_tcc_diagnostic_text_includes_all_sections(tmp_path):
    """Every block a support engineer triages from must be present:
    version line, bundle introspection (incl. each usage-description
    key with present/MISSING marker), codesign output, recent log lines.
    A future refactor that drops one of those sections leaves us blind
    on a ticket — lock the structure here.
    """
    info = {
        "CFBundleIdentifier": "com.sayzo.agent",
        "CFBundleExecutable": "sayzo-agent",
        "LSUIElement": True,
        "NSMicrophoneUsageDescription": "Sayzo opens the microphone…",
        "NSAudioCaptureUsageDescription": "So Sayzo can hear the other…",
        # NSAppleEventsUsageDescription deliberately missing — must show
        # MISSING so a real-bundle key drop is loud.
    }
    fake_bundle = MagicMock()
    fake_bundle.infoDictionary.return_value = info
    fake_bundle.bundlePath.return_value = "/Applications/Sayzo.app"
    fake_nsbundle = MagicMock()
    fake_nsbundle.mainBundle.return_value = fake_bundle
    fake_foundation = SimpleNamespace(NSBundle=fake_nsbundle)

    log_path = tmp_path / "agent.log"
    log_path.write_text(
        "\n".join(
            [
                "2026-05-09 12:00:00 INFO  sayzo_agent.gui.setup.mac_permissions  [mac_permissions] microphone TCC: status=0 media_type='soun' thread=Thread-1",
                "2026-05-09 12:00:00 INFO  sayzo_agent.gui.setup.mac_permissions  [mac_permissions] microphone TCC: user response → False (elapsed=0.004s, stale_tcc_likely=True)",
                "2026-05-09 12:00:00 INFO  some.other.logger  unrelated noise — should be filtered out",
                "2026-05-09 12:00:01 INFO  sayzo_agent.macos_bundle_heal  [mac_heal] codesign audio-tap already valid",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    cs_completed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout="Executable=/Applications/Sayzo.app/Contents/MacOS/sayzo-agent\n",
        stderr=(
            "Identifier=com.sayzo.agent\n"
            "TeamIdentifier=UYT2A4UX79\n"
            "Authority=Developer ID Application: Sheen Santos Capadngan\n"
        ),
    )

    with patch(
        "sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"
    ), patch.dict(
        "sys.modules", {"Foundation": fake_foundation}
    ), patch(
        "sayzo_agent.gui.setup.mac_permissions.subprocess.run",
        return_value=cs_completed,
    ):
        text = mac_permissions.gather_tcc_diagnostic_text(_fake_cfg(tmp_path))

    # Header + version line.
    assert text.splitlines()[0].startswith("Sayzo TCC diagnostic — ")
    # Bundle section with present + MISSING markers — both signals matter.
    assert "/Applications/Sayzo.app" in text
    assert "NSMicrophoneUsageDescription: present" in text
    assert "NSAudioCaptureUsageDescription: present" in text
    assert "NSAppleEventsUsageDescription: *** MISSING ***" in text
    # codesign block
    assert "codesign -dvv:" in text
    assert "TeamIdentifier=UYT2A4UX79" in text
    assert "Authority=Developer ID Application" in text
    # Filtered log tail — only [mac_permissions]/[mac_heal] lines
    assert "microphone TCC: status=0" in text
    assert "[mac_heal] codesign audio-tap already valid" in text
    assert "unrelated noise" not in text


def test_gather_tcc_diagnostic_text_handles_missing_log_file(tmp_path):
    """Fresh-install path: cfg.logs_dir/agent.log doesn't exist yet.
    The diagnostic must still produce a well-formed report with a
    placeholder for the log section instead of raising."""
    info = {"CFBundleIdentifier": "com.sayzo.agent"}
    fake_bundle = MagicMock()
    fake_bundle.infoDictionary.return_value = info
    fake_bundle.bundlePath.return_value = "/Applications/Sayzo.app"
    fake_nsbundle = MagicMock()
    fake_nsbundle.mainBundle.return_value = fake_bundle
    fake_foundation = SimpleNamespace(NSBundle=fake_nsbundle)

    cs_completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="", stderr=""
    )
    with patch(
        "sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"
    ), patch.dict(
        "sys.modules", {"Foundation": fake_foundation}
    ), patch(
        "sayzo_agent.gui.setup.mac_permissions.subprocess.run",
        return_value=cs_completed,
    ):
        text = mac_permissions.gather_tcc_diagnostic_text(_fake_cfg(tmp_path))
    assert "log file not present" in text


def test_gather_tcc_diagnostic_text_swallows_codesign_failures(tmp_path):
    """codesign missing from PATH (managed Mac with stripped CLI tools)
    must not blow up the diagnostic — we still want the bundle info +
    log lines for triage. Surface the call-failed marker so support can
    see what was tried."""
    info = {"CFBundleIdentifier": "com.sayzo.agent"}
    fake_bundle = MagicMock()
    fake_bundle.infoDictionary.return_value = info
    fake_bundle.bundlePath.return_value = "/Applications/Sayzo.app"
    fake_nsbundle = MagicMock()
    fake_nsbundle.mainBundle.return_value = fake_bundle
    fake_foundation = SimpleNamespace(NSBundle=fake_nsbundle)

    with patch(
        "sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"
    ), patch.dict(
        "sys.modules", {"Foundation": fake_foundation}
    ), patch(
        "sayzo_agent.gui.setup.mac_permissions.subprocess.run",
        side_effect=FileNotFoundError("no codesign"),
    ):
        text = mac_permissions.gather_tcc_diagnostic_text(_fake_cfg(tmp_path))
    assert "codesign call failed" in text


def test_copy_diagnostic_to_clipboard_pipes_text_to_pbcopy(tmp_path):
    """The recovery flow's one-click escalation: pbcopy receives the full
    diagnostic text on stdin. We don't assert the exact text (that's
    gather_tcc_diagnostic_text's contract) — just that pbcopy got a
    non-trivial chunk of stdin and we returned True on rc=0."""
    fake_bundle = MagicMock()
    fake_bundle.infoDictionary.return_value = {
        "CFBundleIdentifier": "com.sayzo.agent"
    }
    fake_bundle.bundlePath.return_value = "/Applications/Sayzo.app"
    fake_nsbundle = MagicMock()
    fake_nsbundle.mainBundle.return_value = fake_bundle
    fake_foundation = SimpleNamespace(NSBundle=fake_nsbundle)

    captured: dict = {}

    def fake_run(args, **kwargs):
        # The diagnostic calls subprocess.run twice: once for codesign,
        # once for pbcopy. Capture the pbcopy call's stdin so we can
        # verify the diagnostic text was actually piped.
        if args and args[0] == "pbcopy":
            captured["pbcopy_input"] = kwargs.get("input")
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="", stderr=""
            )
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout="", stderr=""
        )

    with patch(
        "sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"
    ), patch.dict(
        "sys.modules", {"Foundation": fake_foundation}
    ), patch(
        "sayzo_agent.gui.setup.mac_permissions.subprocess.run",
        side_effect=fake_run,
    ):
        ok = mac_permissions.copy_diagnostic_to_clipboard(_fake_cfg(tmp_path))
    assert ok is True
    assert captured.get("pbcopy_input"), "pbcopy received empty stdin"
    assert "Sayzo TCC diagnostic" in captured["pbcopy_input"]


def test_copy_diagnostic_to_clipboard_returns_false_on_pbcopy_failure(tmp_path):
    """Hardened-runtime / sandboxed scenarios where pbcopy refuses to
    accept stdin: we must return False so the React button can flash
    "Copy failed — try again" instead of a misleading "Copied!"."""
    fake_bundle = MagicMock()
    fake_bundle.infoDictionary.return_value = {
        "CFBundleIdentifier": "com.sayzo.agent"
    }
    fake_bundle.bundlePath.return_value = "/Applications/Sayzo.app"
    fake_nsbundle = MagicMock()
    fake_nsbundle.mainBundle.return_value = fake_bundle
    fake_foundation = SimpleNamespace(NSBundle=fake_nsbundle)

    def fake_run(args, **kwargs):
        if args and args[0] == "pbcopy":
            return subprocess.CompletedProcess(
                args=args, returncode=1, stdout="", stderr="pbcopy: failed\n"
            )
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout="", stderr=""
        )

    with patch(
        "sayzo_agent.gui.setup.mac_permissions.sys.platform", "darwin"
    ), patch.dict(
        "sys.modules", {"Foundation": fake_foundation}
    ), patch(
        "sayzo_agent.gui.setup.mac_permissions.subprocess.run",
        side_effect=fake_run,
    ):
        assert (
            mac_permissions.copy_diagnostic_to_clipboard(_fake_cfg(tmp_path))
            is False
        )


def test_copy_diagnostic_to_clipboard_is_noop_on_non_darwin(tmp_path):
    with patch(
        "sayzo_agent.gui.setup.mac_permissions.sys.platform", "win32"
    ), patch(
        "sayzo_agent.gui.setup.mac_permissions.subprocess.run"
    ) as run:
        assert (
            mac_permissions.copy_diagnostic_to_clipboard(_fake_cfg(tmp_path))
            is False
        )
    assert run.called is False
