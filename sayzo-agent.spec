# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Sayzo Agent.

Build:
    pyinstaller sayzo-agent.spec

This produces a one-directory bundle under dist/sayzo-agent/ containing the
main executable plus all dependencies.  The platform installer (NSIS on Windows,
DMG on macOS) wraps this directory for end-user distribution.
"""
import sys
import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import copy_metadata

block_cipher = None

# ---------------------------------------------------------------------------
# Version — single source of truth is pyproject.toml. Phase A auto-update
# compares the runtime __version__ (resolved via importlib.metadata) against
# latest.json. Every place that carries a version string (this spec's
# CFBundleShortVersionString, NSIS's PRODUCT_VERSION via /DPRODUCT_VERSION in
# CI, and __init__.py's importlib.metadata read) derives from this file, so a
# release is a one-line edit to pyproject.toml followed by a push.
# ---------------------------------------------------------------------------

with open("pyproject.toml", "rb") as _f:
    _sayzo_version = tomllib.load(_f)["project"]["version"]

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

# Bundle sayzo-agent's own dist-info so `importlib.metadata.version("sayzo-agent")`
# resolves inside the frozen .app / .exe — drives __version__ in __init__.py
# and the Click `--version` flag.
datas += copy_metadata("sayzo-agent")

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

# Tray icon — sayzo_agent/gui/tray.py loads this from the bundled assets
# directory at runtime. Same source file also supplies the .ico/.icns used
# for the Windows exe / macOS .app bundle icon below.
tray_logo = Path("installer/assets/logo.png")
if tray_logo.exists():
    datas.append((str(tray_logo), "installer/assets"))

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
# Strip MSVC C++ runtime DLLs from anywhere in the bundle.
#
# Multiple Python packages (numpy, sklearn, llvmlite, winrt, pythonnet) ship
# their own copies of msvcp140.dll / vcruntime140.dll, each from a different
# MSVC release. On load order, whichever runs its import first (winrt via
# desktop-notifier, in our case) anchors the process to its version of
# MSVCP140.dll. When torch later loads c10.dll — compiled against a newer
# MSVCP140 — Windows returns the already-loaded older module instead of
# the newer system copy. c10.dll's DllMain calls a function that doesn't
# exist in the older DLL → access violation (0xC0000005) → WinError 1114.
#
# Observed in the wild: winrt ships 14.29 (from VS 2019). torch's c10.dll
# wants 14.40+. Load order: winrt first (at import), torch second (via
# silero_vad → torch), boom.
#
# Fix: remove every bare copy from the bundle. The NSIS installer
# bootstraps the VC++ Redistributable onto the target machine (see the
# Redist bootstrapper in installer/windows/sayzo-agent.nsi), which puts a
# matched set of DLLs in C:\Windows\System32. Windows' normal search path
# resolves them from there — the same way a non-frozen Python install
# behaves. Torch initializes cleanly.
#
# Hash-suffixed copies (numpy.libs\msvcp140-<hash>.dll etc.) are left
# alone — those packages import them via delvewheel's explicit
# hashed-name mechanism, and removing them would break numpy.
if sys.platform == "win32":
    _MSVC_RUNTIME_NAMES = {
        "msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll",
        "vcruntime140.dll", "vcruntime140_1.dll",
        "concrt140.dll",
    }

    def _is_msvc_runtime(entry):
        # Match by basename regardless of subdir so the copy inside
        # winrt/ and similar package subfolders also gets stripped. The
        # case-insensitive comparison handles winrt/MSVCP140.dll as well
        # as top-level msvcp140.dll.
        return Path(entry[0]).name.lower() in _MSVC_RUNTIME_NAMES

    _before = len(a.binaries)
    _stripped = [b[0] for b in a.binaries if _is_msvc_runtime(b)]
    a.binaries = [b for b in a.binaries if not _is_msvc_runtime(b)]
    print(
        f"sayzo-agent.spec: stripped {_before - len(a.binaries)} MSVC runtime "
        f"DLLs (system VC++ Redist supplies them): {_stripped}"
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
            "CFBundleShortVersionString": _sayzo_version,
            "LSUIElement": True,  # hide from Dock (tray-only background app)
            # Usage descriptions are the copy the OS shows in its TCC
            # dialogs. They MUST match Sayzo's armed-only invariant: mic /
            # audio-capture / automation are only used during a capture the
            # user explicitly started (hotkey) or agreed to (meeting-detect
            # prompt). Anything that sounds like passive always-on listening
            # is a bug — it contradicts the product.
            "NSMicrophoneUsageDescription": (
                "Sayzo opens the microphone only when you start a "
                "recording. It stays off otherwise."
            ),
            "NSAudioCaptureUsageDescription": (
                "So Sayzo can hear the other person in your meetings "
                "(Zoom, Meet, FaceTime, etc.) — only while you're recording."
            ),
            # macOS 14.4 is the floor — CoreAudio Process Taps API is unavailable
            # on earlier releases.
            "LSMinimumSystemVersion": "14.4",
            # AppleEvents is used to read the active tab URL in Chrome /
            # Safari / Edge / Arc / Brave — we need it to tell whether
            # you're in a web meeting vs. just browsing. We never read
            # page contents.
            "NSAppleEventsUsageDescription": (
                "So Sayzo can tell when you're in a web meeting (Google "
                "Meet, Teams, etc.). Only the tab's URL — never what's on "
                "the page."
            ),
            "LSApplicationCategoryType": "public.app-category.productivity",
        },
    )
