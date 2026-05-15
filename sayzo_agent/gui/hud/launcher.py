"""Parent-side HUD subprocess manager.

Owns the lifecycle of ``sayzo-agent hud --idle``: spawn, talk to it via
stdin / stdout, respawn on crash with a bounded retry ladder, quit on
agent shutdown. Exposes a synchronous API that mirrors the legacy
``notify.py`` surface — ``notify``, ``ask_consent``, ``notify_actionable``
— plus HUD-specific pill controls (``show_pill``, ``hide_pill``,
``set_pill_collapsed``).

Threading model: the launcher's public methods are safe to call from any
thread. Writes to the subprocess's stdin are serialized through a
``threading.Lock``; the stdout reader runs on a dedicated daemon thread
and resolves per-request futures. ``ask_consent`` blocks the caller's
thread on a ``concurrent.futures.Future``; the caller must not be on the
asyncio loop that will need to schedule other work — same constraint as
the legacy ``DesktopNotifier.ask_consent``.

Failure modes (see ``so-turns-out-granola-memoized-riddle.md`` for the
full design):

* Stdin pipe broken or subprocess crashed → respawn with 5 s / 15 s /
  60 s backoff. After 3 crashes in 60 s, give up for the rest of the
  session — every public method becomes a no-op that returns
  ``default_on_timeout``.
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
import threading
import time
import uuid
from concurrent.futures import Future
from pathlib import Path
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
    HIDE_ALL = "hide_all"
    DEMO_MODE = "demo_mode"
    QUIT = "quit"


class Evt:
    HUD_READY = "hud_ready"
    CARD_RESPONSE = "card_response"
    ACTIONABLE_RESPONSE = "actionable_response"
    PILL_STOP_CLICKED = "pill_stop_clicked"
    PILL_COLLAPSED = "pill_collapsed"
    PILL_EXPANDED = "pill_expanded"
    LOG = "log"


_RESPAWN_DELAYS = (5.0, 15.0, 60.0)
_RESPAWN_WINDOW_SECS = 120.0
_MAX_RESPAWNS = len(_RESPAWN_DELAYS)


def _hud_subprocess_argv() -> list[str]:
    """``argv`` for spawning ``sayzo-agent hud --idle``.

    Frozen builds use the single bundled binary; dev runs use
    ``python -m sayzo_agent hud`` so the entry point resolves without
    relying on the ``sayzo-agent`` console-script being on PATH.

    macOS Helper.app pattern (v3.2.0)
    ---------------------------------
    On macOS frozen builds, we route through the nested ``SayzoHud.app``
    helper bundle's wrapper binary (``installer/macos/sayzo_hud_wrapper.c``,
    bundled at ``Sayzo.app/Contents/Frameworks/SayzoHud.app/Contents/MacOS/SayzoHud``).
    The wrapper does ``posix_spawn`` of the real ``sayzo-agent hud --idle``
    and waits, inheriting stdin/stdout pipes from this launcher.

    Why: macOS LaunchServices treats child processes spawned directly by
    a process whose binary is in the SAME ``.app`` bundle as "internal
    helpers" of the already-registered parent app — refuses to grant
    them their own ASN / CGS (window server) connection. No CGS → no
    rendering. By inserting the wrapper (whose binary is in
    ``SayzoHud.app`` with ``CFBundleIdentifier=com.sayzo.agent.hud``,
    NOT ``com.sayzo.agent``), the spawned HUD's parent has a different
    bundle ID. LaunchServices then registers the HUD as an independent
    ``com.sayzo.agent`` instance with its own ASN + CGS connection.

    Diagnosed 2026-05-15 via ``scripts/probe_macos_hud_proc_state.py``.
    Validated 2026-05-16 via ``scripts/validate_helper_app.sh`` —
    HUD spawned via wrapper showed ``bundleID="com.sayzo.agent"`` and
    proper ASN, vs. ``bundleID=[NULL] !cgsConnection`` for direct spawn.
    See ``installer/macos/sayzo_hud_wrapper.c`` for full rationale.

    Dev (non-frozen) runs on macOS bypass the wrapper since the helper
    bundle isn't built. The bug only manifests when the parent agent
    is itself in a fully-registered ``.app`` bundle, which doesn't
    happen in dev.
    """
    if not getattr(sys, "frozen", False):
        return [sys.executable, "-m", "sayzo_agent", "hud"]

    if sys.platform == "darwin":
        # Frozen macOS: route through the helper bundle's wrapper.
        # sys.executable points at Sayzo.app/Contents/MacOS/sayzo-agent;
        # the helper wrapper lives at Sayzo.app/Contents/Frameworks/
        # SayzoHud.app/Contents/MacOS/SayzoHud — i.e. one level up
        # from Contents/MacOS/, then into Frameworks/SayzoHud.app/...
        contents_dir = Path(sys.executable).parent.parent
        wrapper = (
            contents_dir / "Frameworks" / "SayzoHud.app" /
            "Contents" / "MacOS" / "SayzoHud"
        )
        if wrapper.exists():
            # Wrapper expects: SayzoHud <target_binary> [target_args...]
            # The target is the real sayzo-agent binary in `hud --idle` mode.
            return [str(wrapper), sys.executable, "hud"]
        # Defensive fallback: if the helper bundle isn't present (older
        # build, manual install corruption), spawn the binary directly
        # — HUD will be invisible per the v3.1.x bug, but the agent
        # won't crash. Logged loudly so we notice in the field.
        log.warning(
            "[hud] SayzoHud helper bundle not found at %s — falling back "
            "to direct spawn (HUD will be invisible on Finder-launched agent)",
            wrapper,
        )
        return [sys.executable, "hud"]

    # Frozen Windows / Linux: same as before, no wrapper needed (Windows
    # has no LaunchServices and renders fine; Linux build path doesn't
    # exist yet).
    return [sys.executable, "hud"]


def _hud_subprocess_env() -> dict[str, str]:
    """Env for the HUD subprocess.

    macOS LaunchServices identity overrides (v3.2.1)
    ------------------------------------------------
    On macOS we override three env vars before posix_spawn so
    LaunchServices registers the HUD as a separate app instance
    backed by ``SayzoHud.app`` (built by the v3.2.0 Helper.app
    pattern), not as an internal helper of the running agent:

      * ``__CFBundleIdentifier`` ← ``com.sayzo.agent.hud``
        Tells Cocoa's NSBundle/LaunchServices "I am the helper
        bundle." LSRegister validates this against the disk-resident
        ``Sayzo.app/Contents/Frameworks/SayzoHud.app`` (which DOES
        have that ID), accepts the override, and registers us as a
        fresh com.sayzo.agent.hud instance — own ASN, own CGS
        connection, windows render.
      * ``XPC_SERVICE_NAME`` ← popped
        Otherwise the child inherits the agent's XPC slot
        (``application.com.sayzo.agent.<numbers>``), which marks the
        process as part of the agent's XPC service and triggers the
        same "internal helper, no registration" path even with a
        different __CFBundleIdentifier.
      * ``XPC_FLAGS`` ← popped (paranoia — usually 0x0, but no
        reason to inherit any XPC config from the parent).

    Why this works in v3.2.1 but the v3.1.7 env hack didn't
    --------------------------------------------------------
    v3.1.7 set ``__CFBundleIdentifier=com.apple.Terminal`` to "spoof
    Terminal." The bundle ID Terminal.app lives at
    ``/System/Applications/Utilities/Terminal.app`` — completely
    different binary tree from our HUD's ``sayzo-agent`` binary at
    ``/Applications/Sayzo.app/Contents/MacOS/sayzo-agent``.
    LaunchServices apparently treated that as a bogus override and
    fell through to the same "internal helper" classification.

    In v3.2.1 the override is ``com.sayzo.agent.hud`` and that
    bundle ID IS valid on disk (``SayzoHud.app`` shipped with
    v3.2.0). So LSRegister accepts it. The Helper.app bundle from
    v3.2.0 was necessary scaffolding — without a real
    ``com.sayzo.agent.hud`` bundle on disk, LSRegister has nothing
    to validate the env var against.

    Diagnosed 2026-05-16 via ``ps eww $HUD_PID`` + ``lsappinfo info
    $HUD_PID`` against a v3.2.0 production install. Output:
    ``__CFBundleIdentifier=com.sayzo.agent`` inherited (wrapper is
    transparent for env), ``bundleID=[NULL]`` + ``!cgsConnection``
    in lsappinfo (LS classified as internal helper of running
    com.sayzo.agent agent). See
    ``project_macos_hud_helper_app_v3_2_0`` memory + the v3.2.1
    follow-up note for the full fix history.

    Dev (non-frozen) runs and Windows return the env unchanged.
    """
    env = dict(os.environ)
    if sys.platform == "darwin" and getattr(sys, "frozen", False):
        env["__CFBundleIdentifier"] = "com.sayzo.agent.hud"
        env.pop("XPC_SERVICE_NAME", None)
        env.pop("XPC_FLAGS", None)
    return env


class HudLauncher:
    """Manage the HUD subprocess + dispatch its event stream."""

    def __init__(self) -> None:
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._lock = asyncio.Lock()
        # Per-request future maps, keyed by ``request_id``. Resolved by
        # the stdout reader when it sees the matching ``card_response``
        # or ``actionable_response`` event.
        self._pending_cards: dict[str, Future] = {}
        self._pending_actionables: dict[str, dict[str, Any]] = {}
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
        # Readiness — flipped when the subprocess writes ``hud_ready``.
        self._ready_event = asyncio.Event()
        self._reader_task: Optional[asyncio.Task] = None
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

    async def _spawn_locked(self) -> None:
        argv = _hud_subprocess_argv() + ["--idle"]
        log.info("[hud] spawning subprocess: %s", argv)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                # stderr inherits — HUD logs land in agent.log via the
                # standard logging config the CLI command installs.
                # env: scrub LaunchServices identity vars on macOS so
                # Chromium's renderer doesn't treat the HUD as a
                # background accessory-app window. See
                # ``_hud_subprocess_env`` for the full rationale.
                env=_hud_subprocess_env(),
            )
        except Exception:
            log.warning("[hud] subprocess spawn failed", exc_info=True)
            self._proc = None
            return
        self._ready_event.clear()
        self._reader_task = asyncio.create_task(
            self._stdout_reader_loop(self._proc),
            name="hud-stdout-reader",
        )

    async def wait_for_ready(self, timeout_secs: float = 15.0) -> bool:
        """Block until the subprocess emits ``hud_ready`` or timeout.

        Returns ``True`` on success, ``False`` on timeout. Callers that
        need the HUD to be visible before a high-stakes consent prompt
        should await this with a reasonable timeout. Callers issuing
        fire-and-forget toasts don't need to wait — the subprocess
        buffers commands that arrive before the React app mounts.
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
        """Send ``quit`` and wait for the subprocess to exit."""
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
                    pass

    # ------------------------------------------------------------------
    # Stdout reader.
    # ------------------------------------------------------------------

    async def _stdout_reader_loop(
        self, proc: asyncio.subprocess.Process,
    ) -> None:
        assert proc.stdout is not None
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
        except Exception:
            log.warning("[hud] stdout reader crashed", exc_info=True)
        finally:
            rc = proc.returncode
            log.info("[hud] subprocess exited rc=%s", rc)
            # If we're still the active proc (i.e. quit() didn't clear
            # us first), attempt respawn.
            if self._proc is proc and not self._given_up:
                asyncio.create_task(self._respawn_after_crash())

    def _dispatch_event(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        event = payload.get("event")
        if event == Evt.HUD_READY:
            self._ready_event.set()
            log.info("[hud] subprocess emitted hud_ready")
            return
        if event == Evt.CARD_RESPONSE:
            req_id = payload.get("request_id")
            answer = payload.get("answer")
            fut = self._pending_cards.pop(req_id, None) if req_id else None
            if fut is not None and not fut.done():
                fut.set_result(answer if answer in ("yes", "no", "timeout") else "timeout")
            return
        if event == Evt.ACTIONABLE_RESPONSE:
            req_id = payload.get("request_id")
            outcome = payload.get("outcome")
            entry = self._pending_actionables.pop(req_id, None) if req_id else None
            if entry is None:
                return
            cb = entry["on_pressed"] if outcome == "pressed" else entry["on_expire"]
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
        for fut in list(self._pending_cards.values()):
            if not fut.done():
                fut.set_result("timeout")
        self._pending_cards.clear()
        for entry in list(self._pending_actionables.values()):
            cb = entry.get("on_expire")
            if cb is not None:
                try:
                    cb()
                except Exception:
                    pass
        self._pending_actionables.clear()

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
                if self._given_up:
                    return
                await self._spawn_locked()
            proc = self._proc
            if proc is None or proc.stdin is None:
                return
            try:
                proc.stdin.write(line)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                log.info("[hud] pipe broken — letting reader handle respawn")
            except Exception:
                log.warning("[hud] write to stdin failed", exc_info=True)

    # --- info toast (fire-and-forget) ---------------------------------

    def show_toast(self, title: str, body: str, ttl_secs: float = 4.0) -> bool:
        if self._given_up:
            return False
        toast_id = f"toast-{uuid.uuid4().hex}"
        log.info("[notify] notify scheduled: title=%r", title)
        ok = self._send_threadsafe({
            "cmd": Cmd.SHOW_TOAST,
            "id": toast_id,
            "title": title,
            "body": body,
            "ttl_secs": float(ttl_secs),
        })
        if not ok:
            log.warning("[notify] notify dropped (HUD unavailable): title=%r", title)
        return ok

    # --- consent card (blocking yes/no) -------------------------------

    def ask_consent(
        self,
        title: str,
        body: str,
        yes_label: str,
        no_label: str,
        timeout_secs: float,
        default_on_timeout: ConsentResult = "no",
    ) -> ConsentResult:
        """Show a consent card and block until the user answers or it times out.

        Synchronous — must NOT be called from the asyncio loop thread.
        The legacy ``DesktopNotifier.ask_consent`` had the same constraint.
        """
        if self._given_up:
            log.warning(
                "[notify] ask_consent dropped (HUD given up): title=%r", title,
            )
            return default_on_timeout
        request_id = f"card-{uuid.uuid4().hex}"
        fut: Future = Future()
        self._pending_cards[request_id] = fut
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
        except Exception:
            log.warning("[notify] ask_consent waiter raised", exc_info=True)
            self._pending_cards.pop(request_id, None)
            return default_on_timeout

    def ask_consent_pausing_pill(
        self,
        title: str,
        body: str,
        yes_label: str,
        no_label: str,
        timeout_secs: float,
        default_on_timeout: ConsentResult = "no",
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
            )
        finally:
            if snapshot is not None and self._last_pill_params is not None:
                self._send_threadsafe({"cmd": Cmd.SHOW_PILL, **snapshot})

    # --- actionable toast (daily drill) -------------------------------

    def show_actionable(
        self,
        title: str,
        body: str,
        *,
        button_label: str,
        on_pressed: Callable[[], None],
        expire_after_secs: float,
        on_expire: Optional[Callable[[], None]] = None,
    ) -> bool:
        if self._given_up:
            log.warning(
                "[notify] notify_actionable dropped (HUD given up): title=%r", title,
            )
            return False
        request_id = f"actionable-{uuid.uuid4().hex}"
        log.info(
            "[notify] actionable scheduled: title=%r button=%r expire_after=%ss",
            title, button_label, expire_after_secs,
        )
        self._pending_actionables[request_id] = {
            "on_pressed": on_pressed,
            "on_expire": on_expire,
        }
        ok = self._send_threadsafe({
            "cmd": Cmd.SHOW_ACTIONABLE,
            "request_id": request_id,
            "title": title,
            "body": body,
            "button_label": button_label,
            "expire_after_secs": float(expire_after_secs),
        })
        if not ok:
            self._pending_actionables.pop(request_id, None)
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
        params = {
            "reason": reason,
            "reason_label": reason_label,
            "start_ts": float(start_ts),
            "hotkey": hotkey,
        }
        self._last_pill_params = params
        return self._send_threadsafe({"cmd": Cmd.SHOW_PILL, **params})

    def hide_pill(self) -> bool:
        if self._given_up:
            return False
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
            "proc_pid": self._proc.pid if self._proc is not None else None,
            "returncode": self._proc.returncode if self._proc is not None else None,
        }
