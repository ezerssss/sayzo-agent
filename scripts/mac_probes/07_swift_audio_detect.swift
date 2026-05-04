// Probe 7 — Per-process mic-attribution from a bare Swift binary.
//
// Same API surface as the Python probe 02, but called from native Swift
// against the system-installed CoreAudio framework. Tests whether the
// `'who?'` (kAudioHardwareUnknownPropertyError) we got from Python is:
//
//   (a) a Python ctypes / pyobjc binding issue       → Swift will succeed
//   (b) a bare-unsigned-binary entitlement issue     → Swift will also fail
//   (c) a missing Info.plist key (NSAudioCaptureUsageDescription) issue
//                                                    → bare binary fails too
//
// Result decides next steps: (a) wrap detection in a tiny Swift helper
// shipped alongside the agent; (b) or (c) ship a bundled-app helper
// (still tiny, just .app-shaped) with the right plist + signing.
//
// Compile (no bundle, no plist — deliberately minimal):
//   cd ~/mac_probes
//   swiftc -O -o 07_swift_audio_detect 07_swift_audio_detect.swift \
//       -framework CoreAudio -framework Foundation
//
// Run:
//   ./07_swift_audio_detect           # one-shot
//   ./07_swift_audio_detect --watch   # poll every 1 s
//   ./07_swift_audio_detect --all     # show every audio process, not just is_running_input=YES
//
// While running: join Zoom / Meet / Discord and watch for the meeting
// app's bundle id to appear with `in=YES`. Same expectation as Python
// probe 02 — but if Python failed and Swift succeeds, that tells us
// everything.

import CoreAudio
import Foundation
import Darwin

// ---- helpers -------------------------------------------------------------

func fourccString(_ value: OSStatus) -> String {
    // OSStatus codes from CoreAudio are FourCCs packed into Int32. Extract
    // the four ASCII bytes for human-readable error display.
    let u = UInt32(bitPattern: value)
    let bytes: [UInt8] = [
        UInt8((u >> 24) & 0xFF),
        UInt8((u >> 16) & 0xFF),
        UInt8((u >> 8) & 0xFF),
        UInt8(u & 0xFF),
    ]
    let allPrintable = bytes.allSatisfy { $0 >= 0x20 && $0 < 0x7F }
    if allPrintable, let s = String(bytes: bytes, encoding: .ascii) {
        return "'\(s)'"
    }
    return String(value)
}

func listProcessObjects() -> ([AudioObjectID]?, OSStatus) {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyProcessObjectList,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var size: UInt32 = 0
    var status = AudioObjectGetPropertyDataSize(
        AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size
    )
    if status != noErr {
        return (nil, status)
    }
    let count = Int(size) / MemoryLayout<AudioObjectID>.size
    if count == 0 {
        return ([], noErr)
    }
    var objects = [AudioObjectID](repeating: 0, count: count)
    status = objects.withUnsafeMutableBufferPointer { buf -> OSStatus in
        guard let base = buf.baseAddress else { return OSStatus(-1) }
        return AudioObjectGetPropertyData(
            AudioObjectID(kAudioObjectSystemObject),
            &addr, 0, nil, &size, base
        )
    }
    if status != noErr {
        return (nil, status)
    }
    return (objects, noErr)
}

func readPID(_ obj: AudioObjectID) -> pid_t? {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioProcessPropertyPID,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var pid: pid_t = 0
    var size: UInt32 = UInt32(MemoryLayout<pid_t>.size)
    let status = AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, &pid)
    return status == noErr ? pid : nil
}

func readBundleID(_ obj: AudioObjectID) -> String? {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioProcessPropertyBundleID,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var raw: Unmanaged<CFString>?
    var size: UInt32 = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    let status = withUnsafeMutablePointer(to: &raw) { ptr -> OSStatus in
        ptr.withMemoryRebound(to: UInt8.self, capacity: Int(size)) { _ in
            AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, ptr)
        }
    }
    if status != noErr { return nil }
    return raw?.takeRetainedValue() as String?
}

func readBool(_ obj: AudioObjectID, _ selector: AudioObjectPropertySelector) -> UInt32? {
    var addr = AudioObjectPropertyAddress(
        mSelector: selector,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var value: UInt32 = 0
    var size: UInt32 = UInt32(MemoryLayout<UInt32>.size)
    let status = AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, &value)
    return status == noErr ? value : nil
}

// ---- pretty-printing ---------------------------------------------------

func boolCell(_ v: UInt32?) -> String {
    guard let v = v else { return "  ?" }
    if v == 0 { return " no" }
    if v == 1 { return "YES" }
    return "\(v)"
}

func snapshot(showAll: Bool) {
    let timestamp = ISO8601DateFormatter().string(from: Date())
    print("\n[\(timestamp)]")

    let (objects, status) = listProcessObjects()
    guard let objects = objects else {
        print("  ProcessObjectList enumeration FAILED: OSStatus \(status) \(fourccString(status))")
        if status == 2003332927 {
            print("  ('who?' = kAudioHardwareUnknownPropertyError — same failure")
            print("   as the Python probe; per-process API is gated on this Mac")
            print("   even from a bare Swift binary. Likely needs a bundled .app")
            print("   with NSAudioCaptureUsageDescription in Info.plist.)")
        }
        return
    }
    print("  Found \(objects.count) audio process objects.")

    var rows: [(pid: Int, bundle: String, inp: UInt32?, outp: UInt32?, run: UInt32?)] = []
    for obj in objects {
        let pid = readPID(obj).map { Int($0) } ?? -1
        let bundle = readBundleID(obj) ?? "<no bundle>"
        let inp = readBool(obj, kAudioProcessPropertyIsRunningInput)
        let outp = readBool(obj, kAudioProcessPropertyIsRunningOutput)
        let run = readBool(obj, kAudioProcessPropertyIsRunning)
        if !showAll && (inp ?? 0) == 0 { continue }
        rows.append((pid, bundle, inp, outp, run))
    }

    if rows.isEmpty {
        print("  (no processes with IsRunningInput=YES — try --all to see everything)")
        return
    }

    print("    PID    in  out  run  bundle")
    for r in rows {
        let pidS = r.pid >= 0 ? String(format: "%6d", r.pid) : "     -"
        print("  \(pidS)  \(boolCell(r.inp))  \(boolCell(r.outp))  \(boolCell(r.run))  \(r.bundle)")
    }
}

// ---- driver -------------------------------------------------------------

let args = CommandLine.arguments
let watch = args.contains("--watch")
let showAll = args.contains("--all")

if watch {
    print("Watching audio processes from Swift. Ctrl-C to stop.")
    print("Try: join Zoom / Discord / Meet — look for is_running_input=YES")
    while true {
        snapshot(showAll: showAll)
        Thread.sleep(forTimeInterval: 1.0)
    }
} else {
    snapshot(showAll: showAll)
}
