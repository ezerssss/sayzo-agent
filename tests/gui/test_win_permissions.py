"""Tests for Windows setup permission stubs (HUD era).

Pre-v2.10 the win_permissions module owned the WinRT notification
permission flow for the Notifications onboarding screen. With the
HUD rewrite the screen is gone, the functions are no-op stubs, and
these tests verify the stubs return the documented constants.
"""
from __future__ import annotations

from unittest.mock import patch

from sayzo_agent.gui.setup import win_permissions


def test_has_notification_permission_returns_true_on_win32():
    """The HUD doesn't need OS notification permission — the stub
    reports True so legacy callers (if any) see ``granted``."""
    with patch("sayzo_agent.gui.setup.win_permissions.sys.platform", "win32"):
        assert win_permissions.has_notification_permission() is True


def test_has_notification_permission_returns_none_on_non_win32():
    with patch("sayzo_agent.gui.setup.win_permissions.sys.platform", "darwin"):
        assert win_permissions.has_notification_permission() is None


def test_send_verification_notification_is_noop():
    """No toast fires — the HUD's diagnose-notifications CLI is the
    real check now."""
    assert win_permissions.send_verification_notification() is False


def test_open_notification_settings_spawns_ms_settings():
    with patch(
        "sayzo_agent.gui.setup.win_permissions.sys.platform", "win32"
    ), patch(
        "sayzo_agent.gui.setup.win_permissions.subprocess.Popen"
    ) as popen:
        win_permissions.open_notification_settings()
    assert popen.call_count == 1
    args = popen.call_args.args[0]
    assert args == ["cmd", "/c", "start", "", "ms-settings:notifications"]


def test_open_notification_settings_noop_on_non_win32():
    with patch(
        "sayzo_agent.gui.setup.win_permissions.sys.platform", "darwin"
    ), patch(
        "sayzo_agent.gui.setup.win_permissions.subprocess.Popen"
    ) as popen:
        win_permissions.open_notification_settings()
    popen.assert_not_called()


def test_open_notification_settings_swallows_oserror():
    """OSError on Popen must never propagate — the setup bridge expects
    these helpers to be silent on failure."""
    with patch(
        "sayzo_agent.gui.setup.win_permissions.sys.platform", "win32"
    ), patch(
        "sayzo_agent.gui.setup.win_permissions.subprocess.Popen",
        side_effect=OSError("nope"),
    ):
        win_permissions.open_notification_settings()  # no raise
