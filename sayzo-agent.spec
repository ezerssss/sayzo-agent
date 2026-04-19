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
]

# Windows-specific
if sys.platform == "win32":
    hiddenimports += [
        "pyaudiowpatch",
        "pystray._win32",
        # desktop-notifier → WinRT backend
        "winrt",
        "winsdk",
    ]

# macOS-specific
if sys.platform == "darwin":
    hiddenimports += [
        "pystray._darwin",
        # desktop-notifier → UNUserNotificationCenter backend
        "rubicon",
        "rubicon.objc",
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
        },
    )
