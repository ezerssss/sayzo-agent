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

    return {
        "comtypes": comtypes,
        "IUnknown": IUnknown,
        "S_OK": S_OK,
        "GUID": GUID,
        "IID_IAudioClient": IID_IAudioClient,
        "IActivateAudioInterfaceAsyncOperation": IActivateAudioInterfaceAsyncOperation,
        "IActivateAudioInterfaceCompletionHandler": IActivateAudioInterfaceCompletionHandler,
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
        _com_interfaces_ = [syms["IActivateAudioInterfaceCompletionHandler"]]

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


class ProcessLoopbackCapture:
    """WASAPI process-loopback capture scoped to a set of target PIDs.

    Mirrors :class:`sayzo_agent.capture.system_win.SystemCapture`'s public
    interface — ``async def start`` / ``async def stop`` plus a
    ``queue: asyncio.Queue[tuple[float, np.ndarray]]`` of mono pipeline
    frames. When multiple PIDs are requested, we activate one client per
    PID and sum-mix their outputs into the same queue (all clients emit
    the same float32 mix format so sum-mix is the cheapest merge).
    """

    def __init__(
        self,
        target_pids: tuple[int, ...],
        *,
        sample_rate: int = 16_000,
        frame_ms: int = 20,
        queue_maxsize: int = 200,
    ) -> None:
        if not target_pids:
            raise ValueError("ProcessLoopbackCapture requires at least one target PID")
        self.target_pids = tuple(int(p) for p in target_pids if p > 0)
        if not self.target_pids:
            raise ValueError("ProcessLoopbackCapture: no valid PIDs after filtering")

        self.sample_rate = sample_rate
        self.frame_samples = int(sample_rate * frame_ms / 1000)
        self.frame_duration = self.frame_samples / sample_rate
        self.queue: asyncio.Queue[tuple[float, np.ndarray]] = asyncio.Queue(maxsize=queue_maxsize)

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()

    # ---- lifecycle --------------------------------------------------------

    async def start(self, *, target_pids: Optional[tuple[int, ...]] = None) -> None:
        """Spawn one capture thread per target PID. ``target_pids`` kwarg is
        accepted for signature parity with ``SystemCapture.start`` but, by
        contract, must equal the PIDs passed to ``__init__`` (or be empty /
        None — which we read as "use the constructor value")."""
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

        self._loop = asyncio.get_running_loop()
        self._stop.clear()

        # Spawn one thread per PID. Each thread activates its own IAudioClient
        # and runs an independent capture loop. Threads enqueue onto the
        # shared asyncio.Queue via call_soon_threadsafe, so contention on
        # the queue is the only cross-thread synchronization.
        for pid in self.target_pids:
            t = threading.Thread(
                target=self._run_for_pid,
                args=(pid,),
                name=f"system-proc-loopback-pid{pid}",
                daemon=True,
            )
            self._threads.append(t)
            t.start()

    async def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=2.0)
        self._threads = []

    # ---- per-PID capture thread ------------------------------------------

    def _run_for_pid(self, pid: int) -> None:
        """Blocking loop: activate a process-loopback IAudioClient for ``pid``,
        initialize it in shared / event-driven / loopback mode, then poll
        IAudioCaptureClient in an event-driven loop until ``_stop`` is set.

        On any activation failure we log and exit the thread — the outer
        ``SystemCapture`` in system_win.py catches an empty queue / absent
        thread as "fall back to endpoint capture".
        """
        try:
            import comtypes  # type: ignore[import-not-found]
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except Exception:
            log.exception("[proc-loopback] CoInitializeEx failed for pid=%d", pid)
            return

        try:
            syms = _load_comtypes_symbols()
        except Exception:
            log.exception("[proc-loopback] comtypes symbol load failed for pid=%d", pid)
            return

        try:
            audio_client = _activate_process_loopback_client(syms, pid)
        except Exception:
            log.exception("[proc-loopback] activation failed for pid=%d", pid)
            return
        if audio_client is None:
            log.warning("[proc-loopback] no IAudioClient returned for pid=%d", pid)
            return

        log.info("[proc-loopback] activated client for pid=%d", pid)
        try:
            self._capture_loop(audio_client, pid)
        except Exception:
            log.exception("[proc-loopback] capture loop crashed for pid=%d", pid)
        finally:
            try:
                audio_client.Release()  # type: ignore[attr-defined]
            except Exception:
                pass
            log.info("[proc-loopback] capture thread exiting for pid=%d", pid)

    def _capture_loop(self, audio_client, pid: int) -> None:
        """Drain the IAudioCaptureClient until ``_stop`` is set.

        Reads native-rate float32 frames, accumulates them into resample
        batches (same 25-frame window as endpoint capture), resamples to
        pipeline rate, and enqueues mono pipeline frames with per-frame
        monotonic timestamps.
        """
        # Initialize in shared / event-driven / loopback mode with a 200 ms
        # buffer. 200 ms gives plenty of headroom for the GIL + resampling
        # while keeping latency low enough that end-of-meeting pauses close
        # promptly.
        buffer_duration_hns = 200 * _REFTIMES_PER_MS

        # GetMixFormat → WAVEFORMATEX*. We accept whatever native format
        # WASAPI wants to deliver and resample at batch boundaries.
        try:
            mix_format_ptr = audio_client.GetMixFormat()
        except Exception:
            log.exception("[proc-loopback] GetMixFormat failed pid=%d", pid)
            return

        # Pull native rate + channel count out of the format pointer. Fields
        # (per mmreg.h WAVEFORMATEX): [wFormatTag u16, nChannels u16,
        # nSamplesPerSec u32, nAvgBytesPerSec u32, nBlockAlign u16,
        # wBitsPerSample u16, cbSize u16].
        wf_ptr = ctypes.cast(mix_format_ptr, ctypes.POINTER(ctypes.c_uint8))
        native_rate = int.from_bytes(bytes(wf_ptr[4:8]), "little")
        n_channels = int.from_bytes(bytes(wf_ptr[2:4]), "little")
        bits_per_sample = int.from_bytes(bytes(wf_ptr[14:16]), "little")
        if bits_per_sample != 32:
            log.warning(
                "[proc-loopback] unexpected bits_per_sample=%d for pid=%d "
                "(process loopback normally delivers 32-bit float); "
                "proceeding but may misinterpret frames",
                bits_per_sample, pid,
            )
        if native_rate <= 0 or n_channels <= 0:
            log.error(
                "[proc-loopback] invalid mix format pid=%d rate=%d ch=%d",
                pid, native_rate, n_channels,
            )
            return

        try:
            audio_client.Initialize(
                _AUDCLNT_SHAREMODE_SHARED,
                _AUDCLNT_STREAMFLAGS_LOOPBACK | _AUDCLNT_STREAMFLAGS_EVENTCALLBACK,
                buffer_duration_hns,
                0,
                mix_format_ptr,
                None,
            )
        except Exception:
            log.exception("[proc-loopback] Initialize failed pid=%d", pid)
            return

        # Event handle for the event-driven mode.
        kernel32 = ctypes.windll.kernel32
        event_handle = kernel32.CreateEventW(None, False, False, None)
        if not event_handle:
            log.error("[proc-loopback] CreateEventW failed pid=%d", pid)
            return

        try:
            audio_client.SetEventHandle(event_handle)
        except Exception:
            log.exception("[proc-loopback] SetEventHandle failed pid=%d", pid)
            kernel32.CloseHandle(event_handle)
            return

        try:
            from comtypes import GUID  # type: ignore[import-not-found]
            IID_IAudioCaptureClient = GUID("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")
            capture_client_ptr = audio_client.GetService(IID_IAudioCaptureClient)
        except Exception:
            log.exception("[proc-loopback] GetService(IAudioCaptureClient) failed pid=%d", pid)
            kernel32.CloseHandle(event_handle)
            return

        try:
            audio_client.Start()
        except Exception:
            log.exception("[proc-loopback] Start failed pid=%d", pid)
            kernel32.CloseHandle(event_handle)
            return

        # Resampling parameters (native → pipeline).
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
            pid, native_rate, n_channels, self.sample_rate, up, down,
            _RESAMPLE_BATCH_FRAMES, batch_duration,
        )

        accumulator: list[np.ndarray] = []
        accumulator_samples = 0
        accumulator_first_mono: Optional[float] = None

        WAIT_TIMEOUT = 0x00000102
        WAIT_OBJECT_0 = 0x00000000

        try:
            while not self._stop.is_set():
                # Wait up to 100 ms for the next audio buffer — short enough
                # that stop() returns promptly.
                wait_result = kernel32.WaitForSingleObject(event_handle, 100)
                if wait_result == WAIT_TIMEOUT:
                    continue
                if wait_result != WAIT_OBJECT_0:
                    log.warning(
                        "[proc-loopback] WaitForSingleObject returned 0x%x pid=%d",
                        wait_result, pid,
                    )
                    continue

                # Drain all available packets before waiting again. Process
                # loopback can deliver multiple packets per event when under
                # load — missing one shows up as a silent gap.
                while True:
                    try:
                        packet_length = capture_client_ptr.GetNextPacketSize()
                    except Exception:
                        log.exception("[proc-loopback] GetNextPacketSize failed pid=%d", pid)
                        packet_length = 0
                    if packet_length == 0:
                        break

                    try:
                        data_ptr, frames_available, flags, _pos, _qpc = capture_client_ptr.GetBuffer()
                    except Exception:
                        log.exception("[proc-loopback] GetBuffer failed pid=%d", pid)
                        break

                    mono_at_read = time.monotonic()
                    packet_mono_first = mono_at_read - (frames_available / native_rate)

                    try:
                        # Samples: interleaved float32 per channel.
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
                            log.debug("[proc-loopback] ReleaseBuffer failed pid=%d", pid, exc_info=True)

                # Resample + enqueue once enough native-rate samples buffered.
                while accumulator_samples >= batch_native_samples:
                    if accumulator_first_mono is None:
                        break
                    full = np.concatenate(accumulator) if len(accumulator) > 1 else accumulator[0]
                    batch_native = full[:batch_native_samples]
                    remainder = full[batch_native_samples:]
                    batch_first_mono = accumulator_first_mono
                    if remainder.size > 0:
                        accumulator = [remainder]
                        accumulator_samples = int(remainder.shape[0])
                        accumulator_first_mono = batch_first_mono + batch_native_samples / native_rate
                    else:
                        accumulator = []
                        accumulator_samples = 0
                        accumulator_first_mono = None

                    if need_resample:
                        resampled = resample_poly(batch_native, up, down).astype(np.float32)
                    else:
                        resampled = batch_native.astype(np.float32, copy=False)

                    pos = 0
                    loop = self._loop
                    if loop is None:
                        break
                    while pos + self.frame_samples <= len(resampled):
                        frame = resampled[pos : pos + self.frame_samples]
                        frame_mono = batch_first_mono + (pos / self.sample_rate)
                        pos += self.frame_samples
                        try:
                            loop.call_soon_threadsafe(
                                self.queue.put_nowait, (frame_mono, frame)
                            )
                        except asyncio.QueueFull:
                            log.warning("[proc-loopback] queue full pid=%d", pid)
        finally:
            try:
                audio_client.Stop()
            except Exception:
                pass
            try:
                kernel32.CloseHandle(event_handle)
            except Exception:
                pass


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

    # handler is a comtypes COMObject — pass the QI'd pointer to our
    # completion handler interface.
    handler_iface = handler.QueryInterface(syms["IActivateAudioInterfaceCompletionHandler"])

    hr = ActivateAudioInterfaceAsync(
        _VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
        ctypes.byref(syms["IID_IAudioClient"]),
        ctypes.byref(pv),
        ctypes.cast(ctypes.c_void_p(int(handler_iface)), ctypes.c_void_p),
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
