// audio-tap — Capture all system audio via CoreAudio Process Taps (macOS 14.4+),
// pipe raw PCM to stdout.
//
// Output format: mono float32 PCM at 48 kHz, written to stdout unbuffered.
// On permission denied: prints message to stderr and exits with code 77.
//
// Compile (macOS 14.4+):
//   swiftc -O -o audio-tap main.swift \
//       -framework CoreAudio -framework AudioToolbox -framework AVFoundation
//
// Test:
//   ./audio-tap | ffplay -f f32le -ar 48000 -ac 1 -
//
// Why CoreAudio taps (not ScreenCaptureKit): the tap permission prompt is
// audio-only ("Audio Capture") instead of the alarming "Screen Recording"
// prompt, and macOS does NOT surface the screen-sharing menu-bar item with a
// "Stop Sharing" button — the whole reason we moved off `sck-tap`.

import AudioToolbox
import AVFoundation
import CoreAudio
import Foundation

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

let kSampleRate: Double = 48_000
let kChannelCount: AVAudioChannelCount = 1
let kExitPermissionDenied: Int32 = 77

// Unbuffer stdout so PCM bytes reach the Python reader immediately.
setbuf(stdout, nil)

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
// Converts incoming samples to mono float32 @ 48 kHz, writes to stdout.
// ---------------------------------------------------------------------------

let ioProc: AudioDeviceIOProc = { _, _, inInputData, _, _, _, _ in
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

    if let floatPtr = outBuf.floatChannelData?[0] {
        fwrite(floatPtr, MemoryLayout<Float>.size, Int(outBuf.frameLength), stdout)
    }
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
            + "emitting \(Int(kSampleRate)) Hz mono float32)\n",
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
