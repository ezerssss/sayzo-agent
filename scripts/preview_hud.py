"""Open the Sayzo HUD window standalone for visual review.

Usage:
    python scripts/preview_hud.py demo
    python scripts/preview_hud.py
    python scripts/preview_hud.py launcher

Modes:
  * ``demo`` (default) — launches the real frameless PySide6 HUD with
    the in-HUD demo control strip enabled. Click each button in the
    strip to fire the corresponding event type (pill, dot, consent
    card, info toast, actionable). The HUD is rendered top-right of
    the primary monitor with the same flags it uses in production.
    The demo strip itself keeps the host window visible — that's how
    you exercise the show/hide animation without losing access to the
    buttons. Clicking ``hide all`` fades everything out and hides the
    OS window entirely (you'll need to restart the script to bring
    it back, since there's no tray button in this preview).

  * ``launcher`` — spawns the HUD via the production ``HudLauncher``
    code path (same as the live agent uses) and presents an
    interactive menu so you can drive every event type and traverse
    every consent path by hand. Mirrors the production
    ``ArmController._ask_consent_pausing_pill`` behaviour: pill
    hides for the duration of any "are you still here?" consent and
    is restored if the user opts to keep going.

Run each in a dedicated Python process — after Qt's
``QApplication.exec()`` returns you can't open another window from
the same process.
"""
from __future__ import annotations

import sys


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "demo"
    if mode in {"-h", "--help"}:
        print(__doc__)
        return 0
    if mode == "demo":
        return _run_demo()
    if mode == "launcher":
        return _run_launcher()
    print(__doc__)
    return 1


def _run_demo() -> int:
    from sayzo_agent.config import load_config
    from sayzo_agent.gui.hud.window import HudWindow

    cfg = load_config()
    HudWindow(cfg, demo=True).run_blocking()
    return 0


def _run_launcher() -> int:
    """Interactive HUD test menu.

    Spawns the production ``HudLauncher`` (same subprocess the agent
    uses) and lets you fire every event type from a menu. The four
    "are you still here?" consents pause the pill and restore it on
    continue answers, mirroring what
    ``ArmController._ask_consent_pausing_pill`` does in production.
    """
    import asyncio
    import time
    from typing import Awaitable, Callable

    from sayzo_agent.gui.hud.launcher import HudLauncher

    async def _drive() -> int:
        launcher = HudLauncher()
        await launcher.start()
        ready = await launcher.wait_for_ready(timeout_secs=30.0)
        if not ready:
            print("HUD never became ready — bailing")
            await launcher.quit()
            return 1

        loop = asyncio.get_running_loop()

        # Pill state is tracked inside HudLauncher (`_last_pill_params`)
        # — we just call show_pill / hide_pill and the launcher keeps
        # the snapshot needed by `ask_consent_pausing_pill`.

        def _do_show_pill(reason: str, reason_label: str) -> None:
            launcher.show_pill(
                reason=reason,
                reason_label=reason_label,
                start_ts=time.time(),
                hotkey="Ctrl+Alt+S",
            )

        def _do_hide_pill() -> None:
            launcher.hide_pill()

        def _pill_shown() -> bool:
            return launcher._last_pill_params is not None  # noqa: SLF001

        async def _ask(
            title: str, body: str, yes: str, no: str,
            *, timeout_secs: float, default_on_timeout: str,
        ) -> str:
            """Blocking ``ask_consent`` on a worker thread."""
            return await loop.run_in_executor(
                None,
                lambda: launcher.ask_consent(
                    title, body, yes, no,
                    timeout_secs, default_on_timeout,
                ),
            )

        async def _ask_pausing_pill(
            title: str, body: str, yes: str, no: str,
            *, timeout_secs: float, default_on_timeout: str,
        ) -> str:
            """Delegates to HudLauncher.ask_consent_pausing_pill on a worker thread."""
            was_paused = _pill_shown()
            if was_paused:
                print("   · pill paused")
            result = await loop.run_in_executor(
                None,
                lambda: launcher.ask_consent_pausing_pill(
                    title, body, yes, no,
                    timeout_secs, default_on_timeout,
                ),
            )
            print(f"   → user answered: {result}")
            if was_paused and _pill_shown():
                print("   · pill restored")
            return result

        # ----- Each menu action -------------------------------------------

        async def cmd_pill_hotkey() -> None:
            _do_show_pill("hotkey", "Hotkey")
            print("✓ pill shown (Hotkey)")

        async def cmd_pill_zoom() -> None:
            _do_show_pill("whitelist", "Zoom")
            print("✓ pill shown (Zoom — whitelist reason)")

        async def cmd_hide_pill() -> None:
            _do_hide_pill()
            print("✓ pill hidden")

        async def cmd_collapse() -> None:
            launcher.set_pill_collapsed(True)
            print("✓ pill collapsed to dot")

        async def cmd_expand() -> None:
            launcher.set_pill_collapsed(False)
            print("✓ dot expanded to pill")

        async def cmd_info_toast() -> None:
            launcher.show_toast(
                "Conversation saved",
                "Discussion about Q4 targets · 2m 34s",
                ttl_secs=4.0,
            )
            print("✓ info toast fired (auto-expires in 4 s)")

        async def cmd_actionable() -> None:
            pressed = asyncio.Event()
            expired = asyncio.Event()
            launcher.show_actionable(
                "Daily speaking drill",
                "Two minutes today — practice your filler-word habit.",
                button_label="Open drill",
                on_pressed=lambda: loop.call_soon_threadsafe(pressed.set),
                expire_after_secs=30.0,
                on_expire=lambda: loop.call_soon_threadsafe(expired.set),
            )
            print("✓ actionable shown (click 'Open drill' or wait 30 s)")
            done, _ = await asyncio.wait(
                {
                    asyncio.create_task(pressed.wait()),
                    asyncio.create_task(expired.wait()),
                },
                return_when=asyncio.FIRST_COMPLETED,
            )
            print(f"   → {'pressed' if pressed.is_set() else 'expired'}")

        async def cmd_pending_close() -> None:
            """Joint-silence pending close: 'Was that the end of your meeting?'

            Yes/timeout → disarm (production calls ``commit_close`` +
            ``_disarm_internal``). No → revert close, pill restored.
            """
            if not _pill_shown():
                print("   ⚠  no pill currently shown — firing anyway")
            result = await _ask_pausing_pill(
                "Was that the end of your meeting?",
                "It's been quiet for a bit. Wrap up and save, or keep going?",
                "Yes, done", "Not yet",
                timeout_secs=15.0,
                default_on_timeout="yes",  # default Wrap up
            )
            if result in ("yes", "timeout"):
                _do_hide_pill()
                print("   · disarmed (pill cleared)")

        async def cmd_checkin() -> None:
            """Long-meeting check-in: 'Still in the meeting?'

            Yes/timeout → keep going (pill restored). No → wrap up,
            disarm.
            """
            if not _pill_shown():
                print("   ⚠  no pill — fire option 1 first to be realistic")
            result = await _ask_pausing_pill(
                "Still in the meeting?",
                "Sayzo has been capturing for 1 hr 5 min. Keep going, or wrap up?",
                "Yes, keep going", "Wrap up",
                timeout_secs=15.0,
                default_on_timeout="yes",  # default keep going
            )
            if result == "no":
                _do_hide_pill()
                print("   · wrap up → disarmed")

        async def cmd_meeting_ended() -> None:
            """Whitelist watcher: 'Looks like your meeting ended'.

            Wrap up (yes/timeout) → disarm. Keep going (no) → pill
            restored, watcher silently re-arms.
            """
            if not _pill_shown():
                print("   ⚠  no pill — fire option 2 (Zoom) first to be realistic")
            result = await _ask_pausing_pill(
                "Looks like your meeting ended",
                "Sayzo noticed Zoom stopped using the microphone. "
                "Wrap up and save, or keep going?",
                "Wrap up", "Keep going",
                timeout_secs=15.0,
                default_on_timeout="yes",  # default Wrap up
            )
            if result in ("yes", "timeout"):
                _do_hide_pill()
                print("   · wrap up → disarmed")

        async def cmd_hotkey_end() -> None:
            """Hotkey-pressed-while-armed: 'Stop recording?'

            Yes → disarm. No/timeout → cancel, pill stays / is restored.
            """
            if not _pill_shown():
                print("   ⚠  no pill currently shown — this consent only "
                      "fires when armed")
            result = await _ask_pausing_pill(
                "Stop recording?",
                "We'll save what we've captured so far.",
                "Yes, stop", "Cancel",
                timeout_secs=15.0,
                default_on_timeout="no",  # default cancel
            )
            if result == "yes":
                _do_hide_pill()
                print("   · stopped → disarmed")

        async def cmd_hotkey_start() -> None:
            """Hotkey-pressed-while-disarmed: 'Start recording?'

            Yes → arm, pill shown. No/timeout → stay disarmed.
            No pill exists during this consent so no pause/restore.
            """
            if _pill_shown():
                print("   ⚠  pill already shown — in production this "
                      "consent fires only while disarmed")
            result = await _ask(
                "Start recording?",
                "Sayzo will capture this conversation so we can coach you on it.",
                "Yes, start", "Cancel",
                timeout_secs=15.0,
                default_on_timeout="no",
            )
            print(f"   → user answered: {result}")
            if result == "yes":
                _do_show_pill("hotkey", "Hotkey")
                print("   · armed → pill shown")

        async def cmd_whitelist() -> None:
            """Whitelist auto-suggest: 'Sayzo is ready to coach you'.

            Yes → arm (Zoom reason). No/timeout → stay disarmed.
            No pill during this consent.
            """
            if _pill_shown():
                print("   ⚠  pill already shown — whitelist consent fires "
                      "only while disarmed")
            result = await _ask(
                "Sayzo is ready to coach you",
                "Looks like you're in Zoom. Want us to capture this so "
                "we can highlight your coachable moments?",
                "Start coaching", "Not now",
                timeout_secs=15.0,
                default_on_timeout="no",
            )
            print(f"   → user answered: {result}")
            if result == "yes":
                _do_show_pill("whitelist", "Zoom")
                print("   · armed → pill shown (Zoom)")

        async def cmd_hide_all() -> None:
            launcher.hide_all()
            print("✓ hide_all sent")

        # ----- Menu loop ---------------------------------------------------

        options: list[tuple[str, str, Callable[[], Awaitable[None]]]] = [
            ("1", "Show pill (Hotkey reason)", cmd_pill_hotkey),
            ("2", "Show pill (Zoom reason — whitelist)", cmd_pill_zoom),
            ("3", "Hide pill (manual)", cmd_hide_pill),
            ("4", "Collapse pill → dot", cmd_collapse),
            ("5", "Expand dot → pill", cmd_expand),
            ("6", "Info toast", cmd_info_toast),
            ("7", "Actionable toast (daily drill, 30 s)", cmd_actionable),
            ("8", "Consent: 'Was that the end of your meeting?'",
                cmd_pending_close),
            ("9", "Consent: 'Still in the meeting?' (long-meeting check-in)",
                cmd_checkin),
            ("10", "Consent: 'Looks like your meeting ended' (whitelist)",
                cmd_meeting_ended),
            ("11", "Consent: 'Stop recording?' (hotkey while armed)",
                cmd_hotkey_end),
            ("12", "Consent: 'Start recording?' (hotkey while disarmed)",
                cmd_hotkey_start),
            ("13", "Consent: 'Sayzo is ready to coach you' (whitelist)",
                cmd_whitelist),
            ("14", "Hide all", cmd_hide_all),
        ]

        def print_menu() -> None:
            pill_state = "shown" if _pill_shown() else "hidden"
            print(f"\n=== HUD test menu — pill: {pill_state} ===")
            for key, label, _ in options:
                print(f"  {key:>2}) {label}")
            print("   q) Quit")

        try:
            while True:
                print_menu()
                try:
                    choice_raw = await loop.run_in_executor(None, input, "> ")
                except EOFError:
                    break
                choice = choice_raw.strip().lower()
                if choice in ("q", "quit", "exit"):
                    break
                handler = next(
                    (h for k, _, h in options if k == choice), None,
                )
                if handler is None:
                    print(f"   ? unknown choice: {choice_raw!r}")
                    continue
                try:
                    await handler()
                except Exception:
                    import traceback
                    traceback.print_exc()
        finally:
            await launcher.quit()
        return 0

    return asyncio.run(_drive())


if __name__ == "__main__":
    sys.exit(main())
