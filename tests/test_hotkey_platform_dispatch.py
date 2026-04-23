"""Platform-conditional hotkey backend selection.

``HotkeySource.__init__`` must pick the Carbon backend on macOS and the
pynput-based backend everywhere else. The concrete backends (which touch
OS frameworks) are stubbed — these tests assert the dispatch, not the
OS integration.

Note: ``hotkey_mac.py`` is importable on any platform because it uses
stdlib ctypes to load Carbon lazily on ``register()``, not at import time.
That's what makes cross-platform testing feasible.
"""
from __future__ import annotations

import asyncio

import pytest


def _noop_loop_callback():
    """Return (loop, callback) — both stubs, nothing runs."""
    loop = asyncio.new_event_loop()
    callback = lambda: None
    return loop, callback


def test_darwin_selects_mac_backend(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")

    # Re-import the module fresh so the class body sees the patched sys.platform.
    # (HotkeySource.__init__ checks sys.platform at call time, not import, so
    # we don't actually need a reimport — but we do need to make sure the mac
    # module's import path works.)
    from sayzo_agent.arm import hotkey as hotkey_mod
    from sayzo_agent.arm.hotkey_mac import MacHotkeySource

    loop, cb = _noop_loop_callback()
    try:
        src = hotkey_mod.HotkeySource(loop, cb)
        assert isinstance(src._impl, MacHotkeySource)
    finally:
        loop.close()


def test_non_darwin_selects_pynput_backend(monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")

    from sayzo_agent.arm import hotkey as hotkey_mod

    loop, cb = _noop_loop_callback()
    try:
        src = hotkey_mod.HotkeySource(loop, cb)
        assert isinstance(src._impl, hotkey_mod._PynputHotkeySource)
    finally:
        loop.close()


# ---- hotkey_mac._parse_binding ---------------------------------------------


def test_parse_binding_ctrl_alt_s():
    from sayzo_agent.arm.hotkey_mac import _parse_binding, _CTRL_KEY, _OPT_KEY

    mods, key = _parse_binding("ctrl+alt+s")
    assert mods == (_CTRL_KEY | _OPT_KEY)
    assert key == 1  # kVK_ANSI_S


def test_parse_binding_cmd_shift_r():
    from sayzo_agent.arm.hotkey_mac import _parse_binding, _CMD_KEY, _SHIFT_KEY

    mods, key = _parse_binding("cmd+shift+r")
    assert mods == (_CMD_KEY | _SHIFT_KEY)
    assert key == 15  # kVK_ANSI_R


def test_parse_binding_rejects_no_modifier():
    from sayzo_agent.arm.hotkey_mac import _parse_binding

    with pytest.raises(ValueError, match="modifier"):
        _parse_binding("s")


def test_parse_binding_rejects_multi_keys():
    from sayzo_agent.arm.hotkey_mac import _parse_binding

    with pytest.raises(ValueError, match="exactly one"):
        _parse_binding("ctrl+a+b")


def test_parse_binding_rejects_unsupported_key():
    from sayzo_agent.arm.hotkey_mac import _parse_binding

    with pytest.raises(ValueError, match="unsupported"):
        _parse_binding("ctrl+alt+prtsc")


def test_parse_binding_option_alias_matches_alt():
    from sayzo_agent.arm.hotkey_mac import _parse_binding

    a, _ = _parse_binding("ctrl+alt+s")
    b, _ = _parse_binding("ctrl+option+s")
    c, _ = _parse_binding("ctrl+opt+s")
    assert a == b == c
