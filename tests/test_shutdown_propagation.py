"""Regression test for the OS-shutdown propagation callback.

Pre-v3.20 the nested ``_on_os_shutting_down`` in
``__main__._install_agent_side_hud_shutdown_propagation`` referenced an
undefined ``log`` (no module-level logger; each command defines its own
local). The callback fired on real OS shutdown only, so it shipped a
``NameError: name 'log' is not defined`` to production on BOTH macOS and
Windows — silently defeating the parent->HUD quit propagation. No test ever
invoked the callback. This is that test.
"""
from __future__ import annotations

import sayzo_agent.gui.common.mac_shutdown as mac_sd
import sayzo_agent.gui.common.win_shutdown as win_sd
from sayzo_agent.__main__ import _install_agent_side_hud_shutdown_propagation


class _FakeLauncher:
    def __init__(self) -> None:
        self.quit_calls = 0

    def quit_sync(self, timeout_secs: float = 1.0) -> None:
        self.quit_calls += 1


def test_os_shutdown_callback_runs_without_nameerror(monkeypatch):
    captured: list = []
    # Both installers are imported locally inside the function under test, so
    # patching the module attributes here is picked up at call time.
    monkeypatch.setattr(
        win_sd, "install_session_ending_callback", captured.append
    )
    monkeypatch.setattr(mac_sd, "observe_will_power_off", captured.append)

    fake = _FakeLauncher()
    _install_agent_side_hud_shutdown_propagation(fake)

    # Registered with both platform observers (each no-ops on the wrong OS).
    assert len(captured) == 2
    for cb in captured:
        # Pre-fix this raised NameError: name 'log' is not defined.
        cb()
    # The callback actually pushed quit to the HUD each time (not swallowed).
    assert fake.quit_calls == 2


def test_os_shutdown_callback_swallows_launcher_failure(monkeypatch):
    """A failing quit_sync must be caught + logged, not re-raised into the
    OS-shutdown observer (the whole point of the belt-and-suspenders path)."""
    captured: list = []
    monkeypatch.setattr(
        win_sd, "install_session_ending_callback", captured.append
    )
    monkeypatch.setattr(mac_sd, "observe_will_power_off", captured.append)

    class _Boom:
        def quit_sync(self, timeout_secs: float = 1.0):
            raise RuntimeError("hud already dead")

    _install_agent_side_hud_shutdown_propagation(_Boom())
    for cb in captured:
        cb()  # must not raise
