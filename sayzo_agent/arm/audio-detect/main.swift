// audio-detect — per-process microphone-attribution helper for Sayzo.
//
// Sister binary to sayzo_agent/capture/audio-tap/main.swift. Where audio-tap
// CAPTURES audio, this binary just READS state: who's currently using the
// microphone, mapped to the user-facing app responsible for them.
//
// Why a separate Swift binary instead of pure Python:
// - The CoreAudio per-process attribution APIs (kAudioHardwarePropertyProcessObjectList
//   and friends, macOS 14.4+) return kAudioHardwareUnknownPropertyError ('who?')
//   when called from an unsigned Python ctypes binding on macOS 26 (Tahoe). The
//   same APIs work fine from a bare Swift binary — almost certainly a Hardened
//   Runtime / signing check on the caller's identity.
// - The responsibility SPI (responsibility_get_pid_responsible_for_pid) used to
//   walk helper PIDs back to the user-facing app is also easier from native code.
//
// CLI:
//   audio-detect              # one-shot, human-readable table
//   audio-detect --json       # one-shot, JSON to stdout (the agent's parser)
//
// JSON output schema:
//   [
//     {"pid": 1234, "responsible_pid": 1200, "bundle_id": "us.zoom.xos",
//      "input": 1, "output": 0, "running": 1,
//      "input_device_names": ["MacBook Pro Microphone"]},
//     ...
//   ]
//
// - pid             — the AudioProcessObject's PID (the helper, often)
// - responsible_pid — Apple's privacy-attribution PID (the user-facing app);
//                     -1 if the SPI was unresolvable
// - bundle_id       — bundle id of the pid (NOT the responsible pid); null if
//                     the process has none
// - input / output / running — UInt32 booleans (0 or 1) from the kAudioProcess
//                              IsRunningInput / IsRunningOutput / IsRunning
//                              properties
// - input_device_names — list of human-readable input-device names the
//                        process currently has open (read via
//                        kAudioProcessPropertyDevices with input scope +
//                        kAudioObjectPropertyName per device). Used by the
//                        controller to route MicCapture to the same device
//                        the meeting app picked. Empty when the process
//                        holds no input devices, or when the property read
//                        failed (the agent falls back to the OS default mic).
//
// Compile (matches the audio-tap recipe):
//   swiftc -O -target arm64-apple-macos14.4 -o audio-detect main.swift \
//       -framework CoreAudio -framework Foundation
//
// The `-target` pin is load-bearing — see the same note in
// sayzo_agent/capture/audio-tap/main.swift.
//
// Permissions: NONE. This binary only reads OS state — it never opens an
// audio stream and never creates a tap. No Microphone, no Audio Capture,
// no Screen Recording, no Automation. The orange privacy indicator stays off.

import CoreAudio
import Foundation
import Darwin

// ---------------------------------------------------------------------------
// Responsibility SPI
//
// `responsibility_get_pid_responsible_for_pid(pid)` returns the user-facing
// process responsible for a given pid. macOS itself uses this for privacy
// indicator attribution — when audio capture happens in a helper process
// (com.apple.webkit.GPU, com.google.Chrome.helper.gpu, ...) this is what
// resolves it back to "Safari is using the microphone."
//
// Stable since at least macOS 10.10. Declared in the private
// <sys/responsibility.h>; we resolve it via dlsym at startup so the binary
// degrades gracefully (returning -1 → the agent falls back to bundle-prefix
// inference) if Apple ever changes the symbol.
// ---------------------------------------------------------------------------

typealias ResponsibilityFn = @convention(c) (pid_t) -> pid_t

let gResponsibilityFn: ResponsibilityFn? = {
    guard let handle = dlopen(nil, RTLD_LAZY) else { return nil }
    guard let sym = dlsym(handle, "responsibility_get_pid_responsible_for_pid") else {
        return nil
    }
    return unsafeBitCast(sym, to: ResponsibilityFn.self)
}()

func responsiblePid(for pid: pid_t) -> pid_t? {
    guard let fn = gResponsibilityFn else { return nil }
    let result = fn(pid)
    if result < 0 { return nil }
    return result
}

// ---------------------------------------------------------------------------
// CoreAudio property reads
// ---------------------------------------------------------------------------

func fourccString(_ value: OSStatus) -> String {
    let u = UInt32(bitPattern: value)
    let bytes: [UInt8] = [
        UInt8((u >> 24) & 0xFF),
        UInt8((u >> 16) & 0xFF),
        UInt8((u >> 8) & 0xFF),
        UInt8(u & 0xFF),
    ]
    let printable = bytes.allSatisfy { $0 >= 0x20 && $0 < 0x7F }
    if printable, let s = String(bytes: bytes, encoding: .ascii) {
        return "'\(s)'"
    }
    return String(value)
}

// Returns the list of every AudioProcessObject CoreAudio currently knows
// about, or nil + the OSStatus on failure. Callers log the OSStatus so a
// `'who?'` (kAudioHardwareUnknownPropertyError, decimal 2003332927) is
// distinguishable from a transient error — that one means the per-process
// API is unavailable on this OS / process identity.
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

// Read kAudioObjectPropertyName on any AudioObject. Used for input-device
// human-readable names — same string CoreAudio returns to PortAudio /
// sounddevice for device-name reads, so the matcher can pass it straight
// into sd.InputStream(device=...).
func readObjectName(_ obj: AudioObjectID) -> String? {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioObjectPropertyName,
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

// Returns the human-readable names of every INPUT device the process is
// currently using. Implementation mirrors listProcessObjects(): a
// size-then-read pattern on kAudioProcessPropertyDevices with input
// scope, then resolves each AudioObjectID via readObjectName.
//
// Returns [] when the process holds no input devices (the common case —
// most audio processes are output-only) or when the property is
// unsupported. Property-read errors are deliberately silent here:
// firing one warning per non-input process per poll would drown the
// real diagnostics. Actual transport failures still surface via the
// existing stderr-once path in the Python wrapper.
func readInputDeviceNames(_ obj: AudioObjectID) -> [String] {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioProcessPropertyDevices,
        mScope: kAudioObjectPropertyScopeInput,
        mElement: kAudioObjectPropertyElementMain
    )
    var size: UInt32 = 0
    var status = AudioObjectGetPropertyDataSize(obj, &addr, 0, nil, &size)
    if status != noErr || size == 0 {
        return []
    }
    let count = Int(size) / MemoryLayout<AudioObjectID>.size
    if count == 0 {
        return []
    }
    var deviceIds = [AudioObjectID](repeating: 0, count: count)
    status = deviceIds.withUnsafeMutableBufferPointer { buf -> OSStatus in
        guard let base = buf.baseAddress else { return OSStatus(-1) }
        return AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, base)
    }
    if status != noErr {
        return []
    }
    var names: [String] = []
    names.reserveCapacity(deviceIds.count)
    for did in deviceIds {
        if let name = readObjectName(did) {
            names.append(name)
        }
    }
    return names
}

// ---------------------------------------------------------------------------
// Output formats
// ---------------------------------------------------------------------------

struct AudioProcessRow {
    let pid: Int
    let responsible: Int  // -1 when the SPI didn't resolve
    let bundle: String?
    let input: UInt32
    let output: UInt32
    let running: UInt32
    let inputDeviceNames: [String]
}

func collectRows() -> ([AudioProcessRow]?, OSStatus) {
    let (objects, status) = listProcessObjects()
    guard let objects = objects else { return (nil, status) }
    var rows: [AudioProcessRow] = []
    rows.reserveCapacity(objects.count)
    for obj in objects {
        let pid = readPID(obj).map { Int($0) } ?? -1
        let bundle = readBundleID(obj)
        let input = readBool(obj, kAudioProcessPropertyIsRunningInput) ?? 0
        let output = readBool(obj, kAudioProcessPropertyIsRunningOutput) ?? 0
        let running = readBool(obj, kAudioProcessPropertyIsRunning) ?? 0
        // Only query the input-device list when the process is actually
        // capturing — readInputDeviceNames is cheap when there are no
        // input devices (single GetPropertyDataSize that fails), but we
        // can avoid even that subprocess-CoreAudio round-trip for the
        // output-only majority of audio processes.
        let deviceNames = input != 0 ? readInputDeviceNames(obj) : []
        let resp: Int
        if pid > 0, let r = responsiblePid(for: pid_t(pid)) {
            resp = Int(r)
        } else {
            resp = -1
        }
        rows.append(AudioProcessRow(
            pid: pid, responsible: resp, bundle: bundle,
            input: input, output: output, running: running,
            inputDeviceNames: deviceNames
        ))
    }
    return (rows, noErr)
}

// JSON-escape a string for embedding in a quoted field. Backslash + quote
// + control characters are the bare minimum; bundle ids and device names
// shouldn't contain anything weirder, but the agent must never emit
// unparseable JSON.
func escapeJSONString(_ s: String) -> String {
    return s
        .replacingOccurrences(of: "\\", with: "\\\\")
        .replacingOccurrences(of: "\"", with: "\\\"")
        .replacingOccurrences(of: "\n", with: "\\n")
        .replacingOccurrences(of: "\r", with: "\\r")
        .replacingOccurrences(of: "\t", with: "\\t")
}

func printJSON(_ rows: [AudioProcessRow]) {
    var entries: [String] = []
    entries.reserveCapacity(rows.count)
    for r in rows {
        let bundleField: String
        if let b = r.bundle {
            bundleField = "\"\(escapeJSONString(b))\""
        } else {
            bundleField = "null"
        }
        let devicesField = "[" + r.inputDeviceNames
            .map { "\"\(escapeJSONString($0))\"" }
            .joined(separator: ",") + "]"
        entries.append(
            "{\"pid\":\(r.pid),\"responsible_pid\":\(r.responsible)," +
            "\"bundle_id\":\(bundleField)," +
            "\"input\":\(r.input),\"output\":\(r.output),\"running\":\(r.running)," +
            "\"input_device_names\":\(devicesField)}"
        )
    }
    print("[\(entries.joined(separator: ","))]")
}

func printHuman(_ rows: [AudioProcessRow]) {
    if rows.isEmpty {
        print("(no audio process objects)")
        return
    }
    print(String(format: "  %6s  %6s  %3s  %3s  %3s  %-40s  %s",
                 "pid", "resp", "in", "out", "run", "bundle", "input devices"))
    for r in rows {
        let pidS = r.pid >= 0 ? String(format: "%6d", r.pid) : "     -"
        let respS = r.responsible >= 0 ? String(format: "%6d", r.responsible) : "     -"
        let bundle = r.bundle ?? "<none>"
        let devices = r.inputDeviceNames.isEmpty ? "-" : r.inputDeviceNames.joined(separator: ", ")
        print(String(format: "  %@  %@  %3d  %3d  %3d  %-40@  %@",
                     pidS, respS, r.input, r.output, r.running, bundle, devices))
    }
}

// ---------------------------------------------------------------------------
// Driver
// ---------------------------------------------------------------------------

let args = CommandLine.arguments
let jsonMode = args.contains("--json")

let (rows, status) = collectRows()
guard let rows = rows else {
    FileHandle.standardError.write(Data(
        "audio-detect: ProcessObjectList enumeration failed: OSStatus \(status) \(fourccString(status))\n".utf8
    ))
    if jsonMode {
        // Always emit valid JSON on stdout so the parser doesn't crash.
        print("[]")
    }
    // Exit 1 so the parent can distinguish enumeration failure from
    // "the system has no audio processes." A 0-length JSON array is fine
    // when CoreAudio responded but reported no processes; we return non-zero
    // only on an actual error path.
    exit(1)
}

if jsonMode {
    printJSON(rows)
} else {
    printHuman(rows)
}
