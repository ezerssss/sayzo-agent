"""Windows-specific foreground + mic-holder queries for the armed model.

Capabilities exposed to the ArmController:

- ``get_mic_holders()`` → list of processes currently holding an active
  capture session on the default microphone endpoint, via pycaw
  (``IMMDeviceEnumerator`` → ``IAudioSessionManager2`` → sessions).
- ``get_foreground_info()`` → the frontmost window's owning process name,
  the window title, a heuristic ``is_browser`` flag, and (for browsers)
  the active tab URL read via UI Automation.
- ``get_browser_window_titles()`` / ``get_browser_window_urls()`` →
  parallel lists of titles + active-tab URLs for every visible browser
  window. Needed so the whitelist matcher can still attribute a mic hold
  to the right meeting tab when the user Alt+Tab'd away from the browser.

All queries are best-effort: on any COM/Win32/UIA failure we log and
return empty / None. The ArmController tolerates empties — it just means
no whitelist match fires this poll.

**COM-apartment isolation**: pycaw (``get_mic_holders``) and UI Automation
(``get_browser_tab_url``) both MUST run on a thread whose apartment is STA
(apartment-threaded), because comtypes 1.4.x calls ``CoInitializeEx(STA)``
at module-import time and will raise ``RPC_E_CHANGED_MODE`` if the thread
was already initialized to MTA. The service process's main thread gets
MTA'd early by pystray/pywebview, so we can't import comtypes there.
Solution: a dedicated single-worker ``ThreadPoolExecutor`` whose
initializer calls ``pythoncom.CoInitialize()`` (STA) before any comtypes
import occurs on that thread. Both mic-holder queries and UIA URL reads
run through that executor (serialized — one at a time — which is fine at
the watcher's 2 s poll cadence).
"""
from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from .detectors import BROWSER_PROCESS_NAMES, ForegroundInfo, MicHolder

log = logging.getLogger(__name__)


_com_executor: Optional[ThreadPoolExecutor] = None
_com_executor_lock = threading.Lock()


# Per-HWND URL cache. Populated by ``get_browser_tab_url``; entries age out
# after ``_URL_CACHE_TTL_SECS`` and stale entries are GC'd by
# ``get_browser_window_urls`` each call (so long-running agents don't grow
# an unbounded map). MUST be shorter than the watcher's 2 s poll interval
# so a tab-switch is reflected on the very next poll instead of one poll
# later — at TTL >= poll, the next poll lands inside the still-valid
# window and returns the previous tab's URL, lagging the consent toast
# 2.5–4.5 s after the user switched tabs (user report 2026-04-29). 1.5 s
# is still long enough to dedup the foreground URL read against the
# window-list URL read inside one watcher tick (~50 ms apart).
_URL_CACHE_TTL_SECS = 1.5
_url_cache_lock = threading.Lock()
_url_cache: dict[int, tuple[float, Optional[str]]] = {}


# Known browser URL schemes (used by ``_looks_url_ish`` to decide whether
# an Edit control's value looks like an address-bar URL). Covers the
# browsers in ``BROWSER_PROCESS_NAMES``. ``view-source:`` is included
# because Chrome/Edge's ValuePattern returns the full scheme for it.
_URL_SCHEMES = (
    "http://", "https://", "file://", "about:",
    "chrome://", "edge://", "brave://", "opera://", "vivaldi://", "arc://",
    "view-source:",
)

# Matches bare host strings Chrome/Edge show after stripping the ``https://``
# prefix from the visible omnibox — e.g. ``meet.google.com/abc-defg-hij``,
# ``github.com``, ``example.co.uk/path``. Firefox keeps the scheme so this
# fallback is mostly for Chromium browsers.
_BARE_HOST_RE = re.compile(
    r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9-]+)+(:\d+)?(/.*)?$",
    re.IGNORECASE,
)


def _com_thread_initializer() -> None:
    """Runs once on the COM worker thread before any submitted task.

    Initializes the thread's apartment to STA so comtypes's module-level
    ``CoInitializeEx`` (done the first time pycaw is imported here) agrees
    with the existing mode and returns ``S_FALSE`` instead of raising
    ``RPC_E_CHANGED_MODE``. Without this, pycaw's import would fail inside
    the bundled service because the main thread is already MTA.
    """
    try:
        import pythoncom
        pythoncom.CoInitialize()
    except Exception:
        log.warning(
            "[arm.win] COM worker thread init failed — mic detection disabled",
            exc_info=True,
        )


def _get_com_executor() -> ThreadPoolExecutor:
    global _com_executor
    with _com_executor_lock:
        if _com_executor is None:
            _com_executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="sayzo-com",
                initializer=_com_thread_initializer,
            )
        return _com_executor


def _mic_holders_on_com_thread() -> list[MicHolder]:
    """The real mic-holder query. Must run on the COM worker thread because
    the comtypes objects returned here (IMMDevice, IAudioSessionManager2,
    session enumerator, individual sessions) are apartment-thread-affine
    and can only be used on the thread they were created on."""
    try:
        from pycaw.pycaw import (
            IAudioSessionControl2,
            IAudioSessionManager2,
            IMMDeviceEnumerator,
        )
        from pycaw.constants import CLSID_MMDeviceEnumerator
        from comtypes import CLSCTX_ALL, CoCreateInstance
    except Exception:
        log.warning(
            "[arm.win] pycaw/comtypes import failed on COM worker thread — "
            "meeting detection disabled",
            exc_info=True,
        )
        return []

    # Device role enum values — pycaw doesn't expose these directly.
    EDATAFLOW_CAPTURE = 1
    EROLE_CONSOLE = 0

    import psutil

    try:
        enumerator = CoCreateInstance(
            CLSID_MMDeviceEnumerator,
            IMMDeviceEnumerator,
            CLSCTX_ALL,
        )
        device = enumerator.GetDefaultAudioEndpoint(EDATAFLOW_CAPTURE, EROLE_CONSOLE)
        # IMMDevice.Activate returns an IUnknown pointer in comtypes; cast to
        # the real interface before calling its methods or .GetSessionEnumerator
        # raises AttributeError.
        raw = device.Activate(IAudioSessionManager2._iid_, CLSCTX_ALL, None)
        mgr = raw.QueryInterface(IAudioSessionManager2)
        session_enum = mgr.GetSessionEnumerator()
        count = session_enum.GetCount()
    except Exception:
        log.warning(
            "[arm.win] capture-endpoint session enum failed",
            exc_info=True,
        )
        return []

    holders: list[MicHolder] = []
    for i in range(count):
        try:
            ctrl = session_enum.GetSession(i)
            ctrl2 = ctrl.QueryInterface(IAudioSessionControl2)
            # State: 0 Inactive, 1 Active, 2 Expired. We want Active.
            state = ctrl.GetState()
            if state != 1:
                continue
            pid = ctrl2.GetProcessId()
            if pid <= 0:
                continue
            try:
                name = psutil.Process(pid).name()
            except Exception:
                name = ""
            if name:
                holders.append(MicHolder(process_name=name, pid=pid))
        except Exception:
            log.debug("[arm.win] session %d inspect failed", i, exc_info=True)
            continue

    return holders


def get_mic_holders() -> list[MicHolder]:
    """Enumerate processes with an active capture session on the default mic.

    Submits the query to the COM worker thread (see module docstring).
    Returns empty list on any failure — the ArmController tolerates that.
    """
    try:
        fut = _get_com_executor().submit(_mic_holders_on_com_thread)
        # 2 s matches the watcher's poll interval; if a single query takes
        # longer than a full poll, something is badly wrong and we'd rather
        # skip this round than stack queries.
        return fut.result(timeout=2.0)
    except Exception:
        log.warning(
            "[arm.win] mic-holder worker call failed — meeting detection "
            "skipped this poll",
            exc_info=True,
        )
        return []


def get_foreground_info() -> ForegroundInfo:
    """Return a snapshot of the frontmost window's owning process + title.

    Uses Win32 ``GetForegroundWindow`` → ``GetWindowThreadProcessId`` →
    ``psutil`` for the name, and ``GetWindowText`` for the title. When the
    foreground process is a known browser, the active tab's URL is read via
    UI Automation (``get_browser_tab_url``) so user-added web detector
    URL patterns can match without a hand-authored title fallback.
    """
    try:
        import win32gui
        import win32process
        import psutil
    except Exception:
        log.debug("[arm.win] win32 modules unavailable", exc_info=True)
        return ForegroundInfo()

    hwnd = 0
    title: Optional[str] = None
    proc_name: Optional[str] = None
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return ForegroundInfo()
        title = win32gui.GetWindowText(hwnd) or None
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid <= 0:
            return ForegroundInfo(window_title=title)
        try:
            proc_name = psutil.Process(pid).name()
        except Exception:
            proc_name = None
    except Exception:
        log.debug("[arm.win] foreground query failed", exc_info=True)
        return ForegroundInfo()

    is_browser = bool(proc_name and proc_name.lower() in BROWSER_PROCESS_NAMES)
    browser_tab_title: Optional[str] = title if is_browser else None

    browser_tab_url: Optional[str] = None
    if is_browser and hwnd:
        try:
            browser_tab_url = get_browser_tab_url(hwnd)
        except Exception:
            log.debug("[arm.win] foreground URL read failed", exc_info=True)
            browser_tab_url = None

    return ForegroundInfo(
        process_name=proc_name,
        window_title=title,
        is_browser=is_browser,
        browser_tab_title=browser_tab_title,
        browser_tab_url=browser_tab_url,
    )


def get_browser_window_titles() -> list[str]:
    """Return visible top-level window titles owned by any browser process.

    Needed so the matcher can find a Meet / Teams / Zoom-web window even
    when the user Alt+Tabs away from the browser. The pycaw mic-session
    attribution tells us a browser is capturing, but the active window is
    whatever the user is looking at — so we enumerate every browser window
    and let the matcher pick whichever title satisfies a detector spec.
    """
    try:
        import win32gui
        import win32process
        import psutil
    except Exception:
        log.debug("[arm.win] win32/psutil unavailable", exc_info=True)
        return []

    titles: list[str] = []
    pid_name_cache: dict[int, str] = {}

    def _cb(hwnd: int, _: object) -> None:
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            t = win32gui.GetWindowText(hwnd)
            if not t:
                return
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid <= 0:
                return
            name = pid_name_cache.get(pid)
            if name is None:
                try:
                    name = psutil.Process(pid).name() or ""
                except Exception:
                    name = ""
                pid_name_cache[pid] = name
            if name.lower() in BROWSER_PROCESS_NAMES:
                titles.append(t)
        except Exception:
            return

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        log.debug("[arm.win] EnumWindows failed", exc_info=True)
        return []
    return titles


# ---- active-tab URL read via UI Automation ------------------------------


def _looks_url_ish(s: str) -> bool:
    """True if ``s`` looks like the value a browser would show in the
    address bar. Matches both fully-qualified URLs (``https://...``) and
    the bare-host form Chrome/Edge display after stripping ``https://``.
    """
    s = s.strip()
    if not s:
        return False
    if s.startswith(_URL_SCHEMES):
        return True
    return bool(_BARE_HOST_RE.match(s))


def _normalize_url(val: str) -> str:
    """Re-attach an ``https://`` scheme when Chrome/Edge stripped it for
    display. Leaves other schemes untouched so e.g. ``chrome://settings``
    stays readable but never matches a user pattern like ``^https://``.
    """
    val = val.strip()
    if val.startswith(_URL_SCHEMES):
        return val
    return "https://" + val


def _find_browser_url_in_tree(root, auto_mod) -> Optional[str]:
    """Depth-limited BFS for an Edit control whose ValuePattern value
    looks like a URL. The address bar is the first URL-valued Edit in
    Chrome / Edge / Firefox / Chromium-derivative trees; depth 8 + a
    visit cap keep walks bounded even on heavy pages whose DOM exposes
    thousands of a11y nodes.
    """
    from collections import deque
    try:
        edit_type = auto_mod.ControlType.EditControl
    except Exception:
        return None

    max_depth = 8
    max_visited = 300
    q = deque([(root, 0)])
    visited = 0
    while q and visited < max_visited:
        elem, depth = q.popleft()
        visited += 1
        try:
            is_edit = False
            try:
                is_edit = elem.ControlType == edit_type
            except Exception:
                is_edit = False
            if is_edit:
                val = ""
                try:
                    vp = elem.GetValuePattern()
                    if vp is not None:
                        val = vp.Value or ""
                except Exception:
                    val = ""
                if val and _looks_url_ish(val):
                    return _normalize_url(val)
            if depth < max_depth:
                try:
                    children = elem.GetChildren()
                except Exception:
                    children = []
                for child in children:
                    q.append((child, depth + 1))
        except Exception:
            continue
    return None


def _tab_url_on_com_thread(hwnd: int) -> Optional[str]:
    """UIA-based active-tab URL read. Must run on the COM worker thread
    so its apartment is STA (same constraint as pycaw). Returns None on
    any failure — caller treats None as "try the title fallback."
    """
    try:
        import uiautomation as auto
    except Exception:
        log.debug("[arm.win] uiautomation import failed", exc_info=True)
        return None

    try:
        root = auto.ControlFromHandle(hwnd)
    except Exception:
        log.debug("[arm.win] ControlFromHandle failed for hwnd=%s", hwnd, exc_info=True)
        return None
    if root is None:
        return None
    try:
        return _find_browser_url_in_tree(root, auto)
    except Exception:
        log.debug("[arm.win] UIA tree walk failed for hwnd=%s", hwnd, exc_info=True)
        return None


def get_browser_tab_url(hwnd: int) -> Optional[str]:
    """Return the active tab URL for the browser window at ``hwnd``, or None.

    Cached per-HWND for ``_URL_CACHE_TTL_SECS`` so the foreground read and
    the window-list read share a single UIA walk within one watcher tick.
    Slow path submits to the COM worker executor (same one as pycaw) and
    blocks up to 2 s.
    """
    if not hwnd:
        return None
    now = time.monotonic()
    with _url_cache_lock:
        entry = _url_cache.get(hwnd)
        if entry is not None and now - entry[0] < _URL_CACHE_TTL_SECS:
            return entry[1]
    try:
        fut = _get_com_executor().submit(_tab_url_on_com_thread, hwnd)
        url = fut.result(timeout=2.0)
    except Exception:
        log.debug("[arm.win] tab-URL worker call failed", exc_info=True)
        url = None
    with _url_cache_lock:
        _url_cache[hwnd] = (time.monotonic(), url)
    return url


def get_browser_window_urls() -> list[str]:
    """Return active-tab URLs for every visible browser window, in no
    particular order. Empty list on any failure.

    Parallel to ``get_browser_window_titles`` so the matcher can attribute
    a browser mic-hold to the right meeting tab even when the user
    Alt+Tab'd to a non-browser. Each URL goes through the per-HWND cache
    so a single watcher tick never hits UIA twice for the same window.
    """
    try:
        import win32gui
        import win32process
        import psutil
    except Exception:
        log.debug("[arm.win] win32/psutil unavailable", exc_info=True)
        return []

    hwnds: list[int] = []
    pid_name_cache: dict[int, str] = {}

    def _cb(hwnd: int, _: object) -> None:
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            # Skip chromeless helper windows (no title = likely invisible
            # compositor / tab-drag handle / DevTools shim with no omnibox).
            if not win32gui.GetWindowText(hwnd):
                return
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid <= 0:
                return
            name = pid_name_cache.get(pid)
            if name is None:
                try:
                    name = psutil.Process(pid).name() or ""
                except Exception:
                    name = ""
                pid_name_cache[pid] = name
            if name.lower() in BROWSER_PROCESS_NAMES:
                hwnds.append(hwnd)
        except Exception:
            return

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        log.debug("[arm.win] EnumWindows failed (urls)", exc_info=True)
        return []

    urls: list[str] = []
    for hwnd in hwnds:
        url = get_browser_tab_url(hwnd)
        if url:
            urls.append(url)

    # Drop cache entries for HWNDs that no longer belong to a visible
    # browser window. Without this the cache grows every time the user
    # opens + closes a browser window over a long session.
    live = set(hwnds)
    with _url_cache_lock:
        for dead in [h for h in _url_cache if h not in live]:
            _url_cache.pop(dead, None)

    return urls
