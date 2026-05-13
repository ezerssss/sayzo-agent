"""Unit tests for the EdgeChrome.clear_user_data None-guard patch.

We don't depend on a real pywebview / pythonnet install — the patch
imports ``webview.platforms.edgechromium`` lazily inside the function,
so we stub it in ``sys.modules`` before calling.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _isolate_modules():
    """Snapshot + restore the modules we touch so tests don't bleed."""
    saved = {
        k: sys.modules.get(k)
        for k in (
            "webview",
            "webview.platforms",
            "webview.platforms.edgechromium",
            "sayzo_agent.gui.common.pywebview_patches",
        )
    }
    yield
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


def _install_edgechrome_stub():
    """Install a fake ``webview.platforms.edgechromium.EdgeChrome`` class.

    Returns the class itself so tests can read state off it (was the
    marker set? did the wrapper replace the method?).
    """

    class EdgeChrome:
        original_calls = []

        def clear_user_data(self):
            EdgeChrome.original_calls.append(self)
            return "original-ran"

    fake_module = types.ModuleType("webview.platforms.edgechromium")
    fake_module.EdgeChrome = EdgeChrome
    sys.modules.setdefault("webview", types.ModuleType("webview"))
    sys.modules.setdefault("webview.platforms", types.ModuleType("webview.platforms"))
    sys.modules["webview.platforms.edgechromium"] = fake_module
    return EdgeChrome


def test_no_op_on_non_windows(monkeypatch):
    """macOS / Linux: patch returns False without touching pywebview."""
    monkeypatch.setattr(sys, "platform", "darwin")

    from sayzo_agent.gui.common.pywebview_patches import (
        patch_clear_user_data_none_guard,
    )

    assert patch_clear_user_data_none_guard() is False


def test_returns_false_when_pywebview_missing(monkeypatch):
    """If edgechromium can't import, patch logs + returns False (no crash)."""
    monkeypatch.setattr(sys, "platform", "win32")
    # Force ImportError on the lazy import.
    sys.modules["webview.platforms.edgechromium"] = None  # type: ignore[assignment]

    from sayzo_agent.gui.common.pywebview_patches import (
        patch_clear_user_data_none_guard,
    )

    assert patch_clear_user_data_none_guard() is False


def test_patch_installs_on_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    cls = _install_edgechrome_stub()
    original = cls.clear_user_data

    from sayzo_agent.gui.common.pywebview_patches import (
        patch_clear_user_data_none_guard,
    )

    assert patch_clear_user_data_none_guard() is True
    # Method object swapped.
    assert cls.clear_user_data is not original
    # functools.wraps preserves the wrapped reference.
    assert cls.clear_user_data.__wrapped__ is original
    # Idempotency marker set so re-applying is a no-op.
    assert getattr(cls, "_sayzo_clear_user_data_none_guard", False) is True


def test_patch_is_idempotent(monkeypatch):
    """Calling the patch twice doesn't double-wrap."""
    monkeypatch.setattr(sys, "platform", "win32")
    cls = _install_edgechrome_stub()

    from sayzo_agent.gui.common.pywebview_patches import (
        patch_clear_user_data_none_guard,
    )

    assert patch_clear_user_data_none_guard() is True
    first_method = cls.clear_user_data
    assert patch_clear_user_data_none_guard() is True
    second_method = cls.clear_user_data
    # Same method object — we didn't wrap on top of the wrapper.
    assert first_method is second_method


def test_patched_skips_when_corewebview2_is_none(monkeypatch):
    """The actual bug: CoreWebView2=None must short-circuit, not crash."""
    monkeypatch.setattr(sys, "platform", "win32")
    cls = _install_edgechrome_stub()

    from sayzo_agent.gui.common.pywebview_patches import (
        patch_clear_user_data_none_guard,
    )

    patch_clear_user_data_none_guard()

    instance = MagicMock()
    instance.webview = MagicMock()
    instance.webview.CoreWebView2 = None

    # The unpatched method would crash on `.BrowserProcessId` access.
    # The patched one must Dispose() and return cleanly.
    result = cls.clear_user_data(instance)

    assert result is None
    instance.webview.Dispose.assert_called_once()
    # Original method was NOT called — we short-circuited.
    assert cls.original_calls == []


def test_patched_skip_survives_dispose_failure(monkeypatch):
    """Dispose() raising shouldn't propagate — we're already on the
    shutdown path and the only reason to call it was to release the
    .NET handle, which the process exit will do anyway."""
    monkeypatch.setattr(sys, "platform", "win32")
    cls = _install_edgechrome_stub()

    from sayzo_agent.gui.common.pywebview_patches import (
        patch_clear_user_data_none_guard,
    )

    patch_clear_user_data_none_guard()

    instance = MagicMock()
    instance.webview = MagicMock()
    instance.webview.CoreWebView2 = None
    instance.webview.Dispose.side_effect = RuntimeError("handle gone")

    # Must not raise.
    result = cls.clear_user_data(instance)
    assert result is None


def test_patched_delegates_to_original_when_corewebview2_alive(monkeypatch):
    """Happy path: WebView2 finished initializing → run the original
    clear_user_data unchanged. The whole point of the patch is to only
    intervene in the broken state."""
    monkeypatch.setattr(sys, "platform", "win32")
    cls = _install_edgechrome_stub()

    from sayzo_agent.gui.common.pywebview_patches import (
        patch_clear_user_data_none_guard,
    )

    patch_clear_user_data_none_guard()

    instance = MagicMock()
    instance.webview = MagicMock()
    # CoreWebView2 is a non-None object (any truthy stand-in).
    instance.webview.CoreWebView2 = MagicMock()

    result = cls.clear_user_data(instance)

    # Original ran (and our stub returns the sentinel).
    assert result == "original-ran"
    assert cls.original_calls == [instance]
    # Patch did NOT call Dispose on the short-circuit path.
    instance.webview.Dispose.assert_not_called()
