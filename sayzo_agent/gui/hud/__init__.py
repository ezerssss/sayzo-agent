"""Custom HUD overlay subsystem (replaces native notifications).

The HUD is a frameless, transparent, always-on-top pywebview window
hosted in its own subprocess and driven by JSON commands over stdin /
stdout from the parent agent. It owns every user-facing notification
surface: persistent capture pill, consent prompts, info toasts, coaching
cards. Native OS notification APIs (WinRT, UNUserNotificationCenter,
NSUserNotification, osascript modal) are no longer used.

Public entry points:

* :class:`sayzo_agent.gui.hud.window.HudWindow` — child-process side.
  Lives in the `sayzo-agent hud --idle` subprocess; owns the pywebview
  window and the JSON bridge.
* :class:`sayzo_agent.gui.hud.launcher.HudLauncher` — parent-side. Owns
  the subprocess lifecycle, the stdin writer, the stdout reader thread,
  and the per-request ``Future`` dispatch for blocking ``ask_consent``
  calls.
"""
from __future__ import annotations

from .launcher import HudLauncher  # noqa: F401
