"""Global hotkey listener with asyncio bridge.

Backed by ``pynput`` — works on Windows without elevation and on macOS
with one-time Accessibility permission (surfaced during the onboarding
walkthrough). Rebinding at runtime is supported: ``rebind(new_binding)``
unregisters the old combo and registers the new one.

Threading model: pynput runs its listener on a dedicated background thread.
When the combo fires, we marshal the event onto the ArmController's asyncio
loop via ``loop.call_soon_threadsafe(callback)`` — no ``janus`` needed since
we just need an edge-triggered signal, not a queue.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

log = logging.getLogger(__name__)


class HotkeySource:
    """Wrap a pynput ``GlobalHotKeys`` listener around a single binding.

    ``callback`` is invoked on the ArmController's event loop every time the
    user presses the combo. If registration fails (permission denied, parse
    error, conflict with another app), ``register()`` logs a warning and
    leaves the listener unregistered. The rest of the agent continues to
    work — users without Accessibility permission on macOS fall back to
    the tray menu.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        callback: Callable[[], None],
    ) -> None:
        self._loop = loop
        self._callback = callback
        self._binding: Optional[str] = None
        self._listener = None  # pynput GlobalHotKeys instance

    # ---- lifecycle ---------------------------------------------------------

    def register(self, binding: str) -> Optional[str]:
        """Try to start listening for ``binding``. Returns None on success,
        or an error string (for the Settings window to surface inline)."""
        if self._listener is not None:
            self.unregister()
        try:
            from pynput.keyboard import GlobalHotKeys
        except Exception as exc:
            log.warning("[arm.hotkey] pynput unavailable: %s", exc)
            return f"pynput unavailable: {exc}"

        try:
            pynput_combo = _to_pynput(binding)
            listener = GlobalHotKeys({pynput_combo: self._on_fire})
            listener.start()
        except Exception as exc:
            log.warning("[arm.hotkey] register %r failed: %s", binding, exc)
            return str(exc)

        self._binding = binding
        self._listener = listener
        log.info("[arm.hotkey] registered %s", binding)
        return None

    def unregister(self) -> None:
        if self._listener is None:
            return
        try:
            self._listener.stop()
        except Exception:
            log.debug("[arm.hotkey] listener stop failed", exc_info=True)
        self._listener = None
        self._binding = None

    def rebind(self, new_binding: str) -> Optional[str]:
        """Swap the current binding for a new one. On failure, restore the
        old binding and return an error string."""
        old = self._binding
        self.unregister()
        err = self.register(new_binding)
        if err is not None and old is not None:
            # Best-effort restore.
            restore_err = self.register(old)
            if restore_err is not None:
                log.warning("[arm.hotkey] restore of old binding %r failed: %s", old, restore_err)
        return err

    @property
    def binding(self) -> Optional[str]:
        return self._binding

    # ---- pynput callback ---------------------------------------------------

    def _on_fire(self) -> None:
        """Called on pynput's listener thread. Marshal onto the asyncio loop."""
        try:
            self._loop.call_soon_threadsafe(self._callback)
        except RuntimeError:
            # Loop closed during shutdown; drop the event silently.
            pass


def _to_pynput(binding: str) -> str:
    """Convert our human-readable binding format into pynput's ``GlobalHotKeys``
    syntax.

    Input examples: ``"ctrl+alt+s"``, ``"ctrl+shift+alt+r"``, ``"cmd+opt+k"``.
    Output: ``"<ctrl>+<alt>+s"`` — modifiers wrapped in angle brackets,
    lowercase, ``+``-separated in the order the user wrote them.
    """
    parts = [p.strip().lower() for p in binding.split("+") if p.strip()]
    if not parts:
        raise ValueError(f"empty hotkey binding: {binding!r}")
    aliases = {
        "ctrl": "<ctrl>",
        "control": "<ctrl>",
        "alt": "<alt>",
        "opt": "<alt>",
        "option": "<alt>",
        "shift": "<shift>",
        "cmd": "<cmd>",
        "command": "<cmd>",
        "meta": "<cmd>",
        "win": "<cmd>",
        "super": "<cmd>",
    }
    out = []
    for p in parts:
        out.append(aliases.get(p, p))
    return "+".join(out)


def humanize_binding(binding: str) -> str:
    """Render a stored hotkey binding (``"ctrl+alt+s"``) in Title Case
    (``"Ctrl+Alt+S"``) for display in the tray menu / tooltip / Settings."""
    if not binding:
        return binding
    out: list[str] = []
    for part in binding.split("+"):
        part = part.strip()
        if not part:
            continue
        out.append(part.upper() if len(part) == 1 else part.title())
    return "+".join(out)


_MODIFIERS = frozenset({
    "ctrl", "control", "alt", "opt", "option", "shift",
    "cmd", "command", "meta", "win", "super",
})

_BLOCKED_COMBOS: dict[frozenset[str], str] = {
    frozenset({"ctrl", "c"}): "clipboard copy",
    frozenset({"ctrl", "v"}): "clipboard paste",
    frozenset({"ctrl", "x"}): "clipboard cut",
    frozenset({"ctrl", "a"}): "select all",
    frozenset({"ctrl", "z"}): "undo",
    frozenset({"alt", "f4"}): "window close",
    frozenset({"alt", "tab"}): "app switcher",
    frozenset({"ctrl", "alt", "delete"}): "system shortcut",
}


def validate_binding(binding: str) -> Optional[str]:
    """Return None if the binding is acceptable, or an error string.

    Reject:
      - Bare keys (no modifier at all).
      - A tiny blocklist of dangerous / system-reserved combos.
    """
    parts = {p.strip().lower() for p in binding.split("+") if p.strip()}
    if not parts:
        return "Shortcut can't be empty"
    if not parts & _MODIFIERS:
        return "Please include at least one modifier (Ctrl, Alt, Shift, or ⌘)"
    label = _BLOCKED_COMBOS.get(frozenset(parts))
    if label is not None:
        return f"That shortcut is used by the OS for {label}. Please pick another."
    return None
