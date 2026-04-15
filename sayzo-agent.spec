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

# macOS: bundle the pre-compiled sck-tap binary.
if sys.platform == "darwin":
    sck_tap = Path("sayzo_agent/capture/sck-tap/sck-tap")
    if sck_tap.exists():
        datas.append((str(sck_tap), "sayzo_agent/capture/sck-tap"))

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
]

# Windows-specific
if sys.platform == "win32":
    hiddenimports += [
        "pyaudiowpatch",
        "pystray._win32",
    ]

# macOS-specific
if sys.platform == "darwin":
    hiddenimports += [
        "pystray._darwin",
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
            "NSAppleEventsUsageDescription": (
                "Sayzo needs Screen Recording access to capture system audio."
            ),
        },
    )
