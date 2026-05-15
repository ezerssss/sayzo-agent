"""Probe: why does the macOS HUD page fail to load? (v3.1.4 investigation)

Background — the user's 2026-05-15 bug
--------------------------------------
On macOS the Sayzo HUD subprocess shows ``[hud] page load reported not-ok``
in agent.log immediately after spawning. After that, every notification
times out (``[notify] ask_consent waiter raised TimeoutError``) because
React never emits ``hud_ready`` to the parent over the bridge — so the
HUD is invisible no matter what arms or fires.

Same code, same React bundle, same QtWebEngine version → works perfectly
on Windows. So the cause is a macOS-specific load-time failure: file://
sandboxing, signed-bundle permissions, ES-module sub-resource fetching,
or QWebChannel resource missing from the PyInstaller bundle.

This probe loads the EXACT same URL the HUD subprocess loads
(``file:///Applications/Sayzo.app/Contents/Frameworks/sayzo_agent/gui/webui/dist/index.html#route=hud``)
in a minimal QtWebEngine instance, with full instrumentation that the
production HUD doesn't have:

  * JS console capture — every console.log / console.error / Chromium
    page-level error (e.g. "Failed to load module script") is logged.
  * QWebEngineLoadingInfo error code + errorString (Qt 6.2+) — the real
    reason loadFinished(False) fired, not just "not-ok".
  * URL request interceptor — every sub-resource fetch (the ES module
    chunk, CSS, fonts, qrc:///qtwebchannel/qwebchannel.js) is logged
    with method, URL, and resource type.
  * Render-process termination signals — if Chromium crashed instead of
    just failing the load.
  * Qt + Chromium version dump.

Optional ``--devtools`` flag enables Chromium remote debugging on port
9222. Open Chrome on the same Mac, navigate to ``chrome://inspect``,
click "Inspect" on the HUD page → full DevTools (Console / Network /
Sources tabs) on the live page.

Usage
-----
1. On the Mac with the broken HUD, install PySide6 in any Python 3.10+::

       python3 -m pip install --user "PySide6>=6.5" "PySide6-Addons>=6.5"

   (~150 MB download. The bundled Sayzo Python can't run arbitrary
   scripts, so we use system python3.)

2. Copy this script to the Mac and run::

       python3 probe_macos_hud_load.py

   Optional flags::

       --bundle /Applications/Sayzo.app
                  # Override Sayzo bundle path (default: /Applications/Sayzo.app)
       --index /custom/path/to/index.html
                  # Override the HUD index.html path entirely
       --wait 10  # Seconds to wait after load (default: 8) — bump to 30
                  # if you also want to watch what React does post-mount
       --devtools # Enable Chromium remote debugging on port 9222 — open
                  # Chrome → chrome://inspect to attach full DevTools
       --verbose  # Log every URL request (noisy — only flip on if the
                  # default summary doesn't pinpoint the failure)

3. Paste the entire stdout output back to whoever's debugging.

What we're looking for in the output
------------------------------------
PASS (HUD page loads cleanly):
  [load] loadFinished ok=True
  [load] errorCode=0
  [console] (a few harmless React HMR / module-load lines)
  [interceptor] all sub-resources returned 200
  [bridge] hud_ready event received from React
  → HUD subprocess SHOULD work; the bug is elsewhere

FAIL — file:// sandboxing blocks ES module:
  [console] ERROR: Failed to load module script: Strict MIME type ...
  [console] ERROR: Access to script at 'file://...' from origin 'null'
            has been blocked by CORS policy
  → Fix: set QWebEngineSettings.LocalContentCanAccessFileUrls=True or
    inline the JS bundle into a single index.html

FAIL — Vite chunk URL not found:
  [interceptor] file:///.../assets/index-*.js → STATUS 0 (failed)
  [load] errorString=ContentLoadError
  → Fix: PyInstaller bundle data path is wrong, or the dist/ directory
    isn't actually shipped on macOS

FAIL — QWebChannel resource missing:
  [console] ERROR: GET qrc:///qtwebchannel/qwebchannel.js — 404
  → Fix: PySide6 dependency missing from the macOS PyInstaller spec;
    add ``PySide6.QtWebChannel`` resources to the build

FAIL — render process killed:
  [process] renderProcessTerminated status=Crashed exitCode=...
  → Fix: probably a code-signing / hardened-runtime issue with the
    embedded Chromium helper bundle; check ``codesign --verify`` on
    Sayzo.app/Contents/Frameworks/QtWebEngineCore.framework
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

DEFAULT_BUNDLE = Path("/Applications/Sayzo.app")
DEFAULT_INDEX_REL = Path("Contents/Frameworks/sayzo_agent/gui/webui/dist/index.html")
DEFAULT_WAIT_SECS = 8.0


# ----------------------------------------------------------------------
# Filesystem probe — runs without QtWebEngine. Catches "the bundle is
# missing the dist/ directory entirely" and "index.html is 0 bytes"
# before we even try to load it.
# ----------------------------------------------------------------------


@dataclass
class FsReport:
    bundle: Path
    index: Path
    index_exists: bool = False
    index_size: int = 0
    index_md5: Optional[str] = None
    dist_files: list[tuple[str, int]] = field(default_factory=list)
    main_js: Optional[Path] = None
    main_js_size: int = 0
    main_js_md5: Optional[str] = None
    notes: list[str] = field(default_factory=list)


def probe_filesystem(bundle: Path, index: Path) -> FsReport:
    rep = FsReport(bundle=bundle, index=index)
    rep.index_exists = index.exists() and index.is_file()
    if rep.index_exists:
        rep.index_size = index.stat().st_size
        rep.index_md5 = hashlib.md5(index.read_bytes()).hexdigest()[:12]
    else:
        rep.notes.append(f"index.html does not exist at {index}")
        return rep

    dist = index.parent
    if not dist.is_dir():
        rep.notes.append(f"dist directory does not exist at {dist}")
        return rep

    for child in sorted(dist.rglob("*")):
        if child.is_file():
            rep.dist_files.append(
                (str(child.relative_to(dist)), child.stat().st_size)
            )

    # The Vite build emits a single hashed entry like ``assets/index-XXXXX.js``.
    candidates = [
        dist / rel for rel, _ in rep.dist_files
        if rel.startswith("assets/index-") and rel.endswith(".js")
    ]
    if candidates:
        rep.main_js = candidates[0]
        rep.main_js_size = rep.main_js.stat().st_size
        rep.main_js_md5 = hashlib.md5(rep.main_js.read_bytes()).hexdigest()[:12]
    else:
        rep.notes.append(
            "no assets/index-*.js found in dist/ — Vite bundle missing or "
            "renamed; QtWebEngine will 404 on the script tag"
        )

    return rep


def print_fs_report(rep: FsReport) -> None:
    print("=" * 72)
    print("FILESYSTEM PROBE")
    print("=" * 72)
    print(f"bundle: {rep.bundle}  exists={rep.bundle.exists()}")
    print(f"index:  {rep.index}")
    print(f"        exists={rep.index_exists} size={rep.index_size}B md5={rep.index_md5}")
    if rep.main_js is not None:
        print(f"main JS: {rep.main_js.name}")
        print(f"         size={rep.main_js_size}B md5={rep.main_js_md5}")
    else:
        print("main JS: <not found>")
    print(f"dist file count: {len(rep.dist_files)}")
    for rel, size in rep.dist_files[:30]:
        print(f"  {size:>10}B  {rel}")
    if len(rep.dist_files) > 30:
        print(f"  ... ({len(rep.dist_files) - 30} more)")
    if rep.notes:
        print("NOTES:")
        for n in rep.notes:
            print(f"  ! {n}")
    print()


# ----------------------------------------------------------------------
# QtWebEngine probe — load the actual HUD URL with full instrumentation.
# ----------------------------------------------------------------------


def run_webengine_probe(
    index: Path,
    *,
    wait_secs: float,
    devtools: bool,
    verbose: bool,
) -> int:
    """Returns exit code: 0 if load reported OK, 1 if not."""
    if devtools:
        os.environ.setdefault("QTWEBENGINE_REMOTE_DEBUGGING", "9222")
        print(
            "[devtools] Chromium remote debugging enabled on port 9222\n"
            "[devtools] Open Chrome on this Mac → chrome://inspect → "
            "click 'inspect' on the page to attach full DevTools.\n"
        )

    # Import each subsystem separately so we can give a precise error
    # if a single symbol is missing rather than a misleading "PySide6
    # not installed". ``QT_VERSION_STR`` in particular varies across
    # PySide6 builds (6.6+ exports it from QtCore, older builds don't);
    # ``qVersion()`` is the universal API.
    try:
        import PySide6  # type: ignore[import-not-found]
        from PySide6.QtCore import QTimer, QUrl, qVersion  # type: ignore[import-not-found]
    except ImportError as e:
        print(
            "ERROR: PySide6 / PySide6.QtCore unavailable.\n"
            "Run:  python3 -m pip install --user 'PySide6>=6.5' 'PySide6-Addons>=6.5'\n"
            f"Underlying error: {e}"
        )
        return 2
    try:
        from PySide6.QtWidgets import QApplication  # type: ignore[import-not-found]
    except ImportError as e:
        print(f"ERROR: PySide6.QtWidgets unavailable: {e}")
        return 2
    try:
        from PySide6.QtWebChannel import QWebChannel  # type: ignore[import-not-found]
    except ImportError as e:
        print(
            "ERROR: PySide6.QtWebChannel unavailable — install the addons "
            f"package:\n  python3 -m pip install --user 'PySide6-Addons>=6.5'\n"
            f"Underlying error: {e}"
        )
        return 2
    try:
        from PySide6.QtWebEngineCore import (  # type: ignore[import-not-found]
            QWebEnginePage,
            QWebEngineSettings,
            QWebEngineUrlRequestInterceptor,
        )
        from PySide6.QtWebEngineWidgets import QWebEngineView  # type: ignore[import-not-found]
    except ImportError as e:
        print(
            "ERROR: QtWebEngine unavailable — install the addons package:\n"
            "  python3 -m pip install --user 'PySide6-Addons>=6.5'\n"
            f"Underlying error: {e}"
        )
        return 2

    pyside_ver = getattr(PySide6, "__version__", "unknown")
    qt_ver = qVersion()
    print("=" * 72)
    print("QT WEBENGINE PROBE")
    print("=" * 72)
    print(f"PySide6 version:  {pyside_ver}")
    print(f"Qt version:       {qt_ver}")
    print(f"sys.platform:     {sys.platform}")
    print(f"sys.version:      {sys.version.split()[0]}")
    print(f"sys.executable:   {sys.executable}")
    print()

    # Console-message severity → printable name.
    LEVEL_NAMES = {
        QWebEnginePage.JavaScriptConsoleMessageLevel.InfoMessageLevel: "INFO ",
        QWebEnginePage.JavaScriptConsoleMessageLevel.WarningMessageLevel: "WARN ",
        QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel: "ERROR",
    }

    console_messages: list[str] = []
    intercepted_requests: list[tuple[str, str, str]] = []  # (method, type, url)

    class _LoggingPage(QWebEnginePage):
        def javaScriptConsoleMessage(  # type: ignore[override] # noqa: N802 — Qt selector
            self, level, message, line_number, source_id  # noqa: ANN001
        ) -> None:
            label = LEVEL_NAMES.get(level, f"L{int(level)}")
            line = f"[console] {label} {message}  ({source_id}:{line_number})"
            console_messages.append(line)
            print(line)

    class _LoggingInterceptor(QWebEngineUrlRequestInterceptor):
        def interceptRequest(self, info) -> None:  # type: ignore[override] # noqa: N802 — Qt selector, ANN001
            try:
                method = bytes(info.requestMethod()).decode("ascii", "replace")
                url = info.requestUrl().toString()
                rt = int(info.resourceType())
            except Exception:
                method, url, rt = "?", "?", -1
            intercepted_requests.append((method, str(rt), url))
            if verbose:
                print(f"[interceptor] {method} type={rt} {url}")

    app = QApplication.instance() or QApplication(sys.argv)

    view = QWebEngineView()
    page = _LoggingPage(view)
    view.setPage(page)

    # Permissive settings to factor out sandboxing as a variable.
    s = page.settings()
    s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
    s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
    s.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
    s.setAttribute(QWebEngineSettings.WebAttribute.ErrorPageEnabled, True)
    s.setAttribute(QWebEngineSettings.WebAttribute.AllowRunningInsecureContent, True)

    # Hook everything BEFORE load.
    interceptor = _LoggingInterceptor()
    page.profile().setUrlRequestInterceptor(interceptor)

    load_state: dict[str, object] = {"finished": False, "ok": None, "info": None}

    def _on_load_finished(ok: bool) -> None:  # noqa: ANN001
        load_state["finished"] = True
        load_state["ok"] = ok
        print(f"[load] loadFinished(bool) ok={ok}")

    page.loadFinished.connect(_on_load_finished)

    # Qt 6.2+ also emits loadFinished with QWebEngineLoadingInfo on the
    # page; try to wire that for the error code + string. Older Qt won't
    # have the overload — silently skip.
    try:
        from PySide6.QtWebEngineCore import QWebEngineLoadingInfo  # type: ignore[import-not-found]

        def _on_loading_info(info: QWebEngineLoadingInfo) -> None:
            try:
                status = int(info.status())
                err = int(info.errorCode())
                err_str = info.errorString()
                url = info.url().toString()
                print(
                    f"[load] LoadingInfo status={status} errorCode={err} "
                    f"errorString={err_str!r} url={url}"
                )
            except Exception as e:
                print(f"[load] LoadingInfo read failed: {e}")

        # The QWebEnginePage.loadingChanged signal carries QWebEngineLoadingInfo.
        page.loadingChanged.connect(_on_loading_info)
    except Exception as e:
        print(f"[load] (no QWebEngineLoadingInfo on this Qt build: {e})")

    # Render-process death is a separate signal.
    def _on_render_terminated(status, exit_code) -> None:  # noqa: ANN001
        print(
            f"[process] renderProcessTerminated status={int(status)} "
            f"exitCode={exit_code}"
        )

    page.renderProcessTerminated.connect(_on_render_terminated)

    # QWebChannel registration — match what HudWindow does so we know
    # whether the channel script can even be retrieved.
    channel = QWebChannel(page)
    page.setWebChannel(channel)

    # Build the URL exactly the way the HUD does.
    url = f"{QUrl.fromLocalFile(str(index)).toString()}#route=hud"
    print(f"[load] loading: {url}")
    view.load(QUrl(url))
    view.resize(420, 640)
    if devtools:
        view.show()  # need a visible window for DevTools session

    # Run the event loop for ``wait_secs`` seconds, then quit.
    QTimer.singleShot(int(wait_secs * 1000), app.quit)
    app.exec()

    print()
    print("=" * 72)
    print("SUMMARY")
    print("=" * 72)
    print(f"loadFinished fired: {load_state['finished']}")
    print(f"loadFinished ok:    {load_state['ok']}")
    print(f"console messages:   {len(console_messages)}")
    print(f"intercepted reqs:   {len(intercepted_requests)}")
    if intercepted_requests and not verbose:
        print("intercepted (URLs only):")
        for _m, _t, u in intercepted_requests:
            print(f"  {u}")

    if console_messages:
        print()
        print("CONSOLE MESSAGES (already printed above as they fired):")
        for line in console_messages:
            print(f"  {line}")

    return 0 if load_state["ok"] is True else 1


# ----------------------------------------------------------------------
# Entry point.
# ----------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Diagnose why the macOS Sayzo HUD page fails to load.",
    )
    p.add_argument(
        "--bundle",
        type=Path,
        default=DEFAULT_BUNDLE,
        help=f"Sayzo bundle path (default: {DEFAULT_BUNDLE})",
    )
    p.add_argument(
        "--index",
        type=Path,
        default=None,
        help="Override the HUD index.html path entirely (skips bundle resolution)",
    )
    p.add_argument(
        "--wait",
        type=float,
        default=DEFAULT_WAIT_SECS,
        help=f"Seconds to wait after load (default: {DEFAULT_WAIT_SECS})",
    )
    p.add_argument(
        "--devtools",
        action="store_true",
        help="Enable Chromium remote debugging on port 9222 (Chrome → chrome://inspect)",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Log every URL request as it fires (noisy)",
    )
    args = p.parse_args(argv)

    if args.index is not None:
        index = args.index
        bundle = index.parent
    else:
        bundle = args.bundle
        index = bundle / DEFAULT_INDEX_REL

    fs = probe_filesystem(bundle, index)
    print_fs_report(fs)

    if not fs.index_exists:
        print("Cannot continue — index.html missing. See FILESYSTEM PROBE above.")
        return 1

    return run_webengine_probe(
        index,
        wait_secs=args.wait,
        devtools=args.devtools,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    sys.exit(main())
