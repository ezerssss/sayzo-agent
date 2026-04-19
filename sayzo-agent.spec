# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Sayzo Agent.

Build:
    pyinstaller sayzo-agent.spec

This produces a one-directory bundle under dist/sayzo-agent/ containing the
main executable plus all dependencies.  The platform installer (NSIS on Windows,
DMG on macOS) wraps this directory for end-user distribution.
"""
import sys
from pathlib import Path

block_cipher = None

# ---------------------------------------------------------------------------
# App icon — Sayzo logo. .ico on Windows, .icns on macOS.
# The .icns is generated at build time in the macOS CI job from logo.png
# (see .github/workflows/build.yml) so it does not need to live in git.
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    app_icon = "installer/assets/logo.ico"
elif sys.platform == "darwin":
    app_icon = "installer/assets/logo.icns"
else:
    app_icon = None

# ---------------------------------------------------------------------------
# Data files to bundle
# ---------------------------------------------------------------------------

datas = []

# Silero VAD ONNX model — silero_vad ships it as package data.
import silero_vad
silero_pkg = Path(silero_vad.__file__).parent
silero_onnx = silero_pkg / "data" / "silero_vad.onnx"
if silero_onnx.exists():
    datas.append((str(silero_onnx), "silero_vad/data"))

# faster-whisper's internal VAD ONNX — different file from silero_vad above.
# Loaded lazily by faster_whisper.vad when transcribe(vad_filter=True) is used
# (see sayzo_agent/stt.py), and PyInstaller doesn't pick up non-Python package
# data without an explicit entry.
import faster_whisper
fw_pkg = Path(faster_whisper.__file__).parent
fw_vad_onnx = fw_pkg / "assets" / "silero_vad_v6.onnx"
if fw_vad_onnx.exists():
    datas.append((str(fw_vad_onnx), "faster_whisper/assets"))

# llama-cpp-python ships its native libs in llama_cpp/lib/ and resolves them
# via os.add_dll_directory (Windows) at import time. PyInstaller won't collect
# that subdir otherwise.
import llama_cpp
llama_cpp_lib = Path(llama_cpp.__file__).parent / "lib"
if llama_cpp_lib.exists():
    for f in llama_cpp_lib.iterdir():
        if f.is_file() and f.suffix.lower() in (".dll", ".dylib", ".so"):
            datas.append((str(f), "llama_cpp/lib"))

# Resemblyzer ships pretrained speaker-embedding weights as package data.
# VoiceEncoder() loads it at construction time (sayzo_agent/speaker.py).
import resemblyzer
resemblyzer_pkg = Path(resemblyzer.__file__).parent
resemblyzer_pt = resemblyzer_pkg / "pretrained.pt"
if resemblyzer_pt.exists():
    datas.append((str(resemblyzer_pt), "resemblyzer"))

# macOS: bundle the pre-compiled audio-tap binary (CoreAudio Process Taps helper).
if sys.platform == "darwin":
    audio_tap = Path("sayzo_agent/capture/audio-tap/audio-tap")
    if audio_tap.exists():
        datas.append((str(audio_tap), "sayzo_agent/capture/audio-tap"))

# First-run GUI assets — built HTML/JS/CSS bundle that the pywebview window
# loads. The dev path resolves these via Path(__file__).parent in
# sayzo_agent/gui/setup/window.py; the frozen path uses sys._MEIPASS.
# Built in CI via `npm ci && npm run build` before this spec runs — fail
# loudly if it isn't there so frozen binaries don't ship with a broken
# setup window that silently skips.
webui_dist = Path("sayzo_agent/gui/webui/dist")
if not (webui_dist / "index.html").exists():
    raise SystemExit(
        f"webui not built: {webui_dist / 'index.html'} missing. "
        "Run `cd sayzo_agent/gui/webui && npm ci && npm run build` first."
    )
datas.append((str(webui_dist), "sayzo_agent/gui/webui/dist"))

# ---------------------------------------------------------------------------
# Hidden imports — modules loaded lazily or via importlib that PyInstaller
# cannot detect through static analysis.
# ---------------------------------------------------------------------------

hiddenimports = [
    # Audio / capture
    "sounddevice",
    "_sounddevice_data",
    # VAD
    "silero_vad",
    "onnxruntime",
    # STT
    "faster_whisper",
    "ctranslate2",
    # Speaker embedding
    "resemblyzer",
    "librosa",
    "webrtcvad",
    # LLM
    "llama_cpp",
    # Audio encoding
    "av",
    # Config
    "pydantic",
    "pydantic_settings",
    # GUI
    "pystray",
    "PIL",
    "PIL.Image",
    "PIL.ImageDraw",
    # HuggingFace
    "huggingface_hub",
    # Networking
    "httpx",
    # torch — required by Silero VAD feed()
    "torch",
    # Native toast notifications
    "desktop_notifier",
    # First-run GUI window
    "webview",
]

# Windows-specific
if sys.platform == "win32":
    hiddenimports += [
        "pyaudiowpatch",
        "pystray._win32",
        # desktop-notifier → WinRT backend
        "winrt",
        "winsdk",
        # pywebview → EdgeChromium / WinForms backend (uses .NET via pythonnet)
        "webview.platforms.edgechromium",
        "webview.platforms.winforms",
        "clr_loader",
        "pythonnet",
    ]

# macOS-specific
if sys.platform == "darwin":
    hiddenimports += [
        "pystray._darwin",
        # desktop-notifier → UNUserNotificationCenter backend
        "rubicon",
        "rubicon.objc",
        # pywebview → Cocoa / WKWebView backend (via pyobjc)
        "webview.platforms.cocoa",
        "Foundation",
        "AppKit",
        "WebKit",
    ]

# ---------------------------------------------------------------------------
# Excludes — strip modules we definitely don't need to shrink the bundle.
# ---------------------------------------------------------------------------

excludes = [
    "matplotlib",
    "matplotlib.pyplot",
    "IPython",
    "jupyter",
    "notebook",
    "pytest",
    "pytest_asyncio",
    "tkinter.test",
]

# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

a = Analysis(
    ["entry.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=["hooks"],  # custom hooks override broken contrib hooks
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# ---------------------------------------------------------------------------
# Strip MSVC C++ runtime DLLs from the top-level bundle.
#
# Multiple Python packages (numpy, sklearn, llvmlite, winrt, pythonnet) ship
# their own copies of msvcp140.dll / vcruntime140.dll pairs, each from a
# different MSVC release. PyInstaller deduplicates across packages and
# picks whichever it saw first, which can mate a 14.40 msvcp140.dll with a
# 14.44 msvcp140_1.dll — an incompatible combination. When torch imports
# and loads c10.dll, its DllMain fails at runtime with WinError 1114
# because msvcp140/_1 expect matched versions.
#
# Instead, drop these DLLs from the bundle entirely. The NSIS installer
# bootstraps the VC++ Redistributable onto the target machine (see the
# Redist bootstrapper section in installer/windows/sayzo-agent.nsi), which
# deploys a matched set of DLLs into C:\Windows\System32 — Windows' normal
# DLL resolution then finds them and torch initializes cleanly. This
# matches how a non-frozen Python installation behaves.
#
# Only the top-level copies are removed; the ones under package subfolders
# (numpy.libs/, sklearn/.libs/, winrt/) are left alone because those
# packages reference them via explicit paths and os.add_dll_directory.
if sys.platform == "win32":
    _MSVC_RUNTIME_NAMES = {
        "msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll",
        "vcruntime140.dll", "vcruntime140_1.dll",
        "concrt140.dll",
    }

    def _is_top_level_msvc_runtime(entry):
        dest = entry[0]
        # Top-level entries have no path separator in dest.
        if "\\" in dest or "/" in dest:
            return False
        return dest.lower() in _MSVC_RUNTIME_NAMES

    _before = len(a.binaries)
    a.binaries = [b for b in a.binaries if not _is_top_level_msvc_runtime(b)]
    print(
        f"sayzo-agent.spec: stripped {_before - len(a.binaries)} top-level "
        f"MSVC runtime DLLs; Windows will resolve them from system VC++ Redist."
    )

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="sayzo-agent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,  # CLI: needs a console for first-run, login, --help.
    disable_windowed_traceback=False,
    argv_emulation=False,
    icon=app_icon,
)

# Windows: build a second, windowless exe for the background service so Task
# Scheduler can launch it at login without popping a console window. The CLI
# exe above is still needed for interactive commands (first-run, login, etc.).
collect_targets = [exe]
if sys.platform == "win32":
    exe_service = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="sayzo-agent-service",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        icon=app_icon,
    )
    collect_targets.append(exe_service)

coll = COLLECT(
    *collect_targets,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="sayzo-agent",
)

# ---------------------------------------------------------------------------
# macOS .app bundle
# ---------------------------------------------------------------------------

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Sayzo Agent.app",
        icon=app_icon,
        bundle_identifier="com.sayzo.agent",
        info_plist={
            "CFBundleShortVersionString": "0.1.0",
            "LSUIElement": True,  # hide from Dock (tray-only background app)
            "NSMicrophoneUsageDescription": (
                "Sayzo needs microphone access to capture your "
                "conversations for English coaching."
            ),
            "NSAudioCaptureUsageDescription": (
                "Sayzo records audio from other apps (e.g. Zoom, FaceTime, "
                "Meet) so it can transcribe conversations you're part of."
            ),
            # macOS 14.4 is the floor — CoreAudio Process Taps API is unavailable
            # on earlier releases.
            "LSMinimumSystemVersion": "14.4",
            # First-run GUI uses Apple Events to deep-link into System Settings
            # for granting microphone permission.
            "NSAppleEventsUsageDescription": (
                "Sayzo opens System Settings to help you grant microphone access."
            ),
            "LSApplicationCategoryType": "public.app-category.productivity",
        },
    )
