"""Windows-specific foreground + mic-holder queries for the armed model.

Two capabilities exposed to the ArmController:

- ``get_mic_holders()`` → list of processes currently holding an active
  capture session on the default microphone endpoint, via pycaw
  (``IMMDeviceEnumerator`` → ``IAudioSessionManager2`` → sessions).
- ``get_foreground_info()`` → the frontmost window's owning process name,
  the window title, and a heuristic ``is_browser`` flag.

Both are best-effort: on any COM/Win32 failure we log and return empty
results. The ArmController tolerates empties — it just means no whitelist
match fires this poll.
"""
from __future__ import annotations

import logging
from typing import Optional

from .detectors import BROWSER_PROCESS_NAMES, ForegroundInfo, MicHolder

log = logging.getLogger(__name__)


def get_mic_holders() -> list[MicHolder]:
    """Enumerate processes with an active capture session on the default mic.

    Uses pycaw to open the default ``eCapture`` endpoint and iterate its
    audio sessions. Returns empty list on any failure (device absent,
    permission denied, COM error).
    """
    try:
        from pycaw.pycaw import (
            AudioUtilities,
            IAudioSessionControl2,
            IAudioSessionManager2,
            IMMDeviceEnumerator,
        )
        from pycaw.constants import CLSID_MMDeviceEnumerator
        from comtypes import CLSCTX_ALL, CoCreateInstance, GUID
    except Exception:
        log.debug("[arm.win] pycaw unavailable", exc_info=True)
        return []

    # Device role enum values — pycaw doesn't expose these directly.
    EDATAFLOW_CAPTURE = 1
    EROLE_CONSOLE = 0

    import psutil

    try:
        # Initialize COM for the calling thread. Must be done per thread.
        import pythoncom
        try:
            pythoncom.CoInitialize()
        except Exception:
            pass

        enumerator = CoCreateInstance(
            CLSID_MMDeviceEnumerator,
            IMMDeviceEnumerator,
            CLSCTX_ALL,
        )
        device = enumerator.GetDefaultAudioEndpoint(EDATAFLOW_CAPTURE, EROLE_CONSOLE)
        # Activate IAudioSessionManager2 on the capture endpoint.
        mgr = device.Activate(
            IAudioSessionManager2._iid_, CLSCTX_ALL, None,
        )
        session_enum = mgr.GetSessionEnumerator()
        count = session_enum.GetCount()
    except Exception:
        log.debug("[arm.win] capture-endpoint session enum failed", exc_info=True)
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


def get_foreground_info() -> ForegroundInfo:
    """Return a snapshot of the frontmost window's owning process + title.

    Uses Win32 ``GetForegroundWindow`` → ``GetWindowThreadProcessId`` →
    ``psutil`` for the name, and ``GetWindowText`` for the title.
    """
    try:
        import win32gui
        import win32process
        import psutil
    except Exception:
        log.debug("[arm.win] win32 modules unavailable", exc_info=True)
        return ForegroundInfo()

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
    return ForegroundInfo(
        process_name=proc_name,
        window_title=title,
        is_browser=is_browser,
        browser_tab_title=browser_tab_title,
        # URL read on Windows is deferred — the matcher's title-regex fallback
        # handles Google Meet / Teams web for most cases.
        browser_tab_url=None,
    )
