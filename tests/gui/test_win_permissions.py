"""Unit tests for sayzo_agent.gui.setup.win_permissions."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from sayzo_agent.gui.setup import win_permissions


@pytest.fixture(autouse=True)
def _reset_notifier_singleton():
    win_permissions._NOTIFIER = None
    win_permissions._NOTIFIER_INIT_FAILED = False
    yield
    win_permissions._NOTIFIER = None
    win_permissions._NOTIFIER_INIT_FAILED = False


def _patch_notifier(has_auth_return):
    fake = MagicMock()
    if isinstance(has_auth_return, Exception):
        fake.has_authorisation.side_effect = has_auth_return
    else:
        fake.has_authorisation.return_value = has_auth_return
    module = SimpleNamespace(DesktopNotifierSync=MagicMock(return_value=fake))
    return patch.dict("sys.modules", {"desktop_notifier.sync": module})


def test_has_notification_permission_true():
    with patch(
        "sayzo_agent.gui.setup.win_permissions.sys.platform", "win32"
    ), _patch_notifier(True):
        assert win_permissions.has_notification_permission() is True


def test_has_notification_permission_false():
    with patch(
        "sayzo_agent.gui.setup.win_permissions.sys.platform", "win32"
    ), _patch_notifier(False):
        assert win_permissions.has_notification_permission() is False


def test_has_notification_permission_none_on_backend_error():
    with patch(
        "sayzo_agent.gui.setup.win_permissions.sys.platform", "win32"
    ), _patch_notifier(RuntimeError("boom")):
        assert win_permissions.has_notification_permission() is None


def test_has_notification_permission_none_on_non_windows():
    with patch("sayzo_agent.gui.setup.win_permissions.sys.platform", "darwin"):
        assert win_permissions.has_notification_permission() is None


def test_has_notification_permission_none_when_init_fails():
    failing_ctor = MagicMock(side_effect=RuntimeError("no backend"))
    module = SimpleNamespace(DesktopNotifierSync=failing_ctor)
    with patch(
        "sayzo_agent.gui.setup.win_permissions.sys.platform", "win32"
    ), patch.dict("sys.modules", {"desktop_notifier.sync": module}):
        assert win_permissions.has_notification_permission() is None


def test_open_notification_settings_spawns_ms_settings():
    with patch(
        "sayzo_agent.gui.setup.win_permissions.sys.platform", "win32"
    ), patch("sayzo_agent.gui.setup.win_permissions.subprocess.Popen") as popen:
        win_permissions.open_notification_settings()
    assert popen.call_count == 1
    # The command list should contain the ms-settings URI.
    args = popen.call_args.args[0]
    assert "ms-settings:notifications" in args


def test_open_notification_settings_noop_on_non_windows():
    with patch(
        "sayzo_agent.gui.setup.win_permissions.sys.platform", "darwin"
    ), patch("sayzo_agent.gui.setup.win_permissions.subprocess.Popen") as popen:
        win_permissions.open_notification_settings()
    assert popen.called is False


def test_open_notification_settings_swallows_oserror():
    with patch(
        "sayzo_agent.gui.setup.win_permissions.sys.platform", "win32"
    ), patch(
        "sayzo_agent.gui.setup.win_permissions.subprocess.Popen",
        side_effect=OSError("start failed"),
    ):
        # Should not raise.
        win_permissions.open_notification_settings()
