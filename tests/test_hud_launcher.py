"""Unit tests for HudLauncher's pure parent-side logic (no Qt, no subprocess).

Covers the v3.14 hardening: respawn unification (single path, no inline
spawn), pill replay on hud_ready, heartbeat pong bookkeeping, give-up +
recovery, and the install-update quit paint-grace. The subprocess itself is
never spawned — we monkeypatch the send path and drive the dispatcher /
state methods directly.
"""
from __future__ import annotations

import asyncio
import time
from concurrent.futures import Future

import pytest

import sayzo_agent.gui.hud.launcher as launcher_mod
from sayzo_agent.gui.hud.launcher import Cmd, Evt, HudLauncher


def _capture_sends(launcher) -> list[dict]:
    """Replace _send_threadsafe with a recorder; return the captured list."""
    sent: list[dict] = []

    def _fake_send(payload: dict) -> bool:
        sent.append(payload)
        return True

    launcher._send_threadsafe = _fake_send  # type: ignore[assignment]
    return sent


# --- pill replay on hud_ready ------------------------------------------------


def test_hud_ready_replays_active_pill():
    launcher = HudLauncher()
    sent = _capture_sends(launcher)
    launcher.show_pill(reason="hotkey", reason_label="Hotkey", start_ts=123.0)
    sent.clear()  # drop the original show_pill
    launcher._dispatch_event({"event": Evt.HUD_READY})
    pill_cmds = [p for p in sent if p.get("cmd") == Cmd.SHOW_PILL]
    assert len(pill_cmds) == 1
    assert pill_cmds[0]["reason"] == "hotkey"
    assert pill_cmds[0]["start_ts"] == 123.0


def test_hud_ready_no_replay_after_hide_pill():
    launcher = HudLauncher()
    sent = _capture_sends(launcher)
    launcher.show_pill(reason="hotkey", reason_label="Hotkey", start_ts=1.0)
    launcher.hide_pill()
    sent.clear()
    launcher._dispatch_event({"event": Evt.HUD_READY})
    assert [p for p in sent if p.get("cmd") == Cmd.SHOW_PILL] == []


# --- heartbeat pong bookkeeping ---------------------------------------------


def test_pong_resets_outstanding_pings():
    launcher = HudLauncher()
    launcher._outstanding_pings = 2
    launcher._dispatch_event({"event": Evt.PONG, "id": "ping-2"})
    assert launcher._outstanding_pings == 0


def test_hud_ready_resets_outstanding_pings():
    launcher = HudLauncher()
    _capture_sends(launcher)
    launcher._outstanding_pings = 2
    launcher._dispatch_event({"event": Evt.HUD_READY})
    assert launcher._outstanding_pings == 0


# --- give-up + recovery ------------------------------------------------------


def test_fail_pending_consents_uses_each_caller_default():
    launcher = HudLauncher()
    f_no: Future = Future()
    f_timeout: Future = Future()
    launcher._pending_cards = {"a": (f_no, "no"), "b": (f_timeout, "timeout")}
    launcher._fail_pending_consents()
    assert f_no.result() == "no"
    assert f_timeout.result() == "timeout"
    assert launcher._pending_cards == {}


def test_fail_pending_consents_fires_actionable_on_expire():
    launcher = HudLauncher()
    fired: list[str] = []
    launcher._pending_actionables = {
        "insight-1": {"on_pressed": None, "on_expire": lambda: fired.append("x"),
                      "on_secondary": None},
    }
    launcher._fail_pending_consents()
    assert fired == ["x"]


def test_reset_given_up_clears_state():
    launcher = HudLauncher()
    launcher._given_up = True
    launcher._respawn_count = 3
    launcher._respawn_window_started = 999.0
    # No loop set → reset clears flags and skips the start() schedule.
    launcher.reset_given_up()
    assert launcher._given_up is False
    assert launcher._respawn_count == 0


def test_health_callback_fired_on_give_up():
    launcher = HudLauncher()
    health: list[bool] = []
    launcher.set_health_callback(lambda ok: health.append(ok))
    launcher._fire_health(False)
    assert health == [False]


def test_given_up_makes_public_methods_noop():
    launcher = HudLauncher()
    launcher._given_up = True
    assert launcher.show_toast("t", "b") is False
    assert launcher.show_pill(reason="hotkey", reason_label="x") is False
    assert launcher.ask_consent("t", "b", "y", "n", 1.0, default_on_timeout="no") == "no"


# --- install-update quit paint-grace ----------------------------------------


def test_show_toast_before_quit_arms_marker():
    launcher = HudLauncher()
    _capture_sends(launcher)
    assert launcher.show_toast_before_quit("Updating", "soon", ttl_secs=2.0) is True
    assert launcher._quit_grace_toast_id is not None
    assert launcher._quit_grace_toast_id in launcher._pending_show_times
    # The toast's ttl is stashed so quit() lingers exactly that long after
    # paint (countdown bar runs to 0% instead of freezing on teardown).
    assert launcher._quit_grace_toast_ttl == 2.0


@pytest.mark.asyncio
async def test_quit_grace_no_marker_returns_immediately():
    launcher = HudLauncher()
    # No marker armed → must be a no-op (zero added latency).
    await launcher._wait_for_quit_grace_toast()
    assert launcher._quit_grace_toast_id is None


@pytest.mark.asyncio
async def test_quit_grace_bounded_when_never_painted(monkeypatch):
    monkeypatch.setattr(launcher_mod, "_QUIT_PAINT_GRACE_SECS", 0.05)
    launcher = HudLauncher()
    _capture_sends(launcher)
    launcher.show_toast_before_quit("Updating", "soon", ttl_secs=2.0)
    # Toast id stays in _pending_show_times (never painted) → must give up at
    # the grace deadline rather than hang (the ttl linger is never reached
    # because paint never lands).
    loop = asyncio.get_running_loop()
    start = loop.time()
    await launcher._wait_for_quit_grace_toast()
    assert loop.time() - start < 1.0
    # One-shot: marker + stashed ttl consumed.
    assert launcher._quit_grace_toast_id is None
    assert launcher._quit_grace_toast_ttl == 0.0


@pytest.mark.asyncio
async def test_quit_grace_lingers_remaining_ttl_when_fresh(monkeypatch):
    # Painted with ~full ttl remaining (shown ~now) → linger runs ~ttl so the
    # countdown bar reaches 0% before teardown.
    monkeypatch.setattr(launcher_mod, "_QUIT_PAINT_GRACE_SECS", 1.0)
    launcher = HudLauncher()
    _capture_sends(launcher)
    launcher.show_toast_before_quit("Updating", "soon", ttl_secs=0.2)
    toast_id = launcher._quit_grace_toast_id
    launcher._pending_show_times.pop(toast_id, None)  # paint ack
    loop = asyncio.get_running_loop()
    start = loop.time()
    await launcher._wait_for_quit_grace_toast()
    elapsed = loop.time() - start
    # Lower bound proves the linger ran for ~ttl (the regression was linger==0
    # → froze). Upper bound is generous (asyncio.sleep never returns early;
    # only a loaded CI runner can overshoot) — sibling-test headroom, not a
    # tight multiple that flakes under load.
    assert 0.1 <= elapsed < 1.0


@pytest.mark.asyncio
async def test_quit_grace_caps_linger_at_remaining_ttl(monkeypatch):
    # If the toast has ALREADY been on screen ~ttl (e.g. settings_launcher.quit
    # elapsed before hud quit ran), linger only the REMAINING countdown — not a
    # fresh full ttl of dead empty-screen delay before the update relaunch.
    monkeypatch.setattr(launcher_mod, "_QUIT_PAINT_GRACE_SECS", 1.0)
    launcher = HudLauncher()
    _capture_sends(launcher)
    launcher.show_toast_before_quit("Updating", "soon", ttl_secs=1.0)
    toast_id = launcher._quit_grace_toast_id
    # Pretend the toast was shown 0.95 s ago → only ~0.05 s of countdown left.
    launcher._quit_grace_toast_shown_at = time.monotonic() - 0.95
    launcher._pending_show_times.pop(toast_id, None)  # paint ack
    loop = asyncio.get_running_loop()
    start = loop.time()
    await launcher._wait_for_quit_grace_toast()
    elapsed = loop.time() - start
    # Without the cap this would sleep a fresh 1.0 s.
    assert elapsed < 0.5


# --- respawn unification -----------------------------------------------------


@pytest.mark.asyncio
async def test_send_async_when_down_does_not_spawn_inline(monkeypatch):
    launcher = HudLauncher()
    launcher._loop = asyncio.get_running_loop()
    spawn_calls: list[int] = []

    async def _fake_spawn() -> None:
        spawn_calls.append(1)

    monkeypatch.setattr(launcher, "_spawn_locked", _fake_spawn)
    monkeypatch.setattr(launcher_mod, "_RESPAWN_DELAYS", (0.0, 0.0, 0.0))
    # _proc is None → subprocess is "down".
    await launcher._send_async({"cmd": Cmd.SHOW_TOAST, "id": "x"})
    # The send path itself must NOT spawn inline (pre-v3.14 bug) — respawn is
    # delegated to the single _ensure_respawn_scheduled path.
    assert spawn_calls == []
    assert launcher._respawn_task is not None


@pytest.mark.asyncio
async def test_ensure_respawn_scheduled_is_idempotent():
    launcher = HudLauncher()
    launcher._loop = asyncio.get_running_loop()
    launcher._ensure_respawn_scheduled()
    first = launcher._respawn_task
    launcher._ensure_respawn_scheduled()
    assert launcher._respawn_task is first  # no second task while one pending
    if first is not None:
        first.cancel()
