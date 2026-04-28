"""Windows per-process WASAPI loopback capture (requires Windows 10 2004+).

This is the ``system_win.py`` counterpart for per-app capture: instead of
tapping the default-output endpoint (which picks up every app's audio),
we scope the loopback to a specific process tree via
``ActivateAudioInterfaceAsync`` with ``AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK``
and ``PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE``.

``INCLUDE_TARGET_PROCESS_TREE`` covers descendants automatically — Teams /
Zoom / Electron apps that spawn audio-helper subprocesses all get captured
without us enumerating children.

API reference:
- ``ActivateAudioInterfaceAsync`` — mmdevapi.dll, Windows 10 2004 (build
  19041) and later.
- ``AUDIOCLIENT_ACTIVATION_PARAMS`` — struct containing activation type +
  process ID + mode.
- Magic device interface path ``VAD\\Process_Loopback`` identifies the
  virtual process-loopback device (no GUID endpoint lookup needed).

Architecture mirrors ``system_win.py``:
- Blocking WASAPI event loop runs on a dedicated thread.
- Timestamped mono PCM frames land in ``self.queue`` as
  ``(capture_mono_ts, frame)`` tuples, matching SystemCapture's interface.
- Start/stop lifecycle via ``async def start`` / ``async def stop``.

Unavailable on:
- Windows builds below 19041 (pre-2004 Win10; all Win11 is fine).
- Non-Windows platforms (the module import itself fails cleanly).

If loopback activation fails for any reason — unsupported build, access
denied, target PID no longer exists, COM init trouble — ``start()`` raises,
and the caller (``system_win.SystemCapture``) falls back to endpoint-wide
capture for the session. That mirrors today's behavior so nobody loses
audio to a process-loopback misstep.
"""
from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes as wintypes
import logging
import sys
import threading
import time
from math import gcd
from typing import Optional

import numpy as np
from scipy.signal import resample_poly

log = logging.getLogger(__name__)

# Minimum Windows 10 build for AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK.
# Windows 10 version 2004 = build 19041 (May 2020). All Windows 11 builds
# are well above this so there's no Win11-specific gate.
_MIN_WIN_BUILD = 19041

# Device-interface path to pass to ActivateAudioInterfaceAsync. Defined in
# mmdeviceapi.h as VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK.
_VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"

# Activation type + loopback mode enum values (mmdeviceapi.h).
_AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
_PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0

# AUDCLNT_STREAMFLAGS constants (audioclient.h).
_AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
_AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000

# Share mode.
_AUDCLNT_SHAREMODE_SHARED = 0

# 100-nanosecond units per millisecond (REFERENCE_TIME).
_REFTIMES_PER_MS = 10_000

# How many pipeline frames worth of audio to accumulate before resampling.
# Matches system_win.py (25 frames × 20 ms = 500 ms batches).
_RESAMPLE_BATCH_FRAMES = 25

# WAVE_FORMAT_IEEE_FLOAT (mmreg.h). Process loopback clients always
# deliver float32 PCM at the mix format rate, so we ask for that explicitly
# via IAudioClient::GetMixFormat() (populated by WASAPI after Initialize).
_WAVE_FORMAT_IEEE_FLOAT = 0x0003


def is_supported() -> bool:
    """Return True if the current Windows build supports process loopback.

    Cheap check — called once per ``start()`` attempt. ``sys.getwindowsversion``
    returns the kernel's real build number even when the exe has no Win10
    manifest (which ``win32api.GetVersionEx`` would mask to 6.2).
    """
    if sys.platform != "win32":
        return False
    try:
        ver = sys.getwindowsversion()
    except Exception:
        return False
    # Major 10 AND build >= 19041, OR major > 10 (future Windows 11/12).
    if ver.major > 10:
        return True
    if ver.major == 10 and int(getattr(ver, "build", 0)) >= _MIN_WIN_BUILD:
        return True
    return False


# ---------------------------------------------------------------------------
# ctypes structure definitions
# ---------------------------------------------------------------------------


class _AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(ctypes.Structure):
    _fields_ = [
        ("TargetProcessId", wintypes.DWORD),
        ("ProcessLoopbackMode", ctypes.c_int),  # PROCESS_LOOPBACK_MODE enum
    ]


class _ACTIVATION_UNION(ctypes.Union):
    _fields_ = [
        ("ProcessLoopbackParams", _AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS),
    ]


class _AUDIOCLIENT_ACTIVATION_PARAMS(ctypes.Structure):
    _fields_ = [
        ("ActivationType", ctypes.c_int),  # AUDIOCLIENT_ACTIVATION_TYPE enum
        ("Params", _ACTIVATION_UNION),
    ]


class _PROPVARIANT(ctypes.Structure):
    """Minimal PROPVARIANT — we only ever populate it with a BLOB pointing
    at a packed ``AUDIOCLIENT_ACTIVATION_PARAMS``. Apps that need the full
    PROPVARIANT zoo of types (dates, currency, safe arrays) use oleaut32's
    PropVariantInit/Clear helpers; we don't.

    Layout (propidl.h):
      VARTYPE vt, WORD wReserved1, WORD wReserved2, WORD wReserved3  (8 B)
      union { ... BLOB (DWORD cbSize, BYTE* pBlobData) ... }         (16 B on x64)
    ctypes naturally pads cbBlob → 4B gap → pBlobData to match WinABI.
    """

    _fields_ = [
        ("vt", ctypes.c_ushort),
        ("wReserved1", ctypes.c_ushort),
        ("wReserved2", ctypes.c_ushort),
        ("wReserved3", ctypes.c_ushort),
        ("cbBlob", wintypes.ULONG),
        ("pBlobData", ctypes.c_void_p),
    ]


# VT_BLOB = 65 (per propidl.h).
_VT_BLOB = 65


def _make_loopback_waveformat(rate: int = 48000, channels: int = 2):
    """Build the fixed WAVEFORMATEX the process-loopback client requires.

    For process loopback we MUST construct this ourselves: the loopback
    client doesn't implement ``GetMixFormat`` (returns E_NOTIMPL), and
    Initialize requires a fixed format anyway. Microsoft's
    ``ApplicationLoopback`` sample uses 32-bit float, 2-channel, 48 kHz —
    we mirror that exactly so we have a known-good combo.

    ``rate`` must be 44100 or 48000; ``channels`` 1 or 2; bits-per-sample
    is fixed at 32 (Float32). Per MSDN: any other combination is rejected
    by ``IAudioClient::Initialize`` with E_INVALIDARG.

    Constructed via pycaw's ``WAVEFORMATEX`` (identical layout to the
    Windows SDK struct) so ``ctypes.byref(wfx)`` satisfies pycaw's
    ``IAudioClient.Initialize`` signature, which expects
    ``POINTER(WAVEFORMATEX)`` keyed on pycaw's class identity. Constructing
    a private ``ctypes.Structure`` with the same fields raises
    ``ctypes.ArgumentError: expected LP_WAVEFORMATEX instance instead of
    pointer to _WAVEFORMATEX`` because comtypes checks type identity, not
    binary layout.
    """
    # Import locally so module import doesn't fail on platforms without
    # pycaw (it's win32-only via marker in pyproject.toml).
    from pycaw.api.audioclient.depend import WAVEFORMATEX  # type: ignore[import-not-found]

    bits_per_sample = 32
    block_align = channels * bits_per_sample // 8
    return WAVEFORMATEX(
        wFormatTag=_WAVE_FORMAT_IEEE_FLOAT,
        nChannels=channels,
        nSamplesPerSec=rate,
        nAvgBytesPerSec=rate * block_align,
        nBlockAlign=block_align,
        wBitsPerSample=bits_per_sample,
        cbSize=0,
    )


# ---------------------------------------------------------------------------
# COM interface definitions (pure comtypes — no pycaw helpers here because
# pycaw doesn't wrap ActivateAudioInterfaceAsync / process-loopback APIs).
# ---------------------------------------------------------------------------


def _load_comtypes_symbols():
    """Lazy import of comtypes + the interfaces we need.

    Returns a named tuple of the symbols the rest of the module uses, or
    raises ImportError if comtypes isn't available / the Windows SDK headers
    changed in a way we can't adapt to. Caller treats any exception as
    "fall back to endpoint capture".
    """
    import comtypes  # type: ignore[import-not-found]
    from comtypes import GUID, COMMETHOD, IUnknown  # type: ignore[import-not-found]
    from comtypes.hresult import S_OK  # type: ignore[import-not-found]

    # IIDs from Windows SDK headers.
    IID_IActivateAudioInterfaceAsyncOperation = GUID(
        "{72A22D78-CDE4-431D-B8CC-843A71199B6D}"
    )
    IID_IActivateAudioInterfaceCompletionHandler = GUID(
        "{41D949AB-9862-444A-80F6-C261334DA5EB}"
    )
    IID_IAudioClient = GUID("{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}")
    IID_IAudioCaptureClient = GUID("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")
    # IAgileObject — marker interface (no methods) signalling that the COM
    # object is safe to invoke from any apartment without a proxy. Without
    # this, ActivateAudioInterfaceAsync rejects our completion handler
    # with E_ILLEGAL_METHOD_CALL (0x8000000E) up-front because Windows
    # delivers the callback from an arbitrary system thread and refuses to
    # set up cross-apartment marshaling for an MTA-classic-COM object.
    IID_IAgileObject = GUID("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")

    class IActivateAudioInterfaceAsyncOperation(IUnknown):
        _iid_ = IID_IActivateAudioInterfaceAsyncOperation
        _methods_ = [
            COMMETHOD(
                [],
                comtypes.HRESULT,
                "GetActivateResult",
                (["out"], ctypes.POINTER(comtypes.HRESULT), "activateResult"),
                (["out"], ctypes.POINTER(ctypes.POINTER(IUnknown)), "activatedInterface"),
            ),
        ]

    class IActivateAudioInterfaceCompletionHandler(IUnknown):
        _iid_ = IID_IActivateAudioInterfaceCompletionHandler
        _methods_ = [
            COMMETHOD(
                [],
                comtypes.HRESULT,
                "ActivateCompleted",
                (["in"], ctypes.POINTER(IActivateAudioInterfaceAsyncOperation), "activateOperation"),
            ),
        ]

    class IAgileObject(IUnknown):
        _iid_ = IID_IAgileObject
        _methods_: list = []  # marker — no methods of its own

    # IAudioCaptureClient — the interface IAudioClient::GetService(IID_IAudioCaptureClient)
    # gives us. pycaw doesn't ship this one (it only wraps IAudioClient +
    # ISimpleAudioVolume + IChannelAudioVolume), and comtypes' generic
    # IUnknown only exposes AddRef/QueryInterface/Release — so without this
    # declaration, ``capture_client.GetNextPacketSize()`` raises
    # ``AttributeError`` on the very first iteration of the capture loop.
    class IAudioCaptureClient(IUnknown):
        _iid_ = IID_IAudioCaptureClient
        _methods_ = [
            # HRESULT GetBuffer(
            #     BYTE   **ppData,
            #     UINT32  *pNumFramesToRead,
            #     DWORD   *pdwFlags,
            #     UINT64  *pu64DevicePosition,
            #     UINT64  *pu64QPCPosition);
            COMMETHOD(
                [],
                comtypes.HRESULT,
                "GetBuffer",
                (["out"], ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)), "ppData"),
                (["out"], ctypes.POINTER(wintypes.DWORD), "pNumFramesToRead"),
                (["out"], ctypes.POINTER(wintypes.DWORD), "pdwFlags"),
                (["out"], ctypes.POINTER(ctypes.c_uint64), "pu64DevicePosition"),
                (["out"], ctypes.POINTER(ctypes.c_uint64), "pu64QPCPosition"),
            ),
            # HRESULT ReleaseBuffer(UINT32 NumFramesRead);
            COMMETHOD(
                [],
                comtypes.HRESULT,
                "ReleaseBuffer",
                (["in"], wintypes.DWORD, "NumFramesRead"),
            ),
            # HRESULT GetNextPacketSize(UINT32 *pNumFramesInNextPacket);
            COMMETHOD(
                [],
                comtypes.HRESULT,
                "GetNextPacketSize",
                (["out"], ctypes.POINTER(wintypes.DWORD), "pNumFramesInNextPacket"),
            ),
        ]

    return {
        "comtypes": comtypes,
        "IUnknown": IUnknown,
        "S_OK": S_OK,
        "GUID": GUID,
        "IID_IAudioClient": IID_IAudioClient,
        "IID_IAudioCaptureClient": IID_IAudioCaptureClient,
        "IActivateAudioInterfaceAsyncOperation": IActivateAudioInterfaceAsyncOperation,
        "IActivateAudioInterfaceCompletionHandler": IActivateAudioInterfaceCompletionHandler,
        "IAgileObject": IAgileObject,
        "IAudioCaptureClient": IAudioCaptureClient,
    }


def _make_completion_handler(syms, done_event: threading.Event, result_box: dict):
    """Build a concrete ``IActivateAudioInterfaceCompletionHandler`` that
    signals a threading.Event on completion and stashes the IAudioClient
    pointer (or HRESULT error) in ``result_box``.

    Separate function so tests can construct and call it without standing
    up a full activation flow.
    """
    from comtypes import COMObject  # type: ignore[import-not-found]

    class _Handler(COMObject):
        # IAgileObject is a marker — its presence in ``_com_interfaces_``
        # tells comtypes to succeed QueryInterface(IID_IAgileObject), which
        # is what ActivateAudioInterfaceAsync probes to confirm the handler
        # can be called from any thread without proxying. See the IID_…
        # comment in ``_load_comtypes_symbols`` for the full rationale.
        _com_interfaces_ = [
            syms["IActivateAudioInterfaceCompletionHandler"],
            syms["IAgileObject"],
        ]

        def ActivateCompleted(self, activateOperation):
            try:
                hr, iface = activateOperation.GetActivateResult()
                result_box["hr"] = int(hr)
                if int(hr) == 0 and iface:
                    result_box["iface"] = iface
            except Exception as exc:
                result_box["exc"] = exc
            finally:
                done_event.set()
            return syms["S_OK"]

    return _Handler()


# ---------------------------------------------------------------------------
# Public capture class
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Per-PID persistent capture thread (lifetime = agent process)
# ---------------------------------------------------------------------------
#
# WASAPI process-loopback ActivateAudioInterfaceAsync has a fundamental
# limitation: re-activating against the same target PID inside the same
# host process fails — IAudioClient::Initialize on the second activation
# returns E_UNEXPECTED (0x8000FFFF "Catastrophic failure"). Microsoft's
# own ApplicationLoopback sample never re-activates; it activates once
# per program run. We mirror that lifecycle: activate + Initialize each
# PID exactly once per agent-process lifetime, then Start/Stop the same
# IAudioClient across arm/disarm cycles.
#
# Per-PID dedicated thread also avoids cross-apartment COM marshaling —
# all calls on the audio_client / capture_client wrappers happen from the
# same thread that originally created them.
_PERSISTENT_THREADS: dict[int, "_PersistentLoopbackThread"] = {}
_PERSISTENT_THREADS_LOCK = threading.Lock()


def _enqueue_or_drop_static(
    queue: asyncio.Queue, item: tuple[float, np.ndarray], pid: int
) -> None:
    """Loop-thread callback: put a frame on the queue, drop on overflow.

    Module-level (not bound to any class instance) because the
    persistent capture thread outlives any one ``ProcessLoopbackCapture``
    instance — those come and go with each arm cycle. Catching
    QueueFull HERE (inside the call_soon_threadsafe target) is the only
    way to suppress it; wrapping the call_soon_threadsafe call itself
    in try/except doesn't catch it because put_nowait actually runs
    later on the loop thread.
    """
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        log.warning("[proc-loopback] queue full, dropping frame pid=%d", pid)


class _PersistentLoopbackThread:
    """One long-lived capture thread for a single target PID.

    Spawned on first ``activate()`` for the PID and kept alive for the
    agent-process lifetime (daemon=True). Each subsequent activate /
    deactivate pair toggles ``audio_client.Start()`` / ``Stop()``
    instead of calling ActivateAudioInterfaceAsync again — see the
    module-level comment above for why we can't re-activate.

    Thread states:
      - waiting (idle)            ``_wake.wait()`` blocks until activate
      - running (draining audio)  inner loop runs until ``_idle`` is set
      - back to waiting on disarm
    """

    def __init__(self, pid: int, sample_rate: int, frame_ms: int) -> None:
        self.pid = pid
        self.sample_rate = sample_rate
        self.frame_samples = int(sample_rate * frame_ms / 1000)

        # Cycle-control events. ``_wake`` flips to True on activate(),
        # ``_idle`` flips to True on deactivate(). The thread alternates
        # between the two.
        self._wake = threading.Event()
        self._idle = threading.Event()
        # First-time activation result. ``activate()`` blocks on the
        # event; ``_first_activation_ok`` reports success/failure.
        self._first_activation_done = threading.Event()
        self._first_activation_ok = False

        # Per-cycle queue/loop, set by activate() and read by the
        # capture loop. ``_lock`` guards the swap so the thread sees a
        # consistent (queue, loop) pair.
        self._lock = threading.Lock()
        self._queue: Optional[asyncio.Queue[tuple[float, np.ndarray]]] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Spawned lazily on first activate(); ``daemon=True`` so the OS
        # tears it down on agent exit (no explicit shutdown path).
        self._thread: Optional[threading.Thread] = None

    def activate(
        self,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue,
        timeout_secs: float = 5.0,
    ) -> bool:
        """Resume / start delivering frames to ``queue``.

        Spawns the persistent thread on the very first call. Subsequent
        calls just signal the running thread to resume. Blocks up to
        ``timeout_secs`` only on the first call (waiting for WASAPI
        Activate + Initialize to complete). Returns True on success.
        """
        with self._lock:
            self._loop = loop
            self._queue = queue
        self._idle.clear()
        self._wake.set()

        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run,
                name=f"proc-loopback-pid{self.pid}",
                daemon=True,
            )
            self._thread.start()

        # First-time activation has to finish before we can claim
        # success. ``_first_activation_done`` is set by the thread once
        # ActivateAudioInterfaceAsync + Initialize either complete or
        # fail. After that, every subsequent activate() returns
        # immediately because the event is already set.
        if not self._first_activation_done.wait(timeout=timeout_secs):
            return False
        return self._first_activation_ok

    def deactivate(self) -> None:
        """Stop delivering frames; thread stays alive."""
        self._wake.clear()
        self._idle.set()

    def _run(self) -> None:  # noqa: C901 — long but linear
        # CoInit. comtypes auto-inits this thread to STA on first import;
        # explicit MTA call may trip RPC_E_CHANGED_MODE — that's fine,
        # STA works for our usage (single-thread access, IAgileObject
        # completion handler).
        try:
            import comtypes  # type: ignore[import-not-found]
            _RPC_E_CHANGED_MODE = -2147417850
            try:
                comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
            except OSError as e:
                if getattr(e, "winerror", 0) != _RPC_E_CHANGED_MODE:
                    log.exception(
                        "[proc-loopback] CoInitializeEx failed pid=%d", self.pid,
                    )
                    self._first_activation_done.set()
                    return
                log.debug(
                    "[proc-loopback] thread already CoInit'd, continuing pid=%d",
                    self.pid,
                )
        except Exception:
            log.exception("[proc-loopback] comtypes import failed pid=%d", self.pid)
            self._first_activation_done.set()
            return

        # First (and only) WASAPI activation + initialization for this PID.
        try:
            syms = _load_comtypes_symbols()
        except Exception:
            log.exception("[proc-loopback] comtypes symbol load failed pid=%d", self.pid)
            self._first_activation_done.set()
            return

        try:
            audio_client = _activate_process_loopback_client(syms, self.pid)
        except Exception:
            log.exception("[proc-loopback] activation failed pid=%d", self.pid)
            self._first_activation_done.set()
            return
        if audio_client is None:
            log.warning("[proc-loopback] no IAudioClient returned pid=%d", self.pid)
            self._first_activation_done.set()
            return

        native_rate = 48000
        n_channels = 2
        wfx = _make_loopback_waveformat(rate=native_rate, channels=n_channels)
        buffer_duration_hns = 200 * _REFTIMES_PER_MS
        try:
            audio_client.Initialize(
                _AUDCLNT_SHAREMODE_SHARED,
                _AUDCLNT_STREAMFLAGS_LOOPBACK | _AUDCLNT_STREAMFLAGS_EVENTCALLBACK,
                buffer_duration_hns,
                0,
                ctypes.byref(wfx),
                None,
            )
        except Exception:
            log.exception("[proc-loopback] Initialize failed pid=%d", self.pid)
            self._first_activation_done.set()
            return

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateEventW.argtypes = [
            ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR,
        ]
        kernel32.CreateEventW.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        event_handle = kernel32.CreateEventW(None, False, False, None)
        if not event_handle:
            log.error("[proc-loopback] CreateEventW failed pid=%d", self.pid)
            self._first_activation_done.set()
            return

        try:
            audio_client.SetEventHandle(event_handle)
        except Exception:
            log.exception("[proc-loopback] SetEventHandle failed pid=%d", self.pid)
            self._first_activation_done.set()
            return

        try:
            capture_client_iunk = audio_client.GetService(syms["IID_IAudioCaptureClient"])
        except Exception:
            log.exception(
                "[proc-loopback] GetService(IAudioCaptureClient) failed pid=%d", self.pid,
            )
            self._first_activation_done.set()
            return
        if capture_client_iunk is None:
            log.error("[proc-loopback] GetService returned NULL pid=%d", self.pid)
            self._first_activation_done.set()
            return

        try:
            capture_client_ptr = capture_client_iunk.QueryInterface(
                syms["IAudioCaptureClient"]
            )
        except Exception:
            log.exception(
                "[proc-loopback] QueryInterface(IAudioCaptureClient) failed pid=%d", self.pid,
            )
            self._first_activation_done.set()
            return

        log.info("[proc-loopback] activated client for pid=%d (persistent)", self.pid)

        # Resampling setup — same constants regardless of how many
        # arm cycles we run; native format is fixed to Float32 / 2-ch / 48 kHz.
        g = gcd(native_rate, self.sample_rate)
        up = self.sample_rate // g
        down = native_rate // g
        need_resample = native_rate != self.sample_rate
        native_samples_per_frame = self.frame_samples * down // up
        batch_native_samples = native_samples_per_frame * _RESAMPLE_BATCH_FRAMES
        batch_duration = batch_native_samples / native_rate

        log.info(
            "[proc-loopback] pid=%d native_sr=%d ch=%d target_sr=%d "
            "resample=%d/%d batch=%d frames batch_dur=%.3fs",
            self.pid, native_rate, n_channels, self.sample_rate, up, down,
            _RESAMPLE_BATCH_FRAMES, batch_duration,
        )

        # First-activation success signaled. From here on, the thread
        # only ever toggles Start/Stop on the same audio_client.
        self._first_activation_ok = True
        self._first_activation_done.set()

        WAIT_TIMEOUT = 0x00000102
        WAIT_OBJECT_0 = 0x00000000

        # Outer loop: wait for activate, drain until deactivate, repeat.
        # The thread only exits when the process dies (daemon=True).
        while True:
            # Block until next activate(). On first iteration ``_wake``
            # is already set by activate() before the thread reached
            # this point.
            self._wake.wait()
            self._idle.clear()

            try:
                audio_client.Start()
            except Exception:
                log.exception("[proc-loopback] Start failed pid=%d", self.pid)
                self._wake.clear()
                self._idle.set()
                continue

            accumulator: list[np.ndarray] = []
            accumulator_samples = 0
            accumulator_first_mono: Optional[float] = None

            try:
                while not self._idle.is_set():
                    wait_result = kernel32.WaitForSingleObject(event_handle, 100)
                    if wait_result == WAIT_TIMEOUT:
                        continue
                    if wait_result != WAIT_OBJECT_0:
                        log.warning(
                            "[proc-loopback] WaitForSingleObject returned 0x%x pid=%d",
                            wait_result, self.pid,
                        )
                        continue

                    while True:
                        try:
                            packet_length = capture_client_ptr.GetNextPacketSize()
                        except Exception:
                            log.exception(
                                "[proc-loopback] GetNextPacketSize failed pid=%d", self.pid,
                            )
                            packet_length = 0
                        if packet_length == 0:
                            break

                        try:
                            data_ptr, frames_available, flags, _pos, _qpc = (
                                capture_client_ptr.GetBuffer()
                            )
                        except Exception:
                            log.exception(
                                "[proc-loopback] GetBuffer failed pid=%d", self.pid,
                            )
                            break

                        mono_at_read = time.monotonic()
                        packet_mono_first = (
                            mono_at_read - (frames_available / native_rate)
                        )

                        try:
                            sample_count = int(frames_available) * n_channels
                            if sample_count > 0:
                                arr_type = ctypes.c_float * sample_count
                                arr = arr_type.from_address(
                                    ctypes.cast(data_ptr, ctypes.c_void_p).value or 0
                                )
                                samples = np.ctypeslib.as_array(arr).copy()
                                if n_channels > 1:
                                    samples = samples.reshape(-1, n_channels).mean(axis=1)
                                if accumulator_first_mono is None:
                                    accumulator_first_mono = packet_mono_first
                                accumulator.append(samples)
                                accumulator_samples += samples.shape[0]
                        finally:
                            try:
                                capture_client_ptr.ReleaseBuffer(frames_available)
                            except Exception:
                                log.debug(
                                    "[proc-loopback] ReleaseBuffer failed pid=%d",
                                    self.pid, exc_info=True,
                                )

                    while accumulator_samples >= batch_native_samples:
                        if accumulator_first_mono is None:
                            break
                        full = (
                            np.concatenate(accumulator)
                            if len(accumulator) > 1
                            else accumulator[0]
                        )
                        batch_native = full[:batch_native_samples]
                        remainder = full[batch_native_samples:]
                        batch_first_mono = accumulator_first_mono
                        if remainder.size > 0:
                            accumulator = [remainder]
                            accumulator_samples = int(remainder.shape[0])
                            accumulator_first_mono = (
                                batch_first_mono + batch_native_samples / native_rate
                            )
                        else:
                            accumulator = []
                            accumulator_samples = 0
                            accumulator_first_mono = None

                        if need_resample:
                            resampled = resample_poly(batch_native, up, down).astype(
                                np.float32
                            )
                        else:
                            resampled = batch_native.astype(np.float32, copy=False)

                        with self._lock:
                            queue = self._queue
                            loop = self._loop
                        if queue is None or loop is None:
                            break

                        pos = 0
                        while pos + self.frame_samples <= len(resampled):
                            frame = resampled[pos : pos + self.frame_samples]
                            frame_mono = batch_first_mono + (pos / self.sample_rate)
                            pos += self.frame_samples
                            loop.call_soon_threadsafe(
                                _enqueue_or_drop_static,
                                queue, (frame_mono, frame), self.pid,
                            )
            finally:
                try:
                    audio_client.Stop()
                except Exception:
                    pass
                # Reset the wake event so the next activate() call has
                # to set it explicitly. Without this clear, _wake would
                # still be True from this cycle and the next activate's
                # wait() would return immediately even before the
                # caller signals.
                self._wake.clear()
            log.info("[proc-loopback] paused pid=%d", self.pid)


class ProcessLoopbackCapture:
    """WASAPI process-loopback capture scoped to a set of target PIDs.

    Mirrors :class:`sayzo_agent.capture.system_win.SystemCapture`'s public
    interface — ``async def start`` / ``async def stop`` plus a
    ``queue: asyncio.Queue[tuple[float, np.ndarray]]`` of mono pipeline
    frames.

    Internally backed by per-PID persistent capture threads (see
    ``_PersistentLoopbackThread``) so re-arming against the same PID
    doesn't re-activate WASAPI — re-activation fails on Windows with
    E_UNEXPECTED. The persistent thread activates each PID exactly
    once per agent-process lifetime and toggles Start/Stop on subsequent
    arm/disarm cycles.
    """

    def __init__(
        self,
        target_pids: tuple[int, ...],
        *,
        sample_rate: int = 16_000,
        frame_ms: int = 20,
        queue_maxsize: int = 200,
        queue: Optional[asyncio.Queue[tuple[float, np.ndarray]]] = None,
    ) -> None:
        if not target_pids:
            raise ValueError("ProcessLoopbackCapture requires at least one target PID")
        self.target_pids = tuple(int(p) for p in target_pids if p > 0)
        if not self.target_pids:
            raise ValueError("ProcessLoopbackCapture: no valid PIDs after filtering")

        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.frame_samples = int(sample_rate * frame_ms / 1000)
        self.frame_duration = self.frame_samples / sample_rate
        # Allow the caller (SystemCapture) to inject its own queue so
        # external consumers that already hold a reference to it keep
        # receiving frames. Without this, SystemCapture used to do
        # ``self.queue = delegate.queue`` after start(), leaving every
        # _consume task in app.py reading the now-orphaned original queue
        # while real audio piled up in the delegate's queue and overflowed.
        self.queue: asyncio.Queue[tuple[float, np.ndarray]] = (
            queue if queue is not None else asyncio.Queue(maxsize=queue_maxsize)
        )

        # PIDs successfully activated this session — used by stop() to
        # call deactivate on exactly the threads we activated, no more.
        self._activated_pids: list[int] = []

    # ---- lifecycle --------------------------------------------------------

    async def start(self, *, target_pids: Optional[tuple[int, ...]] = None) -> None:
        """Activate (or wake) the persistent capture thread for each PID.

        First call for a given PID spawns the persistent thread and
        does the WASAPI Activate + Initialize. Subsequent calls just
        toggle the audio_client from Stopped → Started.
        """
        if target_pids and tuple(target_pids) != self.target_pids:
            raise ValueError(
                "ProcessLoopbackCapture.start target_pids disagrees with the "
                "constructor; create a new instance instead of mutating PIDs."
            )
        if not is_supported():
            raise RuntimeError(
                "WASAPI process loopback requires Windows 10 build 19041 "
                "(version 2004, May 2020) or newer"
            )

        loop = asyncio.get_running_loop()

        successes = 0
        for pid in self.target_pids:
            with _PERSISTENT_THREADS_LOCK:
                pcap = _PERSISTENT_THREADS.get(pid)
                if pcap is None:
                    pcap = _PersistentLoopbackThread(
                        pid, self.sample_rate, self.frame_ms,
                    )
                    _PERSISTENT_THREADS[pid] = pcap

            # First-time activation blocks up to 5 s on WASAPI work, so
            # marshal it onto a worker thread to keep the event loop free.
            ok = await asyncio.to_thread(pcap.activate, loop, self.queue, 5.0)
            if ok:
                self._activated_pids.append(pid)
                successes += 1
            else:
                log.warning(
                    "[proc-loopback] activation failed pid=%d", pid,
                )

        if successes == 0:
            raise RuntimeError(
                f"process-loopback activation failed for all {len(self.target_pids)} PID(s)"
            )
        if successes < len(self.target_pids):
            log.warning(
                "[proc-loopback] only %d/%d target PIDs activated successfully",
                successes, len(self.target_pids),
            )

    async def stop(self) -> None:
        """Pause the persistent thread(s); they stay alive for next arm."""
        for pid in self._activated_pids:
            with _PERSISTENT_THREADS_LOCK:
                pcap = _PERSISTENT_THREADS.get(pid)
            if pcap is not None:
                pcap.deactivate()
        self._activated_pids = []


# ---------------------------------------------------------------------------
# Activation helper (sits outside the class so unit tests can mock it)
# ---------------------------------------------------------------------------


def _activate_process_loopback_client(syms: dict, pid: int, *, timeout_secs: float = 5.0):
    """Call ActivateAudioInterfaceAsync + wait for the completion handler.

    Returns an IAudioClient (cast from the IUnknown the activation returns),
    or None if activation failed. Raises on COM-init / missing-DLL errors
    so the caller can distinguish "doesn't work here" from "this one PID
    didn't activate."
    """
    mmdevapi = ctypes.OleDLL("mmdevapi.dll")

    ActivateAudioInterfaceAsync = mmdevapi.ActivateAudioInterfaceAsync
    ActivateAudioInterfaceAsync.argtypes = [
        wintypes.LPCWSTR,          # deviceInterfacePath
        ctypes.c_void_p,           # REFIID
        ctypes.c_void_p,           # PROPVARIANT* activationParams
        ctypes.c_void_p,           # IActivateAudioInterfaceCompletionHandler*
        ctypes.c_void_p,           # IActivateAudioInterfaceAsyncOperation**
    ]
    ActivateAudioInterfaceAsync.restype = ctypes.HRESULT

    # Pack AUDIOCLIENT_ACTIVATION_PARAMS → PROPVARIANT(BLOB).
    params = _AUDIOCLIENT_ACTIVATION_PARAMS()
    params.ActivationType = _AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
    params.Params.ProcessLoopbackParams.TargetProcessId = wintypes.DWORD(pid)
    params.Params.ProcessLoopbackParams.ProcessLoopbackMode = (
        _PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE
    )

    pv = _PROPVARIANT()
    pv.vt = _VT_BLOB
    pv.cbBlob = ctypes.sizeof(params)
    pv.pBlobData = ctypes.cast(ctypes.pointer(params), ctypes.c_void_p)

    done_event = threading.Event()
    result_box: dict = {}

    handler = _make_completion_handler(syms, done_event, result_box)

    async_op_ptr = ctypes.c_void_p(0)

    # handler is a comtypes COMObject — we need the raw pointer to the
    # completion-handler interface. ``ctypes.cast(..., c_void_p)`` reads
    # the address out of the comtypes wrapper; ``int()`` doesn't work
    # here because comtypes interface objects marshal to raw bytes, not
    # a Python int (observed in field logs: ValueError invalid literal
    # for int() with base 10: b'\\x18\\x06\\xf0\\xc4O\\x02\\x00\\x00').
    handler_iface = handler.QueryInterface(syms["IActivateAudioInterfaceCompletionHandler"])
    handler_ptr = ctypes.cast(handler_iface, ctypes.c_void_p)

    hr = ActivateAudioInterfaceAsync(
        _VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
        ctypes.byref(syms["IID_IAudioClient"]),
        ctypes.byref(pv),
        handler_ptr,
        ctypes.byref(async_op_ptr),
    )
    if hr != 0:
        raise OSError(f"ActivateAudioInterfaceAsync HRESULT=0x{hr & 0xFFFFFFFF:08x}")

    # Wait for the completion handler.
    if not done_event.wait(timeout=timeout_secs):
        log.warning(
            "[proc-loopback] activation timeout after %.1fs pid=%d",
            timeout_secs, pid,
        )
        return None

    if "exc" in result_box:
        raise result_box["exc"]
    if int(result_box.get("hr", -1)) != 0:
        log.warning(
            "[proc-loopback] activation HRESULT=0x%08x pid=%d",
            int(result_box["hr"]) & 0xFFFFFFFF, pid,
        )
        return None

    iface = result_box.get("iface")
    if iface is None:
        return None
    # Cast IUnknown → IAudioClient. We need pycaw's IAudioClient for the
    # Initialize/GetMixFormat/Start methods — dynamically import so the
    # module still loads on non-Windows platforms.
    from pycaw.pycaw import IAudioClient  # type: ignore[import-not-found]
    return iface.QueryInterface(IAudioClient)
