// audio-tap — Capture all system audio via CoreAudio Process Taps (macOS 14.4+),
// pipe timestamp-framed PCM to stdout.
//
// Wire protocol (new in protocol version 1): each capture block is written as
//
//     [4 bytes magic "SAYZ"][8 bytes Float64 timestamp][4 bytes UInt32 byte count][N bytes Float32 PCM]
//
// - Magic is ASCII "SAYZ" (0x53 0x41 0x59 0x5A) so Python can detect a stale
//   binary (which wrote raw PCM) and warn.
// - Timestamp is CACurrentMediaTime-equivalent seconds (mach-timebase-derived
//   from CoreAudio's `inInputTime->mHostTime`), matching Python's
//   `time.monotonic()` on macOS since both resolve to `mach_absolute_time()`
//   converted to seconds.
// - Byte count is the PCM payload size in bytes (always a multiple of 4 —
//   Float32 mono at 48 kHz).
// - PCM is mono float32 at 48 kHz, one CoreAudio ioProc block per header.
//
// On permission denied: prints message to stderr and exits with code 77.
//
// Compile (macOS 14.4+):
//   swiftc -O -o audio-tap main.swift \
//       -framework CoreAudio -framework AudioToolbox -framework AVFoundation
//
// Test:
//   ./audio-tap | python3 -c 'import sys, struct
//       while True:
//           h = sys.stdin.buffer.read(16)
//           if len(h) < 16: break
//           magic, ts, n = h[:4], struct.unpack("<d", h[4:12])[0], struct.unpack("<I", h[12:16])[0]
//           pcm = sys.stdin.buffer.read(n)
//           print(magic, ts, n, len(pcm))'
//
// Why CoreAudio taps (not ScreenCaptureKit): the tap permission prompt is
// audio-only ("Audio Capture") instead of the alarming "Screen Recording"
// prompt, and macOS does NOT surface the screen-sharing menu-bar item with a
// "Stop Sharing" button — the whole reason we moved off `sck-tap`.

import AudioToolbox
import AVFoundation
import CoreAudio
import Darwin
import Foundation

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

let kSampleRate: Double = 48_000
let kChannelCount: AVAudioChannelCount = 1
let kExitPermissionDenied: Int32 = 77

// Magic bytes "SAYZ" so Python can distinguish new-protocol output from a
// stale binary that emitted raw PCM.
let kMagicBytes: [UInt8] = [0x53, 0x41, 0x59, 0x5A]

// Unbuffer stdout so PCM bytes reach the Python reader immediately.
setbuf(stdout, nil)

// ---------------------------------------------------------------------------
// Mach timebase for converting AudioTimeStamp.mHostTime → seconds.
// `mach_absolute_time()` units × (numer/denom) = nanoseconds.
// ---------------------------------------------------------------------------

var gMachTimebase = mach_timebase_info_data_t()
mach_timebase_info(&gMachTimebase)

@inline(__always)
func hostTimeToSeconds(_ hostTime: UInt64) -> Double {
    // Convert mach host time to nanoseconds, then to seconds. The same math
    // Python's time.monotonic() and CACurrentMediaTime() use, so the result
    // is directly comparable to a Python-side time.monotonic() value.
    let nanos = Double(hostTime) * Double(gMachTimebase.numer) / Double(gMachTimebase.denom)
    return nanos / 1_000_000_000
}

// ---------------------------------------------------------------------------
// Global state
//
// The CoreAudio IO proc is a @convention(c) callback — it cannot capture
// Swift context, so shared state lives at file scope.
// ---------------------------------------------------------------------------

final class TapState {
    var aggDeviceID: AudioObjectID = 0
    var tapID: AudioObjectID = 0
    var procID: AudioDeviceIOProcID?
    var inputFormat: AVAudioFormat!
    var outputFormat: AVAudioFormat!
    var converter: AVAudioConverter!
    var outputBuffer: AVAudioPCMBuffer!
}
let gState = TapState()
var gRunLoop = true

// ---------------------------------------------------------------------------
// IO proc — called on a real-time audio thread for each input block.
// Converts incoming samples to mono float32 @ 48 kHz, writes a framed record
// (header + PCM) to stdout.
// ---------------------------------------------------------------------------

let ioProc: AudioDeviceIOProc = { _, _, inInputData, inInputTime, _, _, _ in
    guard let converter = gState.converter,
          let outBuf = gState.outputBuffer,
          let inputFormat = gState.inputFormat else {
        return noErr
    }

    // Zero-copy wrapper over the CoreAudio buffer list.
    guard let inBuf = AVAudioPCMBuffer(
        pcmFormat: inputFormat,
        bufferListNoCopy: inInputData,
        deallocator: nil
    ) else {
        return noErr
    }

    outBuf.frameLength = 0

    var inputConsumed = false
    var convertError: NSError?
    let status = converter.convert(to: outBuf, error: &convertError) { _, outStatus in
        if inputConsumed {
            outStatus.pointee = .noDataNow
            return nil
        }
        inputConsumed = true
        outStatus.pointee = .haveData
        return inBuf
    }

    if status == .error {
        return noErr
    }

    guard let floatPtr = outBuf.floatChannelData?[0] else {
        return noErr
    }

    let frameCount = Int(outBuf.frameLength)
    if frameCount == 0 {
        return noErr
    }

    // Timestamp of the first sample in this input block, derived from
    // CoreAudio's hardware-grounded timestamp. Converted to mach-seconds so
    // Python's `time.monotonic()` matches directly.
    let hostTime = inInputTime.pointee.mHostTime
    var timestampSeconds = hostTimeToSeconds(hostTime)
    var pcmByteCount = UInt32(frameCount * MemoryLayout<Float>.size)

    // Emit framing header. Byte order is native little-endian on all macOS
    // platforms (Apple Silicon + Intel), matching Python's struct "<" format.
    _ = kMagicBytes.withUnsafeBufferPointer { ptr in
        fwrite(ptr.baseAddress, 1, 4, stdout)
    }
    withUnsafePointer(to: &timestampSeconds) { ptr in
        _ = fwrite(ptr, MemoryLayout<Double>.size, 1, stdout)
    }
    withUnsafePointer(to: &pcmByteCount) { ptr in
        _ = fwrite(ptr, MemoryLayout<UInt32>.size, 1, stdout)
    }

    // PCM payload.
    fwrite(floatPtr, MemoryLayout<Float>.size, frameCount, stdout)
    return noErr
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

func die(_ msg: String, code: Int32 = 1) -> Never {
    fputs("audio-tap: \(msg)\n", stderr)
    exit(code)
}

// Read the tap's native audio stream format — the format IO proc will deliver.
func readTapStreamFormat(_ tapID: AudioObjectID) -> AudioStreamBasicDescription? {
    var asbd = AudioStreamBasicDescription()
    var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioTapPropertyFormat,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    let status = AudioObjectGetPropertyData(tapID, &addr, 0, nil, &size, &asbd)
    if status != noErr {
        fputs("audio-tap: read tap stream format failed (OSStatus \(status))\n", stderr)
        return nil
    }
    return asbd
}

// ---------------------------------------------------------------------------
// Signal handling
// ---------------------------------------------------------------------------

func installSignalHandlers() {
    let handler: @convention(c) (Int32) -> Void = { _ in
        gRunLoop = false
        CFRunLoopStop(CFRunLoopGetMain())
    }
    signal(SIGTERM, handler)
    signal(SIGINT, handler)
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

@available(macOS 14.4, *)
func run() {
    installSignalHandlers()

    // 1. System-wide mono tap. Empty exclusion list: this binary produces no
    //    audio output, so including it in the global tap is a no-op.
    let tapDesc = CATapDescription(monoGlobalTapButExcludeProcesses: [])
    tapDesc.uuid = UUID()
    tapDesc.muteBehavior = .unmuted

    // 2. Create the tap. First-run triggers the Audio Capture permission
    //    prompt (via NSAudioCaptureUsageDescription). Denial → non-zero status.
    var tapID: AudioObjectID = 0
    let tapStatus = AudioHardwareCreateProcessTap(tapDesc, &tapID)
    if tapStatus != noErr {
        fputs(
            "audio-tap: AudioHardwareCreateProcessTap failed (OSStatus \(tapStatus)).\n"
                + "If this is the first launch or permission was revoked, grant it in:\n"
                + "  System Settings → Privacy & Security → Audio Capture\n"
                + "Then restart the agent.\n",
            stderr
        )
        exit(kExitPermissionDenied)
    }
    gState.tapID = tapID

    // 3. Aggregate device that exposes the tap as an input.
    let aggUID = "com.sayzo.audio-tap.\(UUID().uuidString)"
    let aggDict: [String: Any] = [
        kAudioAggregateDeviceNameKey as String: "sayzo-audio-tap",
        kAudioAggregateDeviceUIDKey as String: aggUID,
        kAudioAggregateDeviceIsPrivateKey as String: true,
        kAudioAggregateDeviceTapAutoStartKey as String: true,
        kAudioAggregateDeviceTapListKey as String: [[
            kAudioSubTapUIDKey as String: tapDesc.uuid.uuidString,
        ]],
    ]

    var aggDeviceID: AudioObjectID = 0
    let aggStatus = AudioHardwareCreateAggregateDevice(aggDict as CFDictionary, &aggDeviceID)
    if aggStatus != noErr {
        _ = AudioHardwareDestroyProcessTap(tapID)
        die("AudioHardwareCreateAggregateDevice failed (OSStatus \(aggStatus))")
    }
    gState.aggDeviceID = aggDeviceID

    // 4. Discover the tap's native format (what the IO proc will deliver).
    guard var nativeASBD = readTapStreamFormat(tapID) else {
        _ = AudioHardwareDestroyAggregateDevice(aggDeviceID)
        _ = AudioHardwareDestroyProcessTap(tapID)
        die("could not read tap stream format")
    }
    guard let nativeFmt = AVAudioFormat(streamDescription: &nativeASBD) else {
        _ = AudioHardwareDestroyAggregateDevice(aggDeviceID)
        _ = AudioHardwareDestroyProcessTap(tapID)
        die("unsupported native tap format (sr=\(nativeASBD.mSampleRate), ch=\(nativeASBD.mChannelsPerFrame))")
    }
    gState.inputFormat = nativeFmt

    // 5. Target format: mono float32 @ 48 kHz, non-interleaved.
    guard let targetFmt = AVAudioFormat(
        commonFormat: .pcmFormatFloat32,
        sampleRate: kSampleRate,
        channels: kChannelCount,
        interleaved: false
    ) else {
        die("failed to build target AVAudioFormat")
    }
    gState.outputFormat = targetFmt

    guard let converter = AVAudioConverter(from: nativeFmt, to: targetFmt) else {
        die("failed to build AVAudioConverter \(nativeFmt) → \(targetFmt)")
    }
    // Use the highest-quality sample-rate conversion and channel-mixing
    // filters available. AVAudioConverter defaults to `.medium`, which is
    // audibly lossy when the tap's native rate doesn't match our 48 kHz
    // target (e.g. 44.1 kHz sources like most music players). `.max` is only
    // slightly more expensive since the converter runs once per CoreAudio
    // IO block (~10 ms) and the CPU already has bandwidth to spare.
    converter.sampleRateConverterQuality = .max
    gState.converter = converter

    // Reusable 1-second output buffer — IO proc blocks are ~5–50 ms.
    guard let outBuf = AVAudioPCMBuffer(pcmFormat: targetFmt, frameCapacity: UInt32(kSampleRate)) else {
        die("failed to allocate AVAudioPCMBuffer")
    }
    gState.outputBuffer = outBuf

    // 6. Install IO proc and start.
    var procID: AudioDeviceIOProcID?
    let createStatus = AudioDeviceCreateIOProcID(aggDeviceID, ioProc, nil, &procID)
    if createStatus != noErr || procID == nil {
        die("AudioDeviceCreateIOProcID failed (OSStatus \(createStatus))")
    }
    gState.procID = procID

    let startStatus = AudioDeviceStart(aggDeviceID, procID)
    if startStatus != noErr {
        if let procID = procID {
            _ = AudioDeviceDestroyIOProcID(aggDeviceID, procID)
        }
        _ = AudioHardwareDestroyAggregateDevice(aggDeviceID)
        _ = AudioHardwareDestroyProcessTap(tapID)
        die("AudioDeviceStart failed (OSStatus \(startStatus))")
    }

    fputs(
        "audio-tap: capturing system audio "
            + "(native \(Int(nativeFmt.sampleRate)) Hz ch=\(nativeFmt.channelCount), "
            + "emitting \(Int(kSampleRate)) Hz mono float32, protocol=SAYZ/v1)\n",
        stderr
    )

    // 7. Run until signalled.
    while gRunLoop {
        CFRunLoopRunInMode(.defaultMode, 1.0, false)
    }

    // 8. Clean shutdown — reverse order of construction.
    _ = AudioDeviceStop(aggDeviceID, procID)
    if let procID = procID {
        _ = AudioDeviceDestroyIOProcID(aggDeviceID, procID)
    }
    _ = AudioHardwareDestroyAggregateDevice(aggDeviceID)
    _ = AudioHardwareDestroyProcessTap(tapID)

    fputs("audio-tap: stopped\n", stderr)
}

// ---------------------------------------------------------------------------
// Entry
// ---------------------------------------------------------------------------

if #available(macOS 14.4, *) {
    run()
} else {
    fputs("audio-tap: requires macOS 14.4 or later\n", stderr)
    exit(1)
}
