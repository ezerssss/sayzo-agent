// sck-tap — Capture all system audio via ScreenCaptureKit, pipe raw PCM to stdout.
//
// Output format: mono float32 PCM at 48 kHz, written to stdout unbuffered.
// On permission denied: prints message to stderr and exits with code 77.
//
// Compile (macOS 13+):
//   swiftc -O -o sck-tap main.swift \
//       -framework ScreenCaptureKit -framework CoreMedia -framework AVFoundation
//
// Test:
//   ./sck-tap | ffplay -f f32le -ar 48000 -ac 1 -

import AVFoundation
import CoreMedia
import Foundation
import ScreenCaptureKit

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

let kSampleRate: Int = 48_000
let kChannelCount: Int = 1  // mono
let kExitPermissionDenied: Int32 = 77

// Unbuffer stdout so PCM bytes reach the Python reader immediately.
setbuf(stdout, nil)

// ---------------------------------------------------------------------------
// Stream output delegate — receives audio sample buffers, writes to stdout.
// ---------------------------------------------------------------------------

class AudioWriter: NSObject, SCStreamOutput {
    func stream(
        _ stream: SCStream,
        didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
        of type: SCStreamOutputType
    ) {
        guard type == .audio else { return }
        guard sampleBuffer.isValid, sampleBuffer.numSamples > 0 else { return }

        // Extract raw audio bytes from the sample buffer.
        guard let blockBuffer = sampleBuffer.dataBuffer else { return }
        let length = CMBlockBufferGetDataLength(blockBuffer)
        var data = Data(count: length)
        data.withUnsafeMutableBytes { rawPtr in
            guard let baseAddress = rawPtr.baseAddress else { return }
            CMBlockBufferCopyDataBytes(
                blockBuffer, atOffset: 0, dataLength: length, destination: baseAddress
            )
        }

        // The audio arrives as interleaved float32 samples.  ScreenCaptureKit
        // may deliver stereo even when we requested mono — downmix in that case.
        let format = CMSampleBufferGetFormatDescription(sampleBuffer)
        let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(format!)!.pointee
        let deliveredChannels = Int(asbd.mChannelsPerFrame)

        if deliveredChannels > 1 {
            // Downmix to mono by averaging channels.
            let sampleCount = length / MemoryLayout<Float>.size
            let frameCount = sampleCount / deliveredChannels
            var mono = [Float](repeating: 0, count: frameCount)
            data.withUnsafeBytes { rawPtr in
                let floats = rawPtr.bindMemory(to: Float.self)
                for i in 0..<frameCount {
                    var sum: Float = 0
                    for ch in 0..<deliveredChannels {
                        sum += floats[i * deliveredChannels + ch]
                    }
                    mono[i] = sum / Float(deliveredChannels)
                }
            }
            mono.withUnsafeBytes { ptr in
                fwrite(ptr.baseAddress, 1, ptr.count, stdout)
            }
        } else {
            data.withUnsafeBytes { ptr in
                fwrite(ptr.baseAddress, 1, ptr.count, stdout)
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Signal handling — clean shutdown on SIGTERM / SIGINT.
// ---------------------------------------------------------------------------

var runLoop = true

func installSignalHandlers() {
    let handler: @convention(c) (Int32) -> Void = { _ in
        runLoop = false
        CFRunLoopStop(CFRunLoopGetMain())
    }
    signal(SIGTERM, handler)
    signal(SIGINT, handler)
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

@available(macOS 13.0, *)
func run() async {
    installSignalHandlers()

    // 1. Get shareable content to build a filter that captures everything.
    let content: SCShareableContent
    do {
        content = try await SCShareableContent.excludingDesktopWindows(
            false, onScreenWindowsOnly: false
        )
    } catch {
        fputs("sck-tap: failed to get shareable content: \(error)\n", stderr)
        if "\(error)".contains("denied") || "\(error)".contains("-3801") {
            fputs(
                "sck-tap: Screen Recording permission denied.\n"
                    + "Grant it in: System Settings → Privacy & Security → Screen Recording\n",
                stderr
            )
            exit(kExitPermissionDenied)
        }
        exit(1)
    }

    guard let display = content.displays.first else {
        fputs("sck-tap: no display found\n", stderr)
        exit(1)
    }

    // 2. Filter: capture the entire display (all audio).
    //    Exclude no apps — we want every sound source.
    let filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])

    // 3. Stream configuration: audio only, mono float32 at 48 kHz.
    let config = SCStreamConfiguration()
    config.capturesAudio = true
    config.excludesCurrentProcessAudio = true  // don't capture our own output
    config.sampleRate = kSampleRate
    config.channelCount = kChannelCount

    // Disable video capture to save resources.
    config.width = 2
    config.height = 2
    config.minimumFrameInterval = CMTime(value: 1, timescale: 1)  // 1 fps minimum

    // 4. Create and start the stream.
    let stream = SCStream(filter: filter, configuration: config, delegate: nil)
    let writer = AudioWriter()
    do {
        try stream.addStreamOutput(writer, type: .audio, sampleHandlerQueue: .global())
        try await stream.startCapture()
    } catch {
        fputs("sck-tap: failed to start capture: \(error)\n", stderr)
        if "\(error)".contains("denied") || "\(error)".contains("-3801") {
            fputs(
                "sck-tap: Screen Recording permission denied.\n"
                    + "Grant it in: System Settings → Privacy & Security → Screen Recording\n",
                stderr
            )
            exit(kExitPermissionDenied)
        }
        exit(1)
    }

    fputs("sck-tap: capturing system audio at \(kSampleRate) Hz mono float32\n", stderr)

    // 5. Run until signalled.
    while runLoop {
        CFRunLoopRunInMode(.defaultMode, 1.0, false)
    }

    // 6. Clean shutdown.
    try? await stream.stopCapture()
    fputs("sck-tap: stopped\n", stderr)
}

if #available(macOS 13.0, *) {
    // Use a semaphore to bridge async → sync at the top level, keeping the
    // main run loop available for ScreenCaptureKit callbacks.
    let semaphore = DispatchSemaphore(value: 0)
    Task {
        await run()
        semaphore.signal()
    }
    semaphore.wait()
} else {
    fputs("sck-tap: requires macOS 13.0 (Ventura) or later\n", stderr)
    exit(1)
}
