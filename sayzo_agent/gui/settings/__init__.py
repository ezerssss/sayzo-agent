"""Pywebview-hosted Settings window.

Runs in a separate ``sayzo-agent settings`` subprocess so each instance
owns its own main thread — Cocoa requires Settings UI on the main thread,
but the agent's main thread is held by pystray on macOS, so an in-process
Settings window can't satisfy both. Subprocess + JSON-RPC IPC sidesteps
the conflict on Mac and gives Windows a single code path.
"""
