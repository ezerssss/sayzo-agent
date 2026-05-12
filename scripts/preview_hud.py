"""Open the Sayzo HUD window standalone for visual review.

Usage:
    python scripts/preview_hud.py demo
    python scripts/preview_hud.py
    python scripts/preview_hud.py launcher

Modes:
  * ``demo`` (default) — launches the real frameless pywebview HUD with
    the in-HUD demo control strip enabled. Click each button in the
    strip to fire the corresponding event type (pill, dot, consent
    card, info toast, actionable). The HUD is rendered top-right of
    the primary monitor with the same flags it uses in production.

  * ``launcher`` — spawns the HUD via the production ``HudLauncher``
    code path (same as the live agent uses) and lets you script
    commands at it from this process. Useful for end-to-end testing of
    the stdin/stdout protocol; the demo mode is fine for visual /
    interaction work and avoids the subprocess overhead.

Run each in a dedicated Python process — after pywebview's
``webview.start()`` returns you can't open another window from the same
process.
"""
from __future__ import annotations

import sys
import time


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "demo"
    if mode in {"demo", "-h", "--help"} and mode != "demo":
        print(__doc__)
        return 0 if mode in {"-h", "--help"} else 1
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
    """Spawn the HUD via the real ``HudLauncher`` and script some events.

    Demonstrates the agent's view of the HUD — same JSON protocol,
    same subprocess lifecycle. Useful for verifying that consent
    round-trips actually resolve back into the parent process.
    """
    import asyncio

    from sayzo_agent.gui.hud.launcher import HudLauncher

    async def _drive() -> int:
        launcher = HudLauncher()
        await launcher.start()
        ready = await launcher.wait_for_ready(timeout_secs=30.0)
        if not ready:
            print("HUD never became ready — bailing")
            await launcher.quit()
            return 1

        print("\n--- showing pill (Hotkey, t=0) ---")
        launcher.show_pill(reason="hotkey", reason_label="Hotkey", hotkey="Ctrl+Alt+S")
        await asyncio.sleep(3.0)

        print("\n--- info toast ---")
        launcher.show_toast(
            "Conversation saved",
            "Discussion about Q4 targets · 2m 34s",
            ttl_secs=4.0,
        )
        await asyncio.sleep(2.0)

        print("\n--- consent card (await answer) ---")
        # ask_consent is blocking — run on a worker thread so the loop
        # stays responsive for the stdout reader to deliver the answer.
        loop = asyncio.get_running_loop()
        answer = await loop.run_in_executor(
            None,
            lambda: launcher.ask_consent(
                "Was that the end of your meeting?",
                "It's been quiet for a bit. Wrap up and save, or keep going?",
                "Wrap up",
                "Keep going",
                15.0,
                "yes",
            ),
        )
        print(f"\n→ user answered: {answer}\n")

        print("--- actionable toast (daily drill) ---")
        pressed = asyncio.Event()
        expired = asyncio.Event()
        launcher.show_actionable(
            "Daily speaking drill",
            "Two minutes today — practice your filler-word habit.",
            button_label="Open drill",
            on_pressed=lambda: loop.call_soon_threadsafe(pressed.set),
            expire_after_secs=15.0,
            on_expire=lambda: loop.call_soon_threadsafe(expired.set),
        )
        done, _ = await asyncio.wait(
            {asyncio.create_task(pressed.wait()), asyncio.create_task(expired.wait())},
            return_when=asyncio.FIRST_COMPLETED,
        )
        print(f"→ actionable outcome: {'pressed' if pressed.is_set() else 'expired'}")

        await asyncio.sleep(1.0)
        launcher.hide_pill()
        await asyncio.sleep(0.5)
        await launcher.quit()
        return 0

    return asyncio.run(_drive())


if __name__ == "__main__":
    sys.exit(main())
