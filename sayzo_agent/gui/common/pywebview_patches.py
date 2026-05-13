"""Runtime patches against pywebview's WinForms / WebView2 backend.

Why this exists
---------------

pywebview 5.4's ``EdgeChrome.clear_user_data`` (in
``webview/platforms/edgechromium.py``) runs inside the ``FormClosed``
chain (``on_close`` → ``_shutdown`` → ``clear_user_data``) and reads
``self.webview.CoreWebView2.BrowserProcessId`` whenever private-mode
is enabled. Private-mode defaults to True and we don't override it.
But ``CoreWebView2`` is ``None`` until ``EnsureCoreWebView2Async``
finishes — so if the form is closed before WebView2 finishes booting
(observed at Windows login when the auto-started agent's idle Settings
subprocess gets closed mid-init), the method raises
``'NoneType' object has no attribute 'BrowserProcessId'``. Pythonnet
re-throws as a CLR exception in the ``FormClosed`` dispatcher, which
surfaces as a .NET JIT-debugger dialog at login.

Safe to monkey-patch because ``clear_user_data`` only runs at
shutdown — never on show/hide/navigation/JS-bridge calls. The v2.7.5
patch that broke the idle-Settings tab-switch wrapped ``on_close``
itself, a hot-path method; see ``project_pywebview_close_guard_reverted.md``.
This patch's blast radius is bounded to the shutdown path.
"""
from __future__ import annotations

import functools
import logging
import sys

log = logging.getLogger(__name__)

_PATCH_MARKER = "_sayzo_clear_user_data_none_guard"


def patch_clear_user_data_none_guard() -> bool:
    """Wrap ``EdgeChrome.clear_user_data`` to no-op on uninitialized WebView2.

    Returns True if the patch is in place after the call (just applied
    or previously applied). Returns False on non-Windows (silent skip)
    or if pywebview's edgechromium module can't be imported (logged).
    Idempotent via a class-attribute marker.
    """
    if sys.platform != "win32":
        return False

    try:
        from webview.platforms.edgechromium import EdgeChrome
    except Exception:
        log.warning("[pywebview_patches] EdgeChrome import failed", exc_info=True)
        return False

    if getattr(EdgeChrome, _PATCH_MARKER, False):
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
    setattr(EdgeChrome, _PATCH_MARKER, True)
    log.info("[pywebview_patches] EdgeChrome.clear_user_data None-guard installed")
    return True
