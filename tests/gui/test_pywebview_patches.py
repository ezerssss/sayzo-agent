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
            "webview.platforms.winforms",
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


def _install_browserform_stub():
    """Install a fake ``webview.platforms.winforms.BrowserView.BrowserForm``.

    Returns the BrowserForm class so tests can read state off it.
    """

    class BrowserForm:
        original_calls = []

        def on_close(self, *args, **kwargs):
            BrowserForm.original_calls.append((self, args, kwargs))
            return "original-ran"

    BrowserView = types.SimpleNamespace(BrowserForm=BrowserForm)

    fake_module = types.ModuleType("webview.platforms.winforms")
    fake_module.BrowserView = BrowserView
    sys.modules.setdefault("webview", types.ModuleType("webview"))
    sys.modules.setdefault("webview.platforms", types.ModuleType("webview.platforms"))
    sys.modules["webview.platforms.winforms"] = fake_module
    return BrowserForm


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


# ---------------------------------------------------------------------------
# patch_on_close_swallow_teardown tests
# ---------------------------------------------------------------------------


def test_on_close_patch_no_op_on_non_windows(monkeypatch):
    """macOS / Linux: patch returns False without touching pywebview."""
    monkeypatch.setattr(sys, "platform", "darwin")

    from sayzo_agent.gui.common.pywebview_patches import (
        patch_on_close_swallow_teardown,
    )

    assert patch_on_close_swallow_teardown() is False


def test_on_close_patch_returns_false_when_pywebview_missing(monkeypatch):
    """If winforms can't import, patch logs + returns False (no crash)."""
    monkeypatch.setattr(sys, "platform", "win32")
    sys.modules["webview.platforms.winforms"] = None  # type: ignore[assignment]

    from sayzo_agent.gui.common.pywebview_patches import (
        patch_on_close_swallow_teardown,
    )

    assert patch_on_close_swallow_teardown() is False


def test_on_close_patch_installs_and_is_idempotent(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    cls = _install_browserform_stub()
    original = cls.on_close

    from sayzo_agent.gui.common.pywebview_patches import (
        patch_on_close_swallow_teardown,
    )

    assert patch_on_close_swallow_teardown() is True
    assert cls.on_close is not original
    assert cls.on_close.__wrapped__ is original
    assert getattr(cls, "_sayzo_on_close_swallow_teardown", False) is True

    # Re-applying must not double-wrap.
    first_method = cls.on_close
    assert patch_on_close_swallow_teardown() is True
    assert cls.on_close is first_method


def test_on_close_patch_passes_through_happy_path(monkeypatch):
    """No exception → original on_close runs, return value preserved."""
    monkeypatch.setattr(sys, "platform", "win32")
    cls = _install_browserform_stub()

    from sayzo_agent.gui.common.pywebview_patches import (
        patch_on_close_swallow_teardown,
    )

    patch_on_close_swallow_teardown()

    instance = MagicMock()
    result = cls.on_close(instance, "arg1", kw="kw1")

    assert result == "original-ran"
    assert cls.original_calls == [(instance, ("arg1",), {"kw": "kw1"})]


def test_on_close_patch_swallows_known_teardown_by_class_name(monkeypatch, caplog):
    """A class named like a CLR teardown exception is swallowed silently."""
    monkeypatch.setattr(sys, "platform", "win32")
    cls = _install_browserform_stub()

    # Re-bind the original to one that raises the teardown exception. The
    # class NAME is what the classifier matches against, so we name the
    # exception class to mimic the real CLR exception that surfaces.
    class InvalidComObjectException(Exception):
        pass

    def boom(self, *args, **kwargs):
        raise InvalidComObjectException("COM object that has been separated from its underlying RCW cannot be used.")

    cls.on_close = boom

    from sayzo_agent.gui.common.pywebview_patches import (
        patch_on_close_swallow_teardown,
    )

    patch_on_close_swallow_teardown()

    with caplog.at_level("WARNING", logger="sayzo_agent.gui.common.pywebview_patches"):
        # Must not raise.
        result = cls.on_close(MagicMock())

    assert result is None
    assert any(
        "on_close swallowed teardown exception" in rec.getMessage()
        for rec in caplog.records
    )


def test_on_close_patch_swallows_by_message_substring(monkeypatch, caplog):
    """A vanilla exception class but with a teardown-shaped message is swallowed.

    Real-world: pythonnet sometimes wraps CLR exceptions in plain Exception
    subclasses; matching on the message text is the second line of defense.
    """
    monkeypatch.setattr(sys, "platform", "win32")
    cls = _install_browserform_stub()

    def boom(self, *args, **kwargs):
        # Plain Exception (not in the class-name allowlist) but the message
        # carries the unique RCW-detached substring.
        raise Exception("something something separated from its underlying RCW something")

    cls.on_close = boom

    from sayzo_agent.gui.common.pywebview_patches import (
        patch_on_close_swallow_teardown,
    )

    patch_on_close_swallow_teardown()

    with caplog.at_level("WARNING", logger="sayzo_agent.gui.common.pywebview_patches"):
        result = cls.on_close(MagicMock())

    assert result is None
    assert any(
        "swallowed teardown exception" in rec.getMessage()
        for rec in caplog.records
    )


def test_on_close_patch_reraises_real_bugs(monkeypatch):
    """A genuine bug (no teardown signature) must propagate unchanged."""
    monkeypatch.setattr(sys, "platform", "win32")
    cls = _install_browserform_stub()

    class GenuineBug(RuntimeError):
        pass

    def boom(self, *args, **kwargs):
        raise GenuineBug("totally unrelated to shutdown")

    cls.on_close = boom

    from sayzo_agent.gui.common.pywebview_patches import (
        patch_on_close_swallow_teardown,
    )

    patch_on_close_swallow_teardown()

    with pytest.raises(GenuineBug, match="totally unrelated"):
        cls.on_close(MagicMock())
