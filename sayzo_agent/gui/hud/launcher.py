"""Parent-side HUD subprocess manager.

Owns the lifecycle of ``sayzo-agent hud --idle``: spawn, talk to it via
stdin / stdout, respawn on crash with a bounded retry ladder, quit on
agent shutdown. Exposes a synchronous API that mirrors the legacy
``notify.py`` surface — ``notify``, ``ask_consent``, ``notify_actionable``
— plus HUD-specific pill controls (``show_pill``, ``hide_pill``,
``set_pill_collapsed``).

Threading model: the launcher's public methods are safe to call from any
thread. The stdin write coroutine and the stdout reader both run as
asyncio tasks on the launcher's loop (``_send_threadsafe`` marshals
writes onto it via ``run_coroutine_threadsafe``); the reader resolves
per-request futures. ``ask_consent`` blocks the caller's thread on a
``concurrent.futures.Future``; the caller must not be on the asyncio
loop that will need to schedule other work — same constraint as the
legacy ``DesktopNotifier.ask_consent``.

Failure modes:

* Stdin pipe broken or subprocess crashed → respawn with
  ``_RESPAWN_DELAYS`` (5 s / 15 s / 60 s) backoff, scheduled through the
  single ``_ensure_respawn_scheduled`` entry point. After
  ``_MAX_RESPAWNS`` crashes inside ``_RESPAWN_WINDOW_SECS`` (120 s),
  give up for the rest of the session — every public method becomes a
  no-op that returns ``default_on_timeout``, and the registered health
  callback fires so the tray can surface it. An explicit user arm calls
  ``reset_given_up`` to leave that state.
* Subprocess alive but hung (Qt loop deadlock / GPU hang) → the
  heartbeat loop (``heartbeat_secs``, 0 disables) kills it after
  ``_MISSED_PONGS_BEFORE_KILL`` unanswered pings, converting the hang
  into a normal crash → respawn.
* Subprocess never sends ``hud_ready`` → first ``ask_consent`` returns
  ``default_on_timeout`` (we don't block forever). The pill / toast
  commands queue inside the subprocess and play once the window loads,
  so cold-boot fire-and-forget toasts are forgiving.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from concurrent.futures import (
    Future,
    InvalidStateError,
    TimeoutError as FuturesTimeout,
)
from typing import Any, Callable, Literal, Optional

log = logging.getLogger(__name__)

ConsentResult = Literal["yes", "no", "timeout"]
ReasonKind = Literal["hotkey", "whitelist", "manual"]


# Wire-protocol command + event names. Strings here MUST match the
# discriminated unions declared in
# ``sayzo_agent/gui/webui/src/lib/hud-bridge.ts`` — the TypeScript side
# is statically checked but Python is not, so drifts will silently break
# the round-trip.
class Cmd:
    SHOW_PILL = "show_pill"
    HIDE_PILL = "hide_pill"
    SET_PILL_COLLAPSED = "set_pill_collapsed"
    SET_AUDIO_LEVELS = "set_audio_levels"
    SHOW_CARD = "show_card"
    SHOW_TOAST = "show_toast"
    SHOW_ACTIONABLE = "show_actionable"
    SHOW_INSIGHT = "show_insight"
    HIDE_CARD = "hide_card"
    HIDE_ALL = "hide_all"
    DEMO_MODE = "demo_mode"
    QUIT = "quit"
    # Liveness ping. Handled entirely at the Qt level in window.py's
    # stdin loop — it never reaches React, so it is deliberately NOT
    # part of the HudCommand union in hud-bridge.ts.
    PING = "ping"


class Evt:
    HUD_READY = "hud_ready"
    CARD_RESPONSE = "card_response"
    ACTIONABLE_RESPONSE = "actionable_response"
    INSIGHT_RESPONSE = "insight_response"
    CARD_PAINTED = "card_painted"
    PILL_STOP_CLICKED = "pill_stop_clicked"
    PILL_COLLAPSED = "pill_collapsed"
    PILL_EXPANDED = "pill_expanded"
    LOG = "log"
    PONG = "pong"


_RESPAWN_DELAYS = (5.0, 15.0, 60.0)
_RESPAWN_WINDOW_SECS = 120.0
_MAX_RESPAWNS = len(_RESPAWN_DELAYS)

# Bounded window quit() gives a `show_toast_before_quit` toast to reach
# its first paint (card_painted) before tearing the subprocess down.
# After paint, quit() lingers for the toast's OWN ttl (stored in
# `_quit_grace_toast_ttl`) so its countdown bar runs visibly to 0% rather
# than freezing mid-fill when the HUD dies. The install-update quit path
# arms this via __main__._fire_pre_apply_toast → notify_before_quit; every
# other quit pays zero extra latency. `_QUIT_PAINT_GRACE_SECS` is the cap
# on how long we wait for that FIRST paint. Module-level so tests can
# monkeypatch it down to ~0.05 s.
_QUIT_PAINT_GRACE_SECS = 1.5

# Heartbeat: after this many pings in a row with no pong, the subprocess
# is treated as alive-but-hung (Qt loop deadlock, GPU hang) and killed so
# the normal respawn ladder takes over. Detection latency is therefore
# (_MISSED_PONGS_BEFORE_KILL + 1) * heartbeat_secs worst-case.
_MISSED_PONGS_BEFORE_KILL = 2

# Throttle window for the "subprocess down — dropping payload" summary log.
_DOWN_DROP_LOG_INTERVAL_SECS = 5.0


def _hud_subprocess_argv() -> list[str]:
    """``argv`` for spawning ``sayzo-agent hud --idle``.

    Frozen builds use the single bundled binary; dev runs use
    ``python -m sayzo_agent hud`` so the entry point resolves without
    relying on the ``sayzo-agent`` console-script being on PATH.
    """
    if not getattr(sys, "frozen", False):
        return [sys.executable, "-m", "sayzo_agent", "hud"]
    return [sys.executable, "hud"]


def _hud_subprocess_env() -> dict[str, str]:
    """Env for the HUD subprocess — inherited from the parent agent."""
    return dict(os.environ)


class HudLauncher:
    """Manage the HUD subprocess + dispatch its event stream."""

    def __init__(self, heartbeat_secs: float = 30.0) -> None:
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._lock = asyncio.Lock()
        # Liveness ping cadence. 0 disables (same convention as
        # Config.heartbeat_secs). Production passes
        # cfg.hud.heartbeat_secs; the default keeps preview_hud.py /
        # diagnose-notifications constructors working unchanged.
        self._heartbeat_secs = max(0.0, float(heartbeat_secs))
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._outstanding_pings = 0
        self._ping_seq = 0
        # Per-request future map, keyed by ``request_id``. Resolved by
        # the stdout reader when it sees the matching ``card_response``
        # event. Stores ``(future, default_on_timeout)`` so the
        # supersede path in :meth:`ask_consent` can resolve the OLD
        # caller's future with the OLD caller's default — using the
        # new caller's default would silently return wrong data,
        # since the two callers can pass different defaults
        # (e.g. ``"no"`` vs ``"timeout"``).
        self._pending_cards: dict[str, tuple[Future, str]] = {}
        self._pending_actionables: dict[str, dict[str, Any]] = {}
        # Schedule time (monotonic) per request_id / toast id, populated
        # the moment a SHOW_* command goes onto stdin. The React side
        # emits ``card_painted`` after one rAF in the component's mount
        # effect; the stdout reader computes ``delta_ms`` between the
        # two and logs it. Diagnoses the layered-window paint-stall
        # (window.py:319-326). Entries are popped on every termination
        # path via :meth:`_clear_pending_show_tracking` so this dict
        # cannot grow unbounded over long-running sessions.
        self._pending_show_times: dict[str, float] = {}
        # Callbacks registered by the ArmController for pill events.
        self._on_pill_stop: Optional[Callable[[], None]] = None
        self._on_pill_collapsed: Optional[Callable[[bool], None]] = None
        # Snapshot of the most recent ``show_pill`` kwargs (set on
        # show, cleared on hide). Used by
        # :meth:`ask_consent_pausing_pill` to restore the pill after a
        # consent that the user opts to keep going from. Mirrors the
        # behaviour ``ArmController._ask_consent_pausing_pill``
        # previously implemented inline.
        self._last_pill_params: Optional[dict[str, Any]] = None
        # Crash bookkeeping.
        self._respawn_count = 0
        self._respawn_window_started: float = 0.0
        self._given_up = False
        # Single in-flight respawn task — every respawn is scheduled
        # through _ensure_respawn_scheduled so the backoff ladder can't
        # be bypassed or doubled (pre-v3.14 _send_async spawned inline,
        # racing the ladder into duplicate HUD processes).
        self._respawn_task: Optional[asyncio.Task] = None
        # Health reporting for the give-up state (tray surface). Fired
        # with False when the ladder gives up, True when a later
        # hud_ready proves recovery after reset_given_up().
        self._health_cb: Optional[Callable[[bool], None]] = None
        self._reported_degraded = False
        # Toast id armed by show_toast_before_quit; quit() polls for its
        # card_painted before teardown (install-update path only).
        self._quit_grace_toast_id: Optional[str] = None
        # The armed toast's ttl; quit() lingers so the countdown bar finishes
        # rather than freezing when the HUD dies. Lingered relative to when the
        # toast was SHOWN (_quit_grace_toast_shown_at), not when quit() notices
        # the paint — preceding teardown (settings_launcher.quit) already
        # elapsed, so a fresh full-ttl sleep would add dead empty-screen time.
        self._quit_grace_toast_ttl: float = 0.0
        self._quit_grace_toast_shown_at: float = 0.0
        # Readiness — flipped when the subprocess writes ``hud_ready``.
        self._ready_event = asyncio.Event()
        self._reader_task: Optional[asyncio.Task] = None
        # Throttle for the "subprocess down — dropping payload" log. A HUD
        # death mid-session produced a 53-line burst in 5 s (one per dropped
        # pill-frame during the respawn window); collapse it to a periodic
        # summary so the signal survives without the flood.
        self._down_drop_count = 0
        self._down_drop_last_log: float = 0.0
        # Lock for the synchronous-write path. The stdin pipe itself is
        # only safe to drain from the loop thread.
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ------------------------------------------------------------------
    # Lifecycle.
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Spawn the HUD subprocess and start reading its stdout.

        Idempotent — calling on a live launcher is a no-op.
        """
        self._loop = asyncio.get_running_loop()
        async with self._lock:
            if self._proc is not None and self._proc.returncode is None:
                return
            if self._given_up:
                log.warning("[hud] launcher: in giving-up state — skipping start")
                return
            await self._spawn_locked()
        if self._heartbeat_secs > 0 and (
            self._heartbeat_task is None or self._heartbeat_task.done()
        ):
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(), name="hud-heartbeat",
            )

    async def _spawn_locked(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            # A concurrent path already brought a live subprocess up
            # (e.g. reset_given_up's start() racing a pending respawn).
            log.info("[hud] _spawn_locked: live subprocess exists — skipping")
            return
        # Reap the previous reader before overwriting its reference —
        # without this, an orphaned reader task could still dispatch
        # late events from a dead pipe while the new reader runs.
        old_reader = self._reader_task
        if old_reader is not None and not old_reader.done():
            old_reader.cancel()
            try:
                await old_reader
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        argv = _hud_subprocess_argv() + ["--idle"]
        log.info("[hud] spawning subprocess: %s", argv)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                # stderr inherits — HUD logs land in agent.log via the
                # standard logging config the CLI command installs.
                env=_hud_subprocess_env(),
            )
        except Exception:
            log.warning("[hud] subprocess spawn failed", exc_info=True)
            self._proc = None
            return
        self._ready_event.clear()
        self._outstanding_pings = 0
        self._reader_task = asyncio.create_task(
            self._stdout_reader_loop(self._proc),
            name="hud-stdout-reader",
        )

    async def wait_for_ready(self, timeout_secs: float = 15.0) -> bool:
        """Block until the subprocess emits ``hud_ready`` or timeout.

        Returns ``True`` on success, ``False`` on timeout. Await this before
        firing a toast/card right after ``start()`` (e.g. the post-upgrade
        toast): until the subprocess exists AND has handshaked ``hud_ready``,
        ``_send`` hits ``_proc is None`` and DROPS the payload. The child
        only buffers commands that arrive after its process is up but before
        ``loadFinished`` — it can't catch anything sent before it's spawned,
        so "fire-and-forget is always safe" was wrong (it dropped the
        post-update toast on every auto-update).
        """
        if self._given_up:
            return False
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=timeout_secs)
            return True
        except asyncio.TimeoutError:
            return False

    def quit_sync(self, timeout_secs: float = 1.0) -> None:
        """Synchronous wrapper around :meth:`quit` for non-asyncio callers.

        Marshals the quit coroutine onto the launcher's running loop via
        ``asyncio.run_coroutine_threadsafe``. Safe to call from any
        thread that doesn't own the loop — specifically the
        ``SystemEvents.SessionEnding`` callback on Windows and the
        ``NSWorkspaceWillPowerOffNotification`` observer on macOS,
        both of which run on platform-specific threads and need a way
        to push a quit command without ``await``-ing.

        Best-effort: silently no-ops if the loop isn't running yet, or
        if the loop has been closed (we're racing the agent's own
        shutdown). Timeout is short by design — these callbacks fire
        when the OS is initiating a shutdown and we have ~5 s before
        Windows / macOS starts force-killing processes.
        """
        loop = self._loop
        if loop is None or not loop.is_running():
            log.warning("[hud] quit_sync called before loop ready or after close")
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self.quit(timeout_secs=timeout_secs), loop
            )
            fut.result(timeout=timeout_secs + 0.5)
        except Exception:
            log.warning("[hud] quit_sync failed", exc_info=True)

    async def quit(self, timeout_secs: float = 3.0) -> None:
        """Send ``quit`` and wait for the subprocess to exit.

        Confirms the process is actually reaped before returning, even
        on the terminate → kill escalation path. The caller that follows
        us on the install-update quit (``apply_staged_at_quit_if_flagged``
        → silent NSIS) relies on this: a killed-but-not-yet-reaped HUD
        still holds the exe / DLL image handles, and the installer's
        ``File /r`` racing that teardown is exactly the "Error opening
        file for writing" / WerFault dialog users reported when
        clicking Update.
        """
        await self._wait_for_quit_grace_toast()
        for task in (self._heartbeat_task, self._respawn_task):
            # A pending respawn firing after quit would resurrect the
            # HUD into a shutting-down agent; the heartbeat would ping
            # a pipe we're about to close.
            if task is not None and not task.done():
                task.cancel()
        async with self._lock:
            proc = self._proc
            self._proc = None
            if proc is None or proc.returncode is not None:
                return
            log.info("[hud] sending quit to subprocess")
            try:
                if proc.stdin is not None:
                    proc.stdin.write(b'{"cmd":"' + Cmd.QUIT.encode() + b'"}\n')
                    await proc.stdin.drain()
                    proc.stdin.close()
            except Exception:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout_secs)
                return
            except asyncio.TimeoutError:
                log.warning("[hud] subprocess didn't quit in %.1fs — terminating", timeout_secs)
            try:
                proc.terminate()
            except ProcessLookupError:
                return
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                log.warning("[hud] terminate didn't take — killing")
                try:
                    proc.kill()
                except ProcessLookupError:
                    return
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    log.warning("[hud] kill not reaped in 2s — proceeding")

    async def _wait_for_quit_grace_toast(self) -> None:
        """Give a ``show_toast_before_quit`` toast a bounded paint window.

        One-shot: consumes the marker armed by
        :meth:`show_toast_before_quit`. Polls for the toast's
        ``card_painted`` (its entry leaving ``_pending_show_times`` —
        the reader task keeps running while we await) for up to
        ``_QUIT_PAINT_GRACE_SECS``; once painted, lingers for whatever is
        LEFT of the toast's ttl (``_quit_grace_toast_ttl`` measured from
        ``_quit_grace_toast_shown_at``) so its countdown bar runs visibly to
        0% rather than freezing mid-fill — but without re-sleeping time the
        toast was already on screen during preceding teardown.
        No-op (zero added latency) on every quit that didn't arm the
        marker — i.e. everything except the install-update path.
        """
        toast_id = self._quit_grace_toast_id
        ttl_secs = self._quit_grace_toast_ttl
        shown_at = self._quit_grace_toast_shown_at
        self._quit_grace_toast_id = None
        self._quit_grace_toast_ttl = 0.0
        self._quit_grace_toast_shown_at = 0.0
        if toast_id is None:
            return
        deadline = time.monotonic() + _QUIT_PAINT_GRACE_SECS
        while time.monotonic() < deadline:
            if toast_id not in self._pending_show_times:
                # Linger only the REMAINING countdown — the toast has been
                # visible since shown_at, so cap total on-screen time at ttl
                # instead of sleeping a fresh full ttl of dead empty-screen
                # delay after settings teardown already elapsed.
                remaining = ttl_secs - (time.monotonic() - shown_at)
                if remaining > 0:
                    await asyncio.sleep(remaining)
                return
            await asyncio.sleep(0.05)
        log.info(
            "[hud] quit: grace toast never painted within %.1fs — proceeding",
            _QUIT_PAINT_GRACE_SECS,
        )

    # ------------------------------------------------------------------
    # Stdout reader.
    # ------------------------------------------------------------------

    async def _stdout_reader_loop(
        self, proc: asyncio.subprocess.Process,
    ) -> None:
        assert proc.stdout is not None
        cancelled = False
        try:
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("[hud] reader: malformed JSON: %r", line[:200])
                    continue
                self._dispatch_event(payload)
        except asyncio.CancelledError:
            # _spawn_locked is replacing this reader (or quit() is
            # tearing down) — not a crash; don't schedule a respawn.
            cancelled = True
            raise
        except Exception:
            log.warning("[hud] stdout reader crashed", exc_info=True)
        finally:
            if not cancelled:
                rc = proc.returncode
                log.info("[hud] subprocess exited rc=%s", rc)
                # If we're still the active proc (i.e. quit() didn't
                # clear us first), attempt respawn.
                if self._proc is proc and not self._given_up:
                    self._ensure_respawn_scheduled()

    def _ensure_respawn_scheduled(self) -> None:
        """Schedule a respawn unless one is already pending.

        The ONLY entry point to the respawn ladder — both the reader's
        crash detection and _send_async's dead-subprocess branch route
        through here, so the 5/15/60s backoff can't be bypassed and two
        callers can't double-spawn. Must be called from the loop thread
        (both call sites are).
        """
        if self._given_up:
            return
        task = self._respawn_task
        if task is not None and not task.done():
            return
        self._respawn_task = asyncio.create_task(
            self._respawn_after_crash(), name="hud-respawn",
        )

    async def _heartbeat_loop(self) -> None:
        """Detect an alive-but-hung HUD (Qt loop deadlock, GPU hang).

        Sends a ``ping`` every ``heartbeat_secs``; the child replies
        ``pong`` from its GUI thread (proving the Qt event loop is
        alive — renderer death is covered separately by the
        renderProcessTerminated handler in window.py). After
        ``_MISSED_PONGS_BEFORE_KILL`` consecutive unanswered pings the
        subprocess is killed, which surfaces as a normal crash to the
        reader → respawn ladder. Paused while the subprocess is down
        (the ladder owns that state) and during cold boot (no
        ``hud_ready`` yet — the child-side ready watchdog owns that
        window).
        """
        try:
            while True:
                await asyncio.sleep(self._heartbeat_secs)
                if self._given_up:
                    return
                proc = self._proc
                if proc is None or proc.returncode is not None:
                    continue
                if not self._ready_event.is_set():
                    continue
                if self._outstanding_pings >= _MISSED_PONGS_BEFORE_KILL:
                    log.error(
                        "[hud] heartbeat: %d consecutive missed pongs — "
                        "killing hung subprocess for respawn",
                        self._outstanding_pings,
                    )
                    self._outstanding_pings = 0
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    continue
                self._outstanding_pings += 1
                self._ping_seq += 1
                self._send_threadsafe(
                    {"cmd": Cmd.PING, "id": f"ping-{self._ping_seq}"}
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("[hud] heartbeat loop crashed", exc_info=True)

    def _dispatch_event(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        event = payload.get("event")
        if event == Evt.HUD_READY:
            self._ready_event.set()
            self._outstanding_pings = 0
            log.info("[hud] subprocess emitted hud_ready")
            if self._reported_degraded:
                # A ready HUD after a give-up + reset_given_up cycle —
                # tell the tray the degraded banner can come down.
                self._reported_degraded = False
                self._fire_health(True)
            if self._last_pill_params is not None:
                # The HUD process (or its renderer — a reload re-emits
                # hud_ready too) restarted while a session pill was
                # active: replay it so the recording indicator doesn't
                # silently vanish mid-meeting. show_pill mints a fresh
                # paint_id; drop the stale one first so
                # _pending_show_times can't accumulate an orphan entry
                # per respawn.
                params = self._last_pill_params
                old_paint_id = params.get("paint_id")
                if isinstance(old_paint_id, str):
                    self._clear_pending_show_tracking(old_paint_id)
                log.info("[hud] hud_ready with active pill — replaying show_pill")
                self.show_pill(
                    reason=params["reason"],
                    reason_label=params["reason_label"],
                    start_ts=params["start_ts"],
                    hotkey=params.get("hotkey", ""),
                )
            return
        if event == Evt.PONG:
            self._outstanding_pings = 0
            return
        if event == Evt.CARD_RESPONSE:
            req_id = payload.get("request_id")
            answer = payload.get("answer")
            entry = self._pending_cards.pop(req_id, None) if req_id else None
            self._clear_pending_show_tracking(req_id)
            if entry is not None:
                fut, _default = entry
                if not fut.done():
                    fut.set_result(
                        answer if answer in ("yes", "no", "timeout") else "timeout"
                    )
            return
        if event == Evt.CARD_PAINTED:
            req_id = payload.get("request_id")
            if not isinstance(req_id, str):
                return
            started = self._pending_show_times.pop(req_id, None)
            if started is None:
                # Late paint after we already cleared (response /
                # supersede / hide_card landed first). Not an error.
                return
            delta_ms = (time.monotonic() - started) * 1000.0
            log.info(
                "[notify] card_painted: request_id=%s delta_ms=%.0f",
                req_id, delta_ms,
            )
            return
        if event in (Evt.ACTIONABLE_RESPONSE, Evt.INSIGHT_RESPONSE):
            # Actionable toasts (capture-saved) and insight cards (post-capture
            # coaching) share the same callback map + dispatch: both carry
            # on_pressed / on_secondary / on_expire keyed by request_id, and
            # the request_id prefixes ("actionable-" / "insight-") never
            # collide. Reusing the map means the give-up path
            # (_fail_pending_consents) already fires on_expire for both.
            req_id = payload.get("request_id")
            outcome = payload.get("outcome")
            entry = self._pending_actionables.pop(req_id, None) if req_id else None
            self._clear_pending_show_tracking(req_id)
            if entry is None:
                return
            # outcome ∈ {"pressed", "expired", "snoozed"}. "snoozed" routes
            # to the optional secondary callback; an unknown / missing
            # outcome falls through to on_expire (safe default — treat as
            # "user didn't take the action").
            if outcome == "pressed":
                cb = entry["on_pressed"]
            elif outcome == "snoozed":
                cb = entry.get("on_secondary")
            else:
                cb = entry["on_expire"]
            if cb is None:
                return
            try:
                cb()
            except Exception:
                log.warning(
                    "[hud] actionable callback raised (outcome=%s)", outcome, exc_info=True
                )
            return
        if event == Evt.PILL_STOP_CLICKED:
            if self._on_pill_stop is not None:
                try:
                    self._on_pill_stop()
                except Exception:
                    log.warning("[hud] pill_stop callback raised", exc_info=True)
            return
        if event in (Evt.PILL_COLLAPSED, Evt.PILL_EXPANDED):
            if self._on_pill_collapsed is not None:
                try:
                    self._on_pill_collapsed(event == Evt.PILL_COLLAPSED)
                except Exception:
                    log.warning("[hud] pill_collapsed callback raised", exc_info=True)
            return
        if event == Evt.LOG:
            level = payload.get("level", "info")
            msg = payload.get("msg", "")
            getattr(log, level if level in ("info", "warning", "error", "debug") else "info")(
                "[hud-js] %s", msg,
            )
            return

    async def _respawn_after_crash(self) -> None:
        """Spawn a fresh HUD subprocess with bounded backoff."""
        now = time.monotonic()
        if now - self._respawn_window_started > _RESPAWN_WINDOW_SECS:
            self._respawn_count = 0
            self._respawn_window_started = now
        if self._respawn_count >= _MAX_RESPAWNS:
            self._given_up = True
            log.error(
                "[hud] giving up after %d respawns in %.1fs — notifications "
                "will be silent for the rest of this session",
                self._respawn_count, _RESPAWN_WINDOW_SECS,
            )
            self._fail_pending_consents()
            self._reported_degraded = True
            self._fire_health(False)
            return
        delay = _RESPAWN_DELAYS[self._respawn_count]
        self._respawn_count += 1
        log.warning(
            "[hud] respawning subprocess (attempt %d/%d, backoff %.0fs)",
            self._respawn_count, _MAX_RESPAWNS, delay,
        )
        await asyncio.sleep(delay)
        async with self._lock:
            if self._given_up:
                return
            await self._spawn_locked()

    def _fail_pending_consents(self) -> None:
        """Resolve every outstanding card/actionable future as timeout."""
        for fut, default in list(self._pending_cards.values()):
            if not fut.done():
                # Use each caller's own registered default — same
                # rationale as the supersede path in :meth:`ask_consent`.
                # Guarded against InvalidStateError for the same reason:
                # the dispatcher thread may resolve the future between
                # ``done()`` and ``set_result``.
                try:
                    fut.set_result(default)
                except InvalidStateError:
                    pass
        self._pending_cards.clear()
        for entry in list(self._pending_actionables.values()):
            cb = entry.get("on_expire")
            if cb is not None:
                try:
                    cb()
                except Exception:
                    pass
        self._pending_actionables.clear()
        self._pending_show_times.clear()

    def _clear_pending_show_tracking(self, request_id: Optional[str]) -> None:
        """Pop the schedule-time entry for ``request_id`` if present.

        Called from every termination path — response received, paint
        delta logged, hide_card sent, supersede — so
        ``_pending_show_times`` cannot grow unbounded over a long
        session of cards and toasts.
        """
        if not request_id:
            return
        self._pending_show_times.pop(request_id, None)

    # ------------------------------------------------------------------
    # Health reporting + recovery from the give-up state.
    # ------------------------------------------------------------------

    def set_health_callback(self, cb: Optional[Callable[[bool], None]]) -> None:
        """Register a callback fired on degraded (False) / recovered (True).

        Used by the tray to surface a "Notifications unavailable" line
        when the respawn ladder gives up, and clear it when a later
        ``reset_given_up`` brings the HUD back. Fired on the loop thread.
        """
        self._health_cb = cb

    def _fire_health(self, ok: bool) -> None:
        cb = self._health_cb
        if cb is None:
            return
        try:
            cb(ok)
        except Exception:
            log.warning("[hud] health callback raised (ok=%s)", ok, exc_info=True)

    def reset_given_up(self) -> None:
        """Clear the give-up state and respawn, in response to user action.

        Called from the arm path (any explicit arm is a fresh, most-
        recent user signal that they expect Sayzo to work). No-op when
        not given up. Thread-safe — marshals ``start()`` onto the loop.
        """
        if not self._given_up:
            return
        log.warning("[hud] give-up reset by user action — respawning")
        self._given_up = False
        self._respawn_count = 0
        self._respawn_window_started = 0.0
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self.start(), loop)
            except Exception:
                log.warning("[hud] reset_given_up: start() schedule failed", exc_info=True)

    # ------------------------------------------------------------------
    # Public command surface.
    # ------------------------------------------------------------------

    def _send_threadsafe(self, payload: dict[str, Any]) -> bool:
        """Schedule a write to the subprocess from any thread.

        Returns ``True`` on schedule success, ``False`` if no loop or no
        subprocess is alive. The write itself is best-effort; failures
        log and trigger a respawn rather than raising.
        """
        if self._given_up:
            return False
        loop = self._loop
        if loop is None:
            log.debug("[hud] _send: no loop yet — dropping payload")
            return False
        try:
            asyncio.run_coroutine_threadsafe(self._send_async(payload), loop)
            return True
        except Exception:
            log.warning("[hud] _send schedule failed", exc_info=True)
            return False

    async def _send_async(self, payload: dict[str, Any]) -> None:
        try:
            line = json.dumps(payload).encode("utf-8") + b"\n"
        except (TypeError, ValueError):
            log.warning("[hud] _send: payload not JSON-serialisable: %r", payload)
            return
        async with self._lock:
            if self._proc is None or self._proc.returncode is not None:
                # Don't spawn inline — that bypassed the backoff ladder
                # and could double-spawn against a pending
                # _respawn_after_crash (pre-v3.14 bug). Drop the payload
                # (logged), make sure the ladder is running, and let
                # state replay (pill via hud_ready, consents via their
                # waiter timeouts) cover what was dropped.
                if not self._given_up:
                    # Throttle: a HUD death mid-session drops one payload per
                    # high-frequency pill-frame, which produced a 53-line burst
                    # in 5 s. Log the FIRST drop of a burst immediately, then a
                    # running summary every interval. The counter is reset on
                    # the next successful send (recovery, below), so each new
                    # down-burst logs its first drop right away.
                    self._down_drop_count += 1
                    now = time.monotonic()
                    if (
                        self._down_drop_count == 1
                        or now - self._down_drop_last_log
                        >= _DOWN_DROP_LOG_INTERVAL_SECS
                    ):
                        log.info(
                            "[hud] _send: subprocess down — dropped %d "
                            "payload(s), respawn scheduled",
                            self._down_drop_count,
                        )
                        self._down_drop_last_log = now
                    self._ensure_respawn_scheduled()
                return
            proc = self._proc
            if proc is None or proc.stdin is None:
                return
            try:
                proc.stdin.write(line)
                await proc.stdin.drain()
                # Successful send → HUD is up. Reset the down-drop counter so
                # the next down-burst logs its first drop immediately rather
                # than folding into a stale count from an earlier incident.
                self._down_drop_count = 0
            except (BrokenPipeError, ConnectionResetError):
                log.info("[hud] pipe broken — letting reader handle respawn")
            except Exception:
                log.warning("[hud] write to stdin failed", exc_info=True)

    # --- info toast (fire-and-forget) ---------------------------------

    def _show_toast_impl(
        self, title: str, body: str, ttl_secs: float,
    ) -> Optional[str]:
        """Send a SHOW_TOAST and return its toast_id, or None if dropped.

        Shared by :meth:`show_toast` (bool contract) and
        :meth:`show_toast_before_quit` (needs the id to await its
        paint). The ``[notify]`` log lines are emitted here so both
        callers produce byte-identical triage output.
        """
        if self._given_up:
            return None
        toast_id = f"toast-{uuid.uuid4().hex}"
        self._pending_show_times[toast_id] = time.monotonic()
        log.info("[notify] notify scheduled: title=%r", title)
        ok = self._send_threadsafe({
            "cmd": Cmd.SHOW_TOAST,
            "id": toast_id,
            "title": title,
            "body": body,
            "ttl_secs": float(ttl_secs),
        })
        if not ok:
            self._clear_pending_show_tracking(toast_id)
            log.warning("[notify] notify dropped (HUD unavailable): title=%r", title)
            return None
        return toast_id

    def show_toast(self, title: str, body: str, ttl_secs: float = 4.0) -> bool:
        return self._show_toast_impl(title, body, ttl_secs) is not None

    def show_toast_before_quit(
        self, title: str, body: str, ttl_secs: float = 2.0,
    ) -> bool:
        """Show a toast and arm :meth:`quit` to wait for it to paint + finish.

        The install-update path uses this for the "Sayzo is updating"
        toast: without the grace window in ``quit`` the agent tears the
        HUD down before the toast's first frame composites, so the user
        sees everything vanish (the "agent just disappeared" perception)
        instead of a reassurance that an update is in progress. ``quit``
        also lingers for ``ttl_secs`` after paint so the toast's countdown
        bar runs to 0% rather than freezing mid-fill on teardown.
        """
        toast_id = self._show_toast_impl(title, body, ttl_secs)
        if toast_id is None:
            return False
        self._quit_grace_toast_id = toast_id
        self._quit_grace_toast_ttl = float(ttl_secs)
        self._quit_grace_toast_shown_at = time.monotonic()
        return True

    # --- consent card (blocking yes/no) -------------------------------

    def ask_consent(
        self,
        title: str,
        body: str,
        yes_label: str,
        no_label: str,
        timeout_secs: float,
        default_on_timeout: ConsentResult = "no",
        supersede: bool = False,
    ) -> ConsentResult:
        """Show a consent card and block until the user answers or it times out.

        Synchronous — must NOT be called from the asyncio loop thread.
        The legacy ``DesktopNotifier.ask_consent`` had the same constraint.

        ``supersede`` controls what happens when another consent is
        already on screen. ``False`` (default) queues the new card
        behind the active one in React's FIFO. ``True`` dismisses every
        pending consent first, resolving each prior future with its
        OWN registered ``default_on_timeout`` and sending HIDE_CARD to
        React. Opt-in because some callers (``pending_close`` with
        ``default='yes'`` = commit_close; ``meeting_ended`` with
        ``default='yes'`` = wrap up) have side-effecting defaults that
        would silently fire if any unrelated new consent superseded
        them. Only the hotkey path opts in — pressing the hotkey is
        always the user's most recent and most explicit signal.
        """
        if self._given_up:
            log.warning(
                "[notify] ask_consent dropped (HUD given up): title=%r", title,
            )
            return default_on_timeout
        if supersede:
            # Supersede any in-flight consent before showing the new one.
            # Walks a snapshot via ``list(...)`` so a concurrent
            # ``card_response`` from the reader thread can't mutate the
            # dict mid-iteration. ``set_result`` is guarded by
            # try/except InvalidStateError because the reader thread can
            # resolve the future between our ``done()`` check and our
            # ``set_result`` call — without the guard that race
            # propagates an exception out of ``ask_consent`` and the new
            # caller never sees its SHOW_CARD fire.
            for old_request_id, (old_fut, old_default) in list(self._pending_cards.items()):
                if not old_fut.done():
                    try:
                        old_fut.set_result(old_default)
                    except InvalidStateError:
                        pass
                self._pending_cards.pop(old_request_id, None)
                self._clear_pending_show_tracking(old_request_id)
                self._send_threadsafe({
                    "cmd": Cmd.HIDE_CARD,
                    "request_id": old_request_id,
                })
                log.info(
                    "[notify] ask_consent: superseding prior request_id=%s",
                    old_request_id,
                )
        request_id = f"card-{uuid.uuid4().hex}"
        fut: Future = Future()
        self._pending_cards[request_id] = (fut, default_on_timeout)
        self._pending_show_times[request_id] = time.monotonic()
        log.info(
            "[notify] ask scheduled: title=%r yes=%r no=%r timeout=%ss",
            title, yes_label, no_label, timeout_secs,
        )
        ok = self._send_threadsafe({
            "cmd": Cmd.SHOW_CARD,
            "request_id": request_id,
            "title": title,
            "body": body,
            "yes_label": yes_label,
            "no_label": no_label,
            "timeout_secs": float(timeout_secs),
        })
        if not ok:
            self._pending_cards.pop(request_id, None)
            self._clear_pending_show_tracking(request_id)
            return default_on_timeout
        try:
            # Add a small grace margin on top of the React-side timeout
            # so we don't race the HUD's own timeout->response message.
            result = fut.result(timeout=timeout_secs + 3.0)
            answer: ConsentResult = (
                result if result in ("yes", "no", "timeout") else "timeout"
            )
            log.info("[notify] ask resolved: title=%r → %s", title, answer)
            return answer
        except Exception as exc:
            # A consent timeout is EXPECTED (user didn't answer in the
            # window) — log a one-liner, not a scary stack trace. Only
            # genuinely unexpected waiter failures get a traceback.
            if isinstance(exc, FuturesTimeout):
                log.info(
                    "[notify] ask_consent timed out (%.0fs) — title=%r → %s",
                    timeout_secs, title, default_on_timeout,
                )
            else:
                log.warning("[notify] ask_consent waiter raised", exc_info=True)
            self._pending_cards.pop(request_id, None)
            self._clear_pending_show_tracking(request_id)
            # Tell React to hide the card so it doesn't linger past
            # the Python-side timeout — pre-v3.11 the card sat on
            # screen until React's own timeout fired, which made the
            # next consent stack on top of a stale one.
            self._send_threadsafe({
                "cmd": Cmd.HIDE_CARD,
                "request_id": request_id,
            })
            return default_on_timeout

    def ask_consent_pausing_pill(
        self,
        title: str,
        body: str,
        yes_label: str,
        no_label: str,
        timeout_secs: float,
        default_on_timeout: ConsentResult = "no",
        supersede: bool = False,
    ) -> ConsentResult:
        """Sync ``ask_consent`` wrapper that hides the pill for the duration.

        Mirror of the pattern ``ArmController`` previously implemented
        inline as ``_ask_consent_pausing_pill``: snapshot the active
        pill, send hide-pill, ask for consent, restore the pill on
        return iff the caller hasn't cleared it (e.g. via an
        intervening ``hide_pill()``) in the meantime. Same sync
        contract as ``ask_consent`` — must NOT be called from an
        asyncio loop thread without ``run_in_executor``.

        ``_last_pill_params`` is the single source of truth for "is
        there a pill to restore"; both callers (ArmController via
        executor, ``preview_hud.py``) read it instead of maintaining
        their own bookkeeping dict.
        """
        snapshot = self._last_pill_params
        if snapshot is not None:
            # Send the IPC hide WITHOUT clearing ``_last_pill_params``
            # so the restore branch can detect if the caller did its
            # own explicit ``hide_pill()`` during the await (which
            # clears the field).
            self._send_threadsafe({"cmd": Cmd.HIDE_PILL})
        try:
            return self.ask_consent(
                title, body, yes_label, no_label,
                timeout_secs, default_on_timeout,
                supersede=supersede,
            )
        finally:
            if snapshot is not None and self._last_pill_params is not None:
                self._send_threadsafe({"cmd": Cmd.SHOW_PILL, **snapshot})

    # --- actionable toast (capture-saved) ------------------------------

    def show_actionable(
        self,
        title: str,
        body: str,
        *,
        button_label: str,
        on_pressed: Callable[[], None],
        expire_after_secs: float,
        on_expire: Optional[Callable[[], None]] = None,
        secondary_button_label: Optional[str] = None,
        on_secondary_pressed: Optional[Callable[[], None]] = None,
    ) -> bool:
        if self._given_up:
            log.warning(
                "[notify] notify_actionable dropped (HUD given up): title=%r", title,
            )
            return False
        request_id = f"actionable-{uuid.uuid4().hex}"
        log.info(
            "[notify] actionable scheduled: title=%r button=%r secondary=%r expire_after=%ss",
            title, button_label, secondary_button_label, expire_after_secs,
        )
        self._pending_actionables[request_id] = {
            "on_pressed": on_pressed,
            "on_expire": on_expire,
            "on_secondary": on_secondary_pressed,
        }
        self._pending_show_times[request_id] = time.monotonic()
        cmd: dict[str, Any] = {
            "cmd": Cmd.SHOW_ACTIONABLE,
            "request_id": request_id,
            "title": title,
            "body": body,
            "button_label": button_label,
            "expire_after_secs": float(expire_after_secs),
        }
        # Only carry the secondary button when present so single-button
        # actionables stay byte-identical to the pre-v3.8.x command shape.
        if secondary_button_label is not None:
            cmd["secondary_button_label"] = secondary_button_label
        ok = self._send_threadsafe(cmd)
        if not ok:
            self._pending_actionables.pop(request_id, None)
            self._clear_pending_show_tracking(request_id)
        return ok

    # --- insight card (post-capture coaching) -------------------------

    def show_insight(
        self,
        *,
        headline: str,
        body: str,
        source_label: str,
        freshness_label: str,
        button_label: str,
        on_pressed: Callable[[], None],
        expire_after_secs: float,
        quote: Optional[str] = None,
        insight_type: Optional[str] = None,
        on_expire: Optional[Callable[[], None]] = None,
        secondary_button_label: Optional[str] = None,
        on_secondary_pressed: Optional[Callable[[], None]] = None,
    ) -> bool:
        """Show the compact post-capture coaching card (v3.10+).

        Mirrors :meth:`show_actionable`'s plumbing — callbacks stored in the
        shared ``_pending_actionables`` map, dispatched on
        ``insight_response``. ``on_pressed`` opens the capture deep-link;
        ``on_secondary_pressed`` is the "Stop showing these" off-switch.
        ``freshness_label`` populates the chip ("Just now" / "5 min ago" /
        "1 hr ago") — computed at fire time so deferred fires don't lie.
        """
        if self._given_up:
            log.warning(
                "[notify] notify_insight dropped (HUD given up): headline=%r",
                headline,
            )
            return False
        request_id = f"insight-{uuid.uuid4().hex}"
        log.info(
            "[notify] insight scheduled: headline=%r type=%r has_quote=%s freshness=%r expire_after=%ss",
            headline, insight_type, bool(quote), freshness_label, expire_after_secs,
        )
        self._pending_actionables[request_id] = {
            "on_pressed": on_pressed,
            "on_expire": on_expire,
            "on_secondary": on_secondary_pressed,
        }
        self._pending_show_times[request_id] = time.monotonic()
        cmd: dict[str, Any] = {
            "cmd": Cmd.SHOW_INSIGHT,
            "request_id": request_id,
            "headline": headline,
            "body": body,
            "source_label": source_label,
            "freshness_label": freshness_label,
            "button_label": button_label,
            "expire_after_secs": float(expire_after_secs),
        }
        if quote:
            cmd["quote"] = quote
        if insight_type:
            cmd["insight_type"] = insight_type
        if secondary_button_label is not None:
            cmd["secondary_button_label"] = secondary_button_label
        ok = self._send_threadsafe(cmd)
        if not ok:
            self._pending_actionables.pop(request_id, None)
            self._clear_pending_show_tracking(request_id)
        return ok

    # --- persistent pill (arm state indicator) ------------------------

    def show_pill(
        self,
        *,
        reason: ReasonKind,
        reason_label: str,
        start_ts: Optional[float] = None,
        hotkey: str = "",
    ) -> bool:
        if self._given_up:
            return False
        if start_ts is None:
            start_ts = time.time()
        # Generate a per-show paint_id so the React StatePill component
        # can emit ``card_painted`` on mount and we can log the
        # show_pill → first-paint delta_ms. Same diagnostic plumbing
        # as cards/toasts/insights/actionables — the pill is the
        # FIRST content shown on a cold disarmed→armed transition,
        # exactly the case where the layered-window paint-stall is
        # most likely to fire, so we want symmetric coverage.
        paint_id = f"pill-{uuid.uuid4().hex}"
        params = {
            "reason": reason,
            "reason_label": reason_label,
            "start_ts": float(start_ts),
            "hotkey": hotkey,
            "paint_id": paint_id,
        }
        self._last_pill_params = params
        self._pending_show_times[paint_id] = time.monotonic()
        ok = self._send_threadsafe({"cmd": Cmd.SHOW_PILL, **params})
        if not ok:
            self._clear_pending_show_tracking(paint_id)
        return ok

    def hide_pill(self) -> bool:
        if self._given_up:
            return False
        # Clear the pill's paint_id from _pending_show_times BEFORE
        # forgetting _last_pill_params — otherwise if the React side
        # never emitted card_painted (rapid arm/disarm before the
        # mount-rAF fires, paint stall, subprocess crash) the entry
        # leaks until _fail_pending_consents runs on respawn.
        if self._last_pill_params is not None:
            paint_id = self._last_pill_params.get("paint_id")
            if isinstance(paint_id, str):
                self._clear_pending_show_tracking(paint_id)
        self._last_pill_params = None
        return self._send_threadsafe({"cmd": Cmd.HIDE_PILL})

    def hide_all(self) -> bool:
        """Clear every visible HUD element (pill, cards, toasts, actionable).

        Public counterpart to the ``hide_all`` JSON command. Used by
        the preview-HUD test script's "hide all" menu option; the
        production agent reaches the same state by going DISARMED.
        """
        if self._given_up:
            return False
        return self._send_threadsafe({"cmd": Cmd.HIDE_ALL})

    def set_pill_collapsed(self, collapsed: bool) -> bool:
        if self._given_up:
            return False
        return self._send_threadsafe({
            "cmd": Cmd.SET_PILL_COLLAPSED,
            "collapsed": bool(collapsed),
        })

    def set_audio_levels(self, mic: float, system: float) -> bool:
        """Push the latest mic + system audio amplitude to the pill.

        Drives the waveform indicator on `StatePill` so the user can see
        Sayzo is actually hearing audio (not just running). Best fired
        at ~10–20 Hz from the agent's capture pipeline while armed — any
        faster wastes pipe bandwidth, any slower starts to feel laggy.

        Values are per-source NORMALIZED levels in [0, 1] (the agent's
        ``Agent._consume`` divides raw RMS by a slow-decaying peak per
        source). 0 ≈ silence, 1 ≈ current peak, so quiet and loud mics
        both fill the bars during speech. The HUD applies a dB-shape
        scale on top for perceptual feel.
        """
        if self._given_up:
            return False
        return self._send_threadsafe({
            "cmd": Cmd.SET_AUDIO_LEVELS,
            "mic": float(mic),
            "system": float(system),
        })

    def set_pill_stop_callback(self, cb: Optional[Callable[[], None]]) -> None:
        self._on_pill_stop = cb

    def set_pill_collapsed_callback(self, cb: Optional[Callable[[bool], None]]) -> None:
        self._on_pill_collapsed = cb

    # --- diagnostics --------------------------------------------------

    def is_alive(self) -> bool:
        return (
            not self._given_up
            and self._proc is not None
            and self._proc.returncode is None
        )

    def diagnose(self) -> dict[str, Any]:
        return {
            "platform": sys.platform,
            "frozen": getattr(sys, "frozen", False),
            "alive": self.is_alive(),
            "ready": self._ready_event.is_set(),
            "given_up": self._given_up,
            "respawn_count": self._respawn_count,
            "pending_cards": len(self._pending_cards),
            "pending_actionables": len(self._pending_actionables),
            "heartbeat_secs": self._heartbeat_secs,
            "outstanding_pings": self._outstanding_pings,
            "proc_pid": self._proc.pid if self._proc is not None else None,
            "returncode": self._proc.returncode if self._proc is not None else None,
        }
