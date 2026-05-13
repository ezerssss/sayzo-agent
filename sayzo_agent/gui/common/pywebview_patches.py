"""Runtime patches against pywebview's WinForms / WebView2 backend.

Why this exists
---------------

pywebview's ``BrowserForm.on_close`` (winforms.py) ends with
``self.Invoke(Func[Type](_shutdown))`` where ``_shutdown`` calls
``self.hide()``, ``self.browser.clear_user_data()``, and
``WinForms.Application.Exit()``. Two failure shapes have surfaced from
that chain in production:

1. ``'NoneType' object has no attribute 'BrowserProcessId'`` in
   ``EdgeChrome.clear_user_data`` when the form closes before WebView2
   finishes its async init (boot-time race).
2. ``InvalidComObjectException: COM object that has been separated from
   its underlying RCW`` in ``Control.MarshaledInvoke`` when Windows
   shutdown kills the WebView2 child process before the form's
   FormClosed dispatcher runs.

Both escape through ``__System_Windows_Forms_FormClosedEventHandlerDispatcher``
and surface as ``.NET Framework — Unhandled Exception`` JIT dialogs on
the user's screen, blocking shutdown.

Two layered patches in this module:

- :func:`patch_clear_user_data_none_guard` — defense-in-depth against
  shape #1. Largely redundant in v2.15.0+ because we now pass
  ``private_mode=False`` to ``webview.start()``, which makes
  ``clear_user_data`` early-return at edgechromium.py:85-86 before any
  ``BrowserProcessId`` access. Kept as belt-and-suspenders in case
  private_mode is ever flipped back during dev or by a future refactor.
- :func:`patch_on_close_swallow_teardown` — direct fix for shape #2 and
  any future variant. Wraps ``BrowserForm.on_close`` with a try/except
  that swallows known teardown exceptions and re-raises anything that
  doesn't look like a pywebview shutdown crash. This is the v2.7.5
  approach, reintroduced — see note below.

Why on_close-wrapping is safe NOW, even though v2.7.5 was reverted
-------------------------------------------------------------------

v2.7.5 shipped a similar ``on_close`` wrap and was reverted in v2.7.6
after a user lag report on the idle-Settings tab-switch path. v2.7.8
later traced the lag to a *separate* race (``loaded`` event firing
before WebView2's JS bridge was usable) and fixed it with
``settings/window.py::_BRIDGE_SETTLE_SECS = 3.0``. The on_close wrap
was an innocent bystander whose timing changes happened to make the
latent bridge-settle race more reliable. With v2.7.8's fix in place,
reintroducing the wrap doesn't carry that regression risk.

Both patches only run at shutdown via the ``FormClosed`` chain — never
on show/hide/navigation/JS-bridge calls. Blast radius bounded.
"""
from __future__ import annotations

import functools
import logging
import sys

log = logging.getLogger(__name__)

_CLEAR_USER_DATA_MARKER = "_sayzo_clear_user_data_none_guard"
_ON_CLOSE_MARKER = "_sayzo_on_close_swallow_teardown"


def patch_clear_user_data_none_guard() -> bool:
    """Wrap ``EdgeChrome.clear_user_data`` to no-op on uninitialized WebView2.

    Defense-in-depth under v2.15.0+'s ``private_mode=False`` default
    (which makes the method early-return at edgechromium.py:85-86
    without ever reaching ``BrowserProcessId``). Still installed so a
    dev who flips private_mode back gets the protection automatically.

    Returns True if the patch is in place after the call (just applied
    or previously applied). Returns False on non-Windows or if
    pywebview's edgechromium module can't be imported. Idempotent via
    a class-attribute marker.
    """
    if sys.platform != "win32":
        return False

    try:
        from webview.platforms.edgechromium import EdgeChrome
    except Exception:
        log.warning("[pywebview_patches] EdgeChrome import failed", exc_info=True)
        return False

    if getattr(EdgeChrome, _CLEAR_USER_DATA_MARKER, False):
        return True

    original = EdgeChrome.clear_user_data

    @functools.wraps(original)
    def clear_user_data(self):
        if self.webview.CoreWebView2 is None:
            log.info("[pywebview_patches] clear_user_data skipped: CoreWebView2 is None")
            try:
                self.webview.Dispose()
            except Exception:
                log.warning("[pywebview_patches] Dispose() raised", exc_info=True)
            return
        return original(self)

    EdgeChrome.clear_user_data = clear_user_data
    setattr(EdgeChrome, _CLEAR_USER_DATA_MARKER, True)
    log.info("[pywebview_patches] EdgeChrome.clear_user_data None-guard installed")
    return True


# Exception-class names we recognize as "pywebview shutdown teardown" and
# swallow inside on_close. Keep in sync with win_shutdown.py's
# _PYWEBVIEW_TEARDOWN_SIGNATURES — same intent, different match site (here
# we have a live Python exception object instead of a stringified .NET
# stack, so we match on exception class name + str()).
_TEARDOWN_EXCEPTION_NAMES = (
    "InvalidComObjectException",
    "ObjectDisposedException",
    "InvalidOperationException",
    "ArgumentException",
    "COMException",
    "KeyError",
    "AttributeError",
)

_TEARDOWN_MESSAGE_SUBSTRINGS = (
    "BrowserProcessId",
    "Process with an Id of",
    "separated from its underlying RCW",
    "Cannot access a disposed object",
)


def _looks_like_teardown_exception(exc: BaseException) -> bool:
    """Conservative classifier: is this a pywebview-teardown-time crash?

    We err on the side of swallowing during the shutdown window because
    the alternative (a real bug landing in on_close at the exact wrong
    moment) is logged at WARNING and the process is exiting anyway.
    Returning True does NOT mean "this is fine" — it means "we'd rather
    log it than show the user a JIT dialog at boot/shutdown."
    """
    name = type(exc).__name__
    if name in _TEARDOWN_EXCEPTION_NAMES:
        return True
    msg = str(exc)
    return any(s in msg for s in _TEARDOWN_MESSAGE_SUBSTRINGS)


def patch_on_close_swallow_teardown() -> bool:
    """Wrap ``BrowserForm.on_close`` to swallow teardown-time exceptions.

    The wrap runs the original ``on_close`` and catches:
      - any ``_TEARDOWN_EXCEPTION_NAMES`` class
      - any exception whose ``str()`` matches ``_TEARDOWN_MESSAGE_SUBSTRINGS``

    Anything else re-raises, preserving the surface for genuine bugs.

    This is the v2.7.5-style approach reintroduced after v2.7.8 fixed
    the bridge-settle race that we previously misattributed to this
    patch. Idempotent class-attribute marker; Windows-only.
    """
    if sys.platform != "win32":
        return False

    try:
        from webview.platforms.winforms import BrowserView
    except Exception:
        log.warning("[pywebview_patches] BrowserView import failed", exc_info=True)
        return False

    form_cls = getattr(BrowserView, "BrowserForm", None)
    if form_cls is None:
        log.warning("[pywebview_patches] BrowserView.BrowserForm not found")
        return False

    if getattr(form_cls, _ON_CLOSE_MARKER, False):
        return True

    original = form_cls.on_close

    @functools.wraps(original)
    def on_close(self, *args, **kwargs):
        try:
            return original(self, *args, **kwargs)
        except BaseException as exc:
            if _looks_like_teardown_exception(exc):
                log.warning(
                    "[pywebview_patches] on_close swallowed teardown exception "
                    "%s: %s",
                    type(exc).__name__,
                    exc,
                )
                return None
            raise

    form_cls.on_close = on_close
    setattr(form_cls, _ON_CLOSE_MARKER, True)
    log.info("[pywebview_patches] BrowserForm.on_close teardown-swallow installed")
    return True
