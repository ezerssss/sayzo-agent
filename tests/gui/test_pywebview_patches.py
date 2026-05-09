"""Unit tests for the close-handler idempotency guard.

We don't depend on a real pywebview install — the patch targets attribute
paths inside ``webview.platforms.winforms`` / ``cocoa``. Tests stub those
modules in ``sys.modules`` so the patcher has something to monkey-patch
against, then verify the guarded handler short-circuits on the second
call instead of raising.
"""
from __future__ import annotations

import sys
import types

import pytest


def _reset_module_state():
    """Force ``apply()`` to re-run from a clean slate on each test."""
    from sayzo_agent.gui.common import pywebview_patches

    pywebview_patches._applied = False


def _install_fake_winforms_module():
    """Create a fake ``webview.platforms.winforms`` with a BrowserForm class.

    Mirrors just enough of pywebview's structure that the patcher can
    re-bind ``BrowserView.BrowserForm.on_close``.
    """
    fake_winforms = types.ModuleType("webview.platforms.winforms")

    class FakeBrowserForm:
        def __init__(self, uid: str) -> None:
            self.uid = uid

        def on_close(self, *args):
            FakeBrowserView.fire_count += 1
            if self.uid not in FakeBrowserView.instances:
                raise KeyError(self.uid)
            del FakeBrowserView.instances[self.uid]

    class FakeBrowserView:
        instances: dict = {}
        BrowserForm = FakeBrowserForm
        fire_count: int = 0

    fake_winforms.BrowserView = FakeBrowserView

    # Make sure parent package paths exist too — import resolution
    # walks them.
    sys.modules.setdefault("webview", types.ModuleType("webview"))
    sys.modules.setdefault("webview.platforms", types.ModuleType("webview.platforms"))
    sys.modules["webview.platforms.winforms"] = fake_winforms
    return FakeBrowserView


@pytest.fixture(autouse=True)
def _isolate_module():
    _reset_module_state()
    saved = {
        k: sys.modules.get(k)
        for k in (
            "webview",
            "webview.platforms",
            "webview.platforms.winforms",
            "webview.platforms.cocoa",
        )
    }
    yield
    _reset_module_state()
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


def test_apply_is_idempotent(monkeypatch):
    """Calling ``apply()`` twice doesn't re-wrap the handler.

    Otherwise the wrapper would accumulate guards and short-circuit even
    on the very first close — defeating the cleanup step.
    """
    monkeypatch.setattr(sys, "platform", "win32")
    fake_bv = _install_fake_winforms_module()
    original = fake_bv.BrowserForm.on_close

    from sayzo_agent.gui.common.pywebview_patches import apply

    apply()
    after_first = fake_bv.BrowserForm.on_close
    assert after_first is not original  # patched

    apply()
    after_second = fake_bv.BrowserForm.on_close
    assert after_second is after_first  # not re-wrapped


def test_first_close_runs_cleanup(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    fake_bv = _install_fake_winforms_module()

    from sayzo_agent.gui.common.pywebview_patches import apply

    apply()

    form = fake_bv.BrowserForm("master")
    fake_bv.instances["master"] = form

    form.on_close()

    # Original cleanup ran exactly once.
    assert fake_bv.fire_count == 1
    assert "master" not in fake_bv.instances
    assert getattr(form, "_sayzo_close_done", False) is True


def test_second_close_short_circuits(monkeypatch):
    """Second FormClosed firing on the same form is a no-op, not a KeyError."""
    monkeypatch.setattr(sys, "platform", "win32")
    fake_bv = _install_fake_winforms_module()

    from sayzo_agent.gui.common.pywebview_patches import apply

    apply()

    form = fake_bv.BrowserForm("master")
    fake_bv.instances["master"] = form

    form.on_close()  # first
    form.on_close()  # second — would raise KeyError without the guard

    # First fire's cleanup ran; second fire didn't re-enter the original.
    assert fake_bv.fire_count == 1


def test_first_close_with_missing_instance_swallows_keyerror(monkeypatch):
    """Belt-and-braces: if 'master' is already gone on entry (some other
    code path beat us to the dict), the KeyError doesn't escape into the
    .NET FormClosed dispatcher. Otherwise it surfaces as the OS
    unhandled-exception dialog the user reported."""
    monkeypatch.setattr(sys, "platform", "win32")
    fake_bv = _install_fake_winforms_module()

    from sayzo_agent.gui.common.pywebview_patches import apply

    apply()

    form = fake_bv.BrowserForm("master")
    # Note: 'master' is NOT in instances. The original would raise
    # KeyError. The patch must catch it.
    form.on_close()  # must not raise
    assert getattr(form, "_sayzo_close_done", False) is True


def test_apply_swallows_import_error_on_unsupported_platform(monkeypatch):
    """A missing pywebview platform module on, say, Linux dev runs must
    not break the agent — the patcher logs and returns."""
    monkeypatch.setattr(sys, "platform", "linux")

    from sayzo_agent.gui.common.pywebview_patches import apply

    # Should not raise even though no patches apply on linux.
    apply()


def test_each_form_has_its_own_latch(monkeypatch):
    """Closing window A must not suppress on_close for window B.

    The latch is per-instance (stored on ``self``), so two distinct
    forms get independent guards. Otherwise opening Settings, closing
    it, then re-opening it would silently skip cleanup on the second
    close.
    """
    monkeypatch.setattr(sys, "platform", "win32")
    fake_bv = _install_fake_winforms_module()

    from sayzo_agent.gui.common.pywebview_patches import apply

    apply()

    form_a = fake_bv.BrowserForm("master")
    form_b = fake_bv.BrowserForm("child_abc")
    fake_bv.instances["master"] = form_a
    fake_bv.instances["child_abc"] = form_b

    form_a.on_close()
    form_b.on_close()

    assert fake_bv.fire_count == 2
    assert fake_bv.instances == {}
