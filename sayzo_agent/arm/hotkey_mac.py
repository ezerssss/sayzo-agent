"""macOS-native global hotkey via Carbon's ``RegisterEventHotKey``.

Replaces ``pynput`` on macOS. pynput's keyboard listener runs on a
dedicated background thread and, on macOS 15+, trips
``dispatch_assert_queue_fail`` in ``TSMGetInputSourceProperty`` — Text
Input Sources APIs are now enforced as main-thread-only. Carbon hot keys
instead install an application event handler whose callbacks fire on the
main thread via the AppKit run loop that pystray is already running, with
no TSM involvement.

Why ctypes, not pyobjc-framework-Carbon:

- pyobjc's Carbon bindings are deprecated and incomplete; the hot-key
  functions aren't reliably exposed across pyobjc versions.
- ctypes loads ``Carbon.framework`` directly — one file, stdlib-only,
  works identically across Python releases.

Threading contract (identical to the pynput backend):

- ``register()`` can be called from any thread. ``InstallEventHandler`` +
  ``RegisterEventHotKey`` target ``GetApplicationEventTarget()`` → events
  are delivered to NSApp's main event loop regardless of registrant
  thread.
- The Carbon event handler fires on the main thread (where pystray's
  NSApp runloop is). From there we marshal the Python callback onto the
  ArmController's asyncio loop via ``loop.call_soon_threadsafe`` — same
  edge-triggered signal ``PynputHotkeySource._on_fire`` provides.
"""
from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import logging
from ctypes import (
    CFUNCTYPE,
    POINTER,
    Structure,
    byref,
    c_int32,
    c_uint32,
    c_void_p,
)
from typing import Callable, Optional

log = logging.getLogger(__name__)


# ---- Carbon constants -------------------------------------------------------


def _four_char_code(s: str) -> int:
    """Carbon FourCharCode — packs 4 ASCII bytes into a UInt32, big-endian."""
    b = s.encode("mac-roman")
    if len(b) != 4:
        raise ValueError(f"four-char code must be 4 bytes: {s!r}")
    return (b[0] << 24) | (b[1] << 16) | (b[2] << 8) | b[3]


_KEVT_CLASS_KEYBOARD = _four_char_code("keyb")  # 'keyb'
_KEVT_HOT_KEY_PRESSED = 5  # kEventHotKeyPressed

# Modifier mask bits from <Carbon/CarbonEvents.h>.
_CMD_KEY = 1 << 8
_SHIFT_KEY = 1 << 9
_OPT_KEY = 1 << 11
_CTRL_KEY = 1 << 12

# kVK_ANSI_* / kVK_* virtual key codes from HIToolbox/Events.h. We only
# list the keys that our hotkey-binding syntax accepts; anything else
# fails `_parse_binding` with a clear error.
_VK: dict[str, int] = {
    # Letters
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7,
    "c": 8, "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15,
    "y": 16, "t": 17, "o": 31, "u": 32, "i": 34, "p": 35, "l": 37,
    "j": 38, "k": 40, "n": 45, "m": 46,
    # Digits
    "1": 18, "2": 19, "3": 20, "4": 21, "5": 23, "6": 22, "7": 26,
    "8": 28, "9": 25, "0": 29,
    # Punctuation (only the ones hotkeys realistically use)
    "=": 24, "-": 27, "]": 30, "[": 33, "'": 39, ";": 41,
    "\\": 42, ",": 43, "/": 44, ".": 47, "`": 50,
    # Named keys
    "return": 36, "enter": 36, "tab": 48, "space": 49,
    "delete": 51, "backspace": 51, "escape": 53, "esc": 53,
    # Function keys
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97,
    "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
    # Arrows
    "up": 126, "down": 125, "left": 123, "right": 124,
}


# ---- Carbon structs ---------------------------------------------------------


class _EventHotKeyID(Structure):
    """Carbon EventHotKeyID: { OSType signature; UInt32 id; }"""
    _fields_ = [("signature", c_uint32), ("id", c_uint32)]


class _EventTypeSpec(Structure):
    """Carbon EventTypeSpec: { UInt32 eventClass; UInt32 eventKind; }"""
    _fields_ = [("eventClass", c_uint32), ("eventKind", c_uint32)]


# OSStatus (*) (EventHandlerCallRef, EventRef, void *userData)
_CARBON_HANDLER_PROC = CFUNCTYPE(c_int32, c_void_p, c_void_p, c_void_p)


# ---- Framework loader -------------------------------------------------------


_carbon_cache: Optional[ctypes.CDLL] = None


def _load_carbon() -> Optional[ctypes.CDLL]:
    """Load Carbon.framework via ctypes. Returns None if unavailable."""
    global _carbon_cache
    if _carbon_cache is not None:
        return _carbon_cache
    candidates = [
        "/System/Library/Frameworks/Carbon.framework/Carbon",
        ctypes.util.find_library("Carbon"),
    ]
    for path in candidates:
        if not path:
            continue
        try:
            lib = ctypes.CDLL(path)
        except OSError:
            log.debug("[arm.hotkey_mac] CDLL load %s failed", path, exc_info=True)
            continue
        # Declare signatures for the functions we call. Keeps ctypes from
        # defaulting to c_int return types (which would truncate pointers).
        lib.GetApplicationEventTarget.restype = c_void_p
        lib.GetApplicationEventTarget.argtypes = []
        lib.InstallEventHandler.restype = c_int32
        lib.InstallEventHandler.argtypes = [
            c_void_p, c_void_p, c_uint32, POINTER(_EventTypeSpec),
            c_void_p, POINTER(c_void_p),
        ]
        lib.RemoveEventHandler.restype = c_int32
        lib.RemoveEventHandler.argtypes = [c_void_p]
        lib.RegisterEventHotKey.restype = c_int32
        lib.RegisterEventHotKey.argtypes = [
            c_uint32, c_uint32, _EventHotKeyID, c_void_p,
            c_uint32, POINTER(c_void_p),
        ]
        lib.UnregisterEventHotKey.restype = c_int32
        lib.UnregisterEventHotKey.argtypes = [c_void_p]
        _carbon_cache = lib
        return lib
    log.warning("[arm.hotkey_mac] Carbon.framework could not be loaded")
    return None


# ---- public API -------------------------------------------------------------


def _parse_binding(binding: str) -> tuple[int, int]:
    """Convert ``"ctrl+alt+s"`` to ``(modifier_mask, key_code)`` for Carbon.

    Raises ``ValueError`` on empty / modifier-less / unsupported-key input.
    """
    parts = [p.strip().lower() for p in binding.split("+") if p.strip()]
    if not parts:
        raise ValueError(f"empty hotkey binding: {binding!r}")

    mods = 0
    key_parts: list[str] = []
    for p in parts:
        if p in ("ctrl", "control"):
            mods |= _CTRL_KEY
        elif p in ("alt", "opt", "option"):
            mods |= _OPT_KEY
        elif p == "shift":
            mods |= _SHIFT_KEY
        elif p in ("cmd", "command", "meta", "win", "super"):
            mods |= _CMD_KEY
        else:
            key_parts.append(p)

    if mods == 0:
        raise ValueError("hotkey must include at least one modifier")
    if len(key_parts) != 1:
        raise ValueError(
            f"hotkey must include exactly one non-modifier key, got {key_parts!r}"
        )
    key = key_parts[0]
    if key not in _VK:
        raise ValueError(f"unsupported key {key!r}")
    return mods, _VK[key]


class MacHotkeySource:
    """Carbon-backed global hotkey source. Public API mirrors
    :class:`sayzo_agent.arm.hotkey.PynputHotkeySource`."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        callback: Callable[[], None],
    ) -> None:
        self._loop = loop
        self._callback = callback
        self._binding: Optional[str] = None
        self._hot_key_ref: Optional[c_void_p] = None
        self._handler_ref: Optional[c_void_p] = None
        # CRITICAL: keep a Python-side reference to the CFUNCTYPE instance.
        # If it goes out of scope, Python frees the C trampoline while Carbon
        # still holds a pointer to it — the next hotkey press would jump into
        # deallocated memory.
        self._handler_proc: Optional[_CARBON_HANDLER_PROC] = None
        self._carbon: Optional[ctypes.CDLL] = None

    def register(self, binding: str) -> Optional[str]:
        """Try to register ``binding`` as a global hot key. Returns None on
        success, or an error string (same convention as the pynput backend)."""
        if self._hot_key_ref is not None:
            self.unregister()

        try:
            mods, key_code = _parse_binding(binding)
        except ValueError as exc:
            log.warning("[arm.hotkey_mac] parse %r failed: %s", binding, exc)
            return str(exc)

        carbon = _load_carbon()
        if carbon is None:
            return "Carbon framework unavailable"

        def _trampoline(_call_ref: int, _event_ref: int, _user_data: int) -> int:
            # Runs on the main thread (Carbon events go through NSApp's
            # queue). Marshal onto the asyncio loop and return noErr (0).
            try:
                self._loop.call_soon_threadsafe(self._callback)
            except RuntimeError:
                # Loop closed during shutdown — drop the event silently.
                pass
            return 0

        handler_proc = _CARBON_HANDLER_PROC(_trampoline)
        event_type = _EventTypeSpec(
            eventClass=_KEVT_CLASS_KEYBOARD,
            eventKind=_KEVT_HOT_KEY_PRESSED,
        )
        handler_ref = c_void_p()
        err = carbon.InstallEventHandler(
            carbon.GetApplicationEventTarget(),
            ctypes.cast(handler_proc, c_void_p),
            1,
            byref(event_type),
            None,
            byref(handler_ref),
        )
        if err != 0:
            log.warning("[arm.hotkey_mac] InstallEventHandler err=%d", err)
            return f"InstallEventHandler failed (OSStatus={err})"

        hot_key_id = _EventHotKeyID(signature=_four_char_code("sayz"), id=1)
        hot_key_ref = c_void_p()
        err = carbon.RegisterEventHotKey(
            c_uint32(key_code),
            c_uint32(mods),
            hot_key_id,
            carbon.GetApplicationEventTarget(),
            c_uint32(0),
            byref(hot_key_ref),
        )
        if err != 0:
            carbon.RemoveEventHandler(handler_ref)
            log.warning("[arm.hotkey_mac] RegisterEventHotKey err=%d", err)
            return f"RegisterEventHotKey failed (OSStatus={err})"

        self._handler_proc = handler_proc
        self._handler_ref = handler_ref
        self._hot_key_ref = hot_key_ref
        self._carbon = carbon
        self._binding = binding
        log.info("[arm.hotkey_mac] registered %s", binding)
        return None

    def unregister(self) -> None:
        if self._carbon is None:
            self._handler_proc = None
            self._binding = None
            return
        if self._hot_key_ref is not None:
            try:
                self._carbon.UnregisterEventHotKey(self._hot_key_ref)
            except Exception:
                log.debug("[arm.hotkey_mac] UnregisterEventHotKey failed", exc_info=True)
            self._hot_key_ref = None
        if self._handler_ref is not None:
            try:
                self._carbon.RemoveEventHandler(self._handler_ref)
            except Exception:
                log.debug("[arm.hotkey_mac] RemoveEventHandler failed", exc_info=True)
            self._handler_ref = None
        self._handler_proc = None
        self._binding = None

    def rebind(self, new_binding: str) -> Optional[str]:
        old = self._binding
        self.unregister()
        err = self.register(new_binding)
        if err is not None and old is not None:
            restore_err = self.register(old)
            if restore_err is not None:
                log.warning(
                    "[arm.hotkey_mac] restore of old binding %r failed: %s",
                    old, restore_err,
                )
        return err

    @property
    def binding(self) -> Optional[str]:
        return self._binding
