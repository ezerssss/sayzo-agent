"""Shared CoreAudio + CoreFoundation bindings for the macOS detection probes.

All four scripts in this folder import from here. Pure ctypes — no pyobjc
required for the audio-side helpers, so the scripts run on a fresh macOS
Python install with only `pip install pyobjc-framework-Cocoa
pyobjc-framework-ApplicationServices psutil` for the AX / NSWorkspace bits.

The CoreAudio API surface used here was added in macOS 14.4 (Sonoma) — the
``kAudioHardwarePropertyProcessObjectList`` family. Older macOS will still
load the framework but ``AudioObjectGetPropertyDataSize`` will return
``kAudioHardwareUnknownPropertyError`` (560227702) for the new selectors,
which the scripts surface as a clear error rather than crashing.
"""
from __future__ import annotations

import ctypes
from ctypes import (
    CDLL, POINTER, Structure, byref, c_char_p, c_int, c_int32, c_long,
    c_uint32, c_void_p, sizeof,
)


# ---- framework loaders --------------------------------------------------

_CoreAudio = CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
_CoreFoundation = CDLL(
    "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
)


# ---- AudioObjectPropertyAddress + function bindings ---------------------

class AudioObjectPropertyAddress(Structure):
    _fields_ = [
        ("mSelector", c_uint32),
        ("mScope", c_uint32),
        ("mElement", c_uint32),
    ]


_CoreAudio.AudioObjectGetPropertyDataSize.argtypes = [
    c_uint32,
    POINTER(AudioObjectPropertyAddress),
    c_uint32,
    c_void_p,
    POINTER(c_uint32),
]
_CoreAudio.AudioObjectGetPropertyDataSize.restype = c_int32

_CoreAudio.AudioObjectGetPropertyData.argtypes = [
    c_uint32,
    POINTER(AudioObjectPropertyAddress),
    c_uint32,
    c_void_p,
    POINTER(c_uint32),
    c_void_p,
]
_CoreAudio.AudioObjectGetPropertyData.restype = c_int32

_CoreFoundation.CFStringGetLength.argtypes = [c_void_p]
_CoreFoundation.CFStringGetLength.restype = c_long

_CoreFoundation.CFStringGetMaximumSizeForEncoding.argtypes = [c_long, c_uint32]
_CoreFoundation.CFStringGetMaximumSizeForEncoding.restype = c_long

_CoreFoundation.CFStringGetCString.argtypes = [c_void_p, c_char_p, c_long, c_uint32]
_CoreFoundation.CFStringGetCString.restype = c_int

_CoreFoundation.CFRelease.argtypes = [c_void_p]
_CoreFoundation.CFRelease.restype = None


# ---- four-character codes (FourCC) --------------------------------------

def fourcc(s: str) -> int:
    """Convert a 4-character ASCII string into the UInt32 CoreAudio expects."""
    assert len(s) == 4, s
    return (ord(s[0]) << 24) | (ord(s[1]) << 16) | (ord(s[2]) << 8) | ord(s[3])


# Constants we need (selected from <CoreAudio/AudioHardware*.h>).
kAudioObjectSystemObject = 1
kAudioObjectPropertyScopeGlobal = fourcc("glob")
kAudioObjectPropertyElementMain = 0  # 0 always; was kAudioObjectPropertyElementMaster pre-12

# Default input device path (used by the CURRENT agent code).
kAudioHardwarePropertyDefaultInputDevice = fourcc("dIn ")
kAudioDevicePropertyDeviceIsRunningSomewhere = fourcc("goin")

# Per-process audio attribution path (macOS 14.4+).
kAudioHardwarePropertyProcessObjectList = fourcc("prl#")
kAudioProcessPropertyPID = fourcc("ppid")
kAudioProcessPropertyBundleID = fourcc("pbid")
kAudioProcessPropertyIsRunning = fourcc("pir?")
kAudioProcessPropertyIsRunningInput = fourcc("piri")
kAudioProcessPropertyIsRunningOutput = fourcc("piro")

# Aggregate-IO query that some older code paths use as a fallback.
kAudioDevicePropertyDeviceIsRunning = fourcc("goon")

# Device enumeration + introspection.
kAudioHardwarePropertyDevices = fourcc("dev#")
kAudioObjectPropertyName = fourcc("lnam")
kAudioDevicePropertyDeviceUID = fourcc("uid ")
kAudioDevicePropertyTransportType = fourcc("tran")
kAudioDevicePropertyStreams = fourcc("stm#")
kAudioDevicePropertyHogMode = fourcc("oink")
kAudioObjectPropertyScopeInput = fourcc("inpt")
kAudioObjectPropertyScopeOutput = fourcc("outp")

# Transport-type FourCC → human label. Catches the common ones; unknown
# values are printed as the FourCC itself.
_TRANSPORT_LABELS = {
    fourcc("bltn"): "built-in",
    fourcc("aggr"): "aggregate",
    fourcc("virt"): "virtual",
    fourcc("usb "): "usb",
    fourcc("blue"): "bluetooth",
    fourcc("blea"): "bluetooth-le",
    fourcc("1394"): "firewire",
    fourcc("airp"): "airplay",
    fourcc("avb "): "avb",
    fourcc("hdmi"): "hdmi",
    fourcc("dply"): "displayport",
    fourcc("thnd"): "thunderbolt",
    fourcc("pci "): "pci",
    fourcc("cont"): "continuity-camera",
    0: "unknown",
}


def transport_label(value: int | None) -> str:
    if value is None:
        return "?"
    if value in _TRANSPORT_LABELS:
        return _TRANSPORT_LABELS[value]
    # Decode fourcc.
    try:
        b = bytes([
            (value >> 24) & 0xFF,
            (value >> 16) & 0xFF,
            (value >> 8) & 0xFF,
            value & 0xFF,
        ])
        return b.decode("ascii", errors="replace").strip() or str(value)
    except Exception:
        return str(value)


def list_audio_devices() -> list[int]:
    addr = AudioObjectPropertyAddress(
        kAudioHardwarePropertyDevices,
        kAudioObjectPropertyScopeGlobal,
        kAudioObjectPropertyElementMain,
    )
    size = c_uint32(0)
    err = _CoreAudio.AudioObjectGetPropertyDataSize(
        kAudioObjectSystemObject, byref(addr), 0, None, byref(size)
    )
    if err != 0:
        raise OSError(f"AudioObjectGetPropertyDataSize(Devices) OSStatus={err}")
    if size.value == 0:
        return []
    count = size.value // sizeof(c_uint32)
    array = (c_uint32 * count)()
    err = _CoreAudio.AudioObjectGetPropertyData(
        kAudioObjectSystemObject, byref(addr), 0, None, byref(size), array
    )
    if err != 0:
        raise OSError(f"AudioObjectGetPropertyData(Devices) OSStatus={err}")
    return [int(array[i]) for i in range(count)]


def device_has_streams_in_scope(device_id: int, scope: int) -> bool:
    """Returns True if the device has at least one stream in ``scope``
    (input or output). Used to filter the device list to inputs only."""
    addr = AudioObjectPropertyAddress(
        kAudioDevicePropertyStreams,
        scope,
        kAudioObjectPropertyElementMain,
    )
    size = c_uint32(0)
    err = _CoreAudio.AudioObjectGetPropertyDataSize(
        device_id, byref(addr), 0, None, byref(size)
    )
    if err != 0:
        return False
    return size.value > 0


def read_uint32_with_scope(obj_id: int, selector: int, scope: int) -> int | None:
    addr = AudioObjectPropertyAddress(
        selector,
        scope,
        kAudioObjectPropertyElementMain,
    )
    out = c_uint32(0)
    size = c_uint32(sizeof(c_uint32))
    err = _CoreAudio.AudioObjectGetPropertyData(
        obj_id, byref(addr), 0, None, byref(size), byref(out)
    )
    if err != 0:
        return None
    return int(out.value)


def read_int32_with_scope(obj_id: int, selector: int, scope: int) -> int | None:
    addr = AudioObjectPropertyAddress(
        selector,
        scope,
        kAudioObjectPropertyElementMain,
    )
    out = c_int32(0)
    size = c_uint32(sizeof(c_int32))
    err = _CoreAudio.AudioObjectGetPropertyData(
        obj_id, byref(addr), 0, None, byref(size), byref(out)
    )
    if err != 0:
        return None
    return int(out.value)


def read_cfstring_with_scope(obj_id: int, selector: int, scope: int) -> str | None:
    addr = AudioObjectPropertyAddress(
        selector,
        scope,
        kAudioObjectPropertyElementMain,
    )
    cfstr = c_void_p(0)
    size = c_uint32(sizeof(c_void_p))
    err = _CoreAudio.AudioObjectGetPropertyData(
        obj_id, byref(addr), 0, None, byref(size), byref(cfstr)
    )
    if err != 0 or not cfstr.value:
        return None
    try:
        return cfstring_to_str(cfstr.value)
    finally:
        cfrelease(cfstr.value)

kCFStringEncodingUTF8 = 0x08000100


# ---- helpers ------------------------------------------------------------

def cfstring_to_str(cfstr: int) -> str | None:
    """Convert a CFStringRef pointer value into a Python str.

    Caller still owns the CFString (the +1 retain came from Get) — this
    helper does NOT release it. The audio-process bundle-id reader handles
    release in a try/finally because it owns that copy.
    """
    if not cfstr:
        return None
    length = _CoreFoundation.CFStringGetLength(cfstr)
    if length == 0:
        return ""
    max_size = _CoreFoundation.CFStringGetMaximumSizeForEncoding(
        length, kCFStringEncodingUTF8
    )
    buf = ctypes.create_string_buffer(int(max_size) + 1)
    if _CoreFoundation.CFStringGetCString(
        cfstr, buf, int(max_size) + 1, kCFStringEncodingUTF8
    ):
        return buf.value.decode("utf-8", errors="replace")
    return None


def cfrelease(cfstr: int) -> None:
    if cfstr:
        _CoreFoundation.CFRelease(cfstr)


def get_default_input_device_id() -> int | None:
    """Return the AudioObjectID of the current default input device, or
    None if CoreAudio reports no default."""
    addr = AudioObjectPropertyAddress(
        kAudioHardwarePropertyDefaultInputDevice,
        kAudioObjectPropertyScopeGlobal,
        kAudioObjectPropertyElementMain,
    )
    out = c_uint32(0)
    size = c_uint32(sizeof(c_uint32))
    err = _CoreAudio.AudioObjectGetPropertyData(
        kAudioObjectSystemObject, byref(addr), 0, None, byref(size), byref(out)
    )
    if err != 0:
        raise OSError(
            f"AudioObjectGetPropertyData(default-input) failed: OSStatus={err}"
        )
    return out.value or None


def is_default_input_running_somewhere() -> bool:
    """Mirrors the agent's current ``is_mic_active`` exactly."""
    dev = get_default_input_device_id()
    if dev is None:
        return False
    addr = AudioObjectPropertyAddress(
        kAudioDevicePropertyDeviceIsRunningSomewhere,
        kAudioObjectPropertyScopeGlobal,
        kAudioObjectPropertyElementMain,
    )
    out = c_uint32(0)
    size = c_uint32(sizeof(c_uint32))
    err = _CoreAudio.AudioObjectGetPropertyData(
        dev, byref(addr), 0, None, byref(size), byref(out)
    )
    if err != 0:
        raise OSError(
            f"AudioObjectGetPropertyData(IsRunningSomewhere) failed: OSStatus={err}"
        )
    return bool(out.value)


# ---- per-process audio enumeration (macOS 14.4+) ------------------------

def list_audio_process_object_ids() -> list[int]:
    """Return every AudioObjectID exposed via
    ``kAudioHardwarePropertyProcessObjectList`` (macOS 14.4+).

    Raises OSError on any CoreAudio error, with the OSStatus included so
    you can tell ``kAudioHardwareUnknownPropertyError`` (560227702 — too-old
    macOS) apart from a real failure.
    """
    addr = AudioObjectPropertyAddress(
        kAudioHardwarePropertyProcessObjectList,
        kAudioObjectPropertyScopeGlobal,
        kAudioObjectPropertyElementMain,
    )
    size = c_uint32(0)
    err = _CoreAudio.AudioObjectGetPropertyDataSize(
        kAudioObjectSystemObject, byref(addr), 0, None, byref(size)
    )
    if err != 0:
        raise OSError(
            f"AudioObjectGetPropertyDataSize(ProcessObjectList) failed: "
            f"OSStatus={err}"
        )
    if size.value == 0:
        return []
    count = size.value // sizeof(c_uint32)
    array = (c_uint32 * count)()
    err = _CoreAudio.AudioObjectGetPropertyData(
        kAudioObjectSystemObject, byref(addr), 0, None, byref(size), array
    )
    if err != 0:
        raise OSError(
            f"AudioObjectGetPropertyData(ProcessObjectList) failed: "
            f"OSStatus={err}"
        )
    return [int(array[i]) for i in range(count)]


def read_uint32(obj_id: int, selector: int) -> int | None:
    addr = AudioObjectPropertyAddress(
        selector,
        kAudioObjectPropertyScopeGlobal,
        kAudioObjectPropertyElementMain,
    )
    out = c_uint32(0)
    size = c_uint32(sizeof(c_uint32))
    err = _CoreAudio.AudioObjectGetPropertyData(
        obj_id, byref(addr), 0, None, byref(size), byref(out)
    )
    if err != 0:
        return None
    return int(out.value)


def read_int32(obj_id: int, selector: int) -> int | None:
    addr = AudioObjectPropertyAddress(
        selector,
        kAudioObjectPropertyScopeGlobal,
        kAudioObjectPropertyElementMain,
    )
    out = c_int32(0)
    size = c_uint32(sizeof(c_int32))
    err = _CoreAudio.AudioObjectGetPropertyData(
        obj_id, byref(addr), 0, None, byref(size), byref(out)
    )
    if err != 0:
        return None
    return int(out.value)


def read_cfstring(obj_id: int, selector: int) -> str | None:
    """Read a CFStringRef property. Releases the CFString before returning."""
    addr = AudioObjectPropertyAddress(
        selector,
        kAudioObjectPropertyScopeGlobal,
        kAudioObjectPropertyElementMain,
    )
    cfstr = c_void_p(0)
    size = c_uint32(sizeof(c_void_p))
    err = _CoreAudio.AudioObjectGetPropertyData(
        obj_id, byref(addr), 0, None, byref(size), byref(cfstr)
    )
    if err != 0 or not cfstr.value:
        return None
    try:
        return cfstring_to_str(cfstr.value)
    finally:
        cfrelease(cfstr.value)


def snapshot_audio_processes() -> list[dict]:
    """One call: enumerate every AudioProcessObject + read PID, BundleID,
    IsRunning, IsRunningInput, IsRunningOutput. Returns one dict per object.

    ``None`` for any field means "the property read returned an error" (the
    object exists but the OS didn't expose that property right now —
    common for system processes you can't introspect).
    """
    out: list[dict] = []
    for obj_id in list_audio_process_object_ids():
        out.append({
            "audio_object_id": obj_id,
            "pid": read_int32(obj_id, kAudioProcessPropertyPID),
            "bundle_id": read_cfstring(obj_id, kAudioProcessPropertyBundleID),
            "is_running": read_uint32(obj_id, kAudioProcessPropertyIsRunning),
            "is_running_input": read_uint32(obj_id, kAudioProcessPropertyIsRunningInput),
            "is_running_output": read_uint32(obj_id, kAudioProcessPropertyIsRunningOutput),
        })
    return out


# ---- pretty-print ------------------------------------------------------

def fmt_bool(v) -> str:
    if v is None:
        return "?"
    if v == 0:
        return "no"
    if v == 1:
        return "YES"
    return str(v)
