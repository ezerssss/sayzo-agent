# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Sayzo.

Build:
    pyinstaller sayzo-agent.spec

This produces a one-directory bundle under dist/sayzo-agent/ containing the
main executable plus all dependencies.  The platform installer (NSIS on Windows,
DMG on macOS) wraps this directory for end-user distribution.
"""
import sys
import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_submodules,
    copy_metadata,
)

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

# Silero VAD v5 ONNX model — vendored package data (v3.17+, see
# sayzo_agent/silero_onnx.py). Runs through onnxruntime directly; the
# silero-vad package + torch + torchaudio are no longer dependencies
# (that combination shipped ~320 MB of PyTorch to run this 2 MB model).
silero_onnx = Path("sayzo_agent/data/silero_vad.onnx")
if not silero_onnx.exists():
    raise SystemExit(f"vendored VAD model missing: {silero_onnx}")
datas.append((str(silero_onnx), "sayzo_agent/data"))

# macOS: bundle the pre-compiled audio-tap binary (CoreAudio Process Taps helper).
if sys.platform == "darwin":
    audio_tap = Path("sayzo_agent/capture/audio-tap/audio-tap")
    if audio_tap.exists():
        datas.append((str(audio_tap), "sayzo_agent/capture/audio-tap"))

    # macOS: bundle the pre-compiled audio-detect binary (per-process mic
    # attribution helper). Read-only — no Audio Capture permission needed,
    # no NSAudioCaptureUsageDescription required. Compiled in CI by the
    # parallel `Compile audio-detect Swift binary` step.
    audio_detect = Path("sayzo_agent/arm/audio-detect/audio-detect")
    if audio_detect.exists():
        datas.append((str(audio_detect), "sayzo_agent/arm/audio-detect"))

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

# livekit.rtc native FFI binary (WebRTC AEC3 — see sayzo_agent/aec.py).
# The Python loader at livekit/rtc/_ffi_client.py calls
# ``importlib.resources.files("livekit.rtc.resources") / "liblivekit_ffi.<ext>"``
# at runtime to locate the dylib/dll/so. PyInstaller's static analysis
# misses BOTH the resources sub-package (which holds the binary as
# package data) and the binary itself — so a frozen build without
# explicit bundling hits ``ImportError: failed to load liblivekit_ffi.*:
# No module named 'livekit.rtc.resources'`` on first AEC use. The
# ``sayzo-agent healthcheck`` CLI exercises ``cancel_echo`` against a
# synthetic buffer specifically so this kind of break-on-package-graph
# regression fails CI rather than the user.
datas += collect_data_files("livekit", include_py_files=False)

# ---------------------------------------------------------------------------
# Hidden imports — modules loaded lazily or via importlib that PyInstaller
# cannot detect through static analysis.
# ---------------------------------------------------------------------------

hiddenimports = [
    # Audio / capture
    "sounddevice",
    "_sounddevice_data",
    # VAD — onnxruntime session over the vendored model, lazy-imported
    # inside sayzo_agent/silero_onnx.py so the static scanner misses it.
    "onnxruntime",
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
    # Networking
    "httpx",
    # First-run GUI window
    "webview",
]

# livekit (WebRTC AEC3 wrapper, see sayzo_agent/aec.py). Lazy-imported
# inside ``aec._get_apm()`` and the ``importlib.resources`` lookup of
# the FFI binary references ``livekit.rtc.resources`` by name only —
# PyInstaller's static scanner misses both. ``collect_submodules``
# pulls the resources sub-package and every other module livekit
# requires at runtime (the protobuf shims under ``livekit.rtc._proto``
# are particularly easy to miss).
hiddenimports += collect_submodules("livekit")

# Windows-specific
if sys.platform == "win32":
    hiddenimports += [
        "pyaudiowpatch",
        "pystray._win32",
        # pywebview → EdgeChromium / WinForms backend (uses .NET via pythonnet)
        "webview.platforms.edgechromium",
        "webview.platforms.winforms",
        "clr_loader",
        "pythonnet",
        # pywin32 modules used by arm/platform_win.py's foreground/window
        # queries. These are simple at module level so PyInstaller usually
        # picks them up, but pin them here to be explicit.
        "pythoncom",
        "pywintypes",
        "win32gui",
        "win32process",
        "win32api",
    ]
    # arm/platform_win.py imports pycaw + comtypes + uiautomation inside
    # function bodies (try/except guarded) so PyInstaller's static scanner
    # can't see them. Use collect_submodules to pull the whole package
    # tree — comtypes has dynamic runtime codegen, pycaw.pycaw imports
    # several submodules via pycaw.api.*, and uiautomation lazy-imports
    # its generated UIA type-library bindings.
    hiddenimports += collect_submodules("pycaw")
    hiddenimports += collect_submodules("comtypes")
    hiddenimports += collect_submodules("uiautomation")

# macOS-specific
if sys.platform == "darwin":
    hiddenimports += [
        "pystray._darwin",
        # pywebview → Cocoa / WKWebView backend (via pyobjc)
        "webview.platforms.cocoa",
        "Foundation",
        "AppKit",
        "WebKit",
        # AVFoundation → AVCaptureDevice for Microphone TCC. Loaded lazily
        # by gui/setup/mac_permissions.prompt_microphone so PyInstaller's
        # static scanner doesn't see it without an explicit hint.
        "AVFoundation",
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
    # torch was removed as a dep in v3.17 (VAD runs on onnxruntime — see
    # sayzo_agent/silero_onnx.py) but noisereduce does a guarded
    # `try: import torch` for its optional torchgate path, which
    # PyInstaller's static scanner treats as a real import. In CI's fresh
    # venv torch isn't installed so this is a no-op; in a long-lived dev
    # venv where torch survives as a pip orphan, the soft-import silently
    # re-adds ~330 MB (plus numba/llvmlite riding the same class of
    # guarded imports). Excluding keeps local builds equal to CI builds.
    "torch",
    "torchaudio",
    "numba",
    "llvmlite",
    "sklearn",
    "sympy",
    "networkx",
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
# Multiple Python packages (numpy, pythonnet, onnxruntime) ship their own
# copies of msvcp140.dll / vcruntime140.dll, each from a different MSVC
# release. On load order, whichever runs its import first anchors the
# process to its version of MSVCP140.dll; any later-loaded native module
# compiled against a newer runtime then hits missing exports → access
# violation (0xC0000005) → WinError 1114.
#
# The original in-the-wild pairing was winrt (14.29) vs torch's c10.dll
# (14.40+) — both gone since v2.10 / v3.17 — but the multi-copy hazard is
# generic, so the strip stays.
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

# ---------------------------------------------------------------------------
# Prune Qt payload the HUD never uses (v3.17).
#
# The HUD subprocess imports QtCore / QtGui / QtWidgets / QtWebChannel /
# QtWebEngineWidgets / QtWebEngineCore only (no QML/Quick Python imports —
# Qt6Quick.dll etc. still ship because Qt6WebEngineCore.dll links them).
# Two prune classes, both verified by running the frozen bundle's
# `healthcheck` + `diagnose-notifications` after a build:
#
# 1. Translations: every .qm file (Qt falls back to its built-in English
#    source strings; all Sayzo-facing copy lives in our React apps) and
#    every qtwebengine_locales/*.pak except en-US.pak (Chromium-internal
#    strings — form controls, context menus; QtWebEngine falls back to
#    en-US for missing locales).
# 2. Leaf Qt modules PyInstaller's Qt hooks over-collect that nothing in
#    the WebEngine dependency chain links: 3D, Charts, data-vis, Quick3D,
#    virtual keyboard, QtPdf, and the QML-import-only QuickControls2 /
#    QuickDialogs2 family (only reachable through QML `import` statements,
#    and the HUD has no QML).
# 3. The PySide6/qml/ plugin tree — QML-engine imports only. The HUD is
#    QtWebEngineWidgets; Qt6Quick.dll stays (Qt6WebEngineCore.dll links
#    it) but nothing ever evaluates a QML document.
# 4. QtWebEngine *.debug.pak / *.debug.bin resources (only loaded by
#    debug builds of WebEngineCore — we ship release) and the devtools
#    resource pak (only loaded when remote debugging is enabled; the HUD
#    never enables it — the setup window's debug devtools is pywebview/
#    EdgeChromium, not QtWebEngine).
#
# Deliberately KEPT: opengl32sw.dll (software-GL fallback — HUD must
# render on GPU-less / broken-driver / RDP machines), icudtl.dat +
# qtwebengine_resources*.pak + v8_context_snapshot.bin (mandatory),
# Qt6ShaderTools (Quick's RHI shader pipeline), all .pyd bindings.
# ---------------------------------------------------------------------------

_QT_PRUNE_DLL_PREFIXES = (
    "qt63d", "qt6charts", "qt6datavisualization", "qt6graphs",
    "qt6quick3d", "qt6virtualkeyboard", "qt6pdf",
    "qt6quickcontrols2", "qt6quickdialogs2", "qt6quicktemplates2",
    "qt6quicklayouts", "qt6quickparticles", "qt6quickshapes",
    "qt6quicktest", "qt6quickeffects", "qt6labs",
)


def _qt_prune(entry):
    dest = entry[0].replace("\\", "/")
    low = dest.lower()
    if "pyside6" not in low:
        return False
    if "/translations/" in low or low.startswith("pyside6/translations"):
        if low.endswith("qtwebengine_locales/en-us.pak"):
            return False
        return True
    if "/qml/" in low or low.startswith("pyside6/qml"):
        return True
    if low.endswith((".debug.pak", ".debug.bin")):
        return True
    if low.endswith("qtwebengine_devtools_resources.pak"):
        return True
    base = low.rsplit("/", 1)[-1]
    return base.startswith(_QT_PRUNE_DLL_PREFIXES)


# Non-Qt hook over-collection (v3.17). Neither is reachable from the
# import graph (verified via xref + grepping every dep package):
#   - Pythonwin/ (mfc140u.dll + win32ui.pyd, ~7 MB) — pywin32's MFC GUI
#     toolkit; the agent uses win32gui/win32process/win32api only.
#   - PySide6/QtOpenGL.pyd (~9 MB) — Python binding nothing imports;
#     Qt6OpenGL.dll / Qt6OpenGLWidgets.dll (C-level link deps of the
#     Quick/WebEngine DLLs) are deliberately KEPT.
# `sayzo-agent healthcheck` exercises uiautomation + the HUD imports
# against the built bundle, so an over-aggressive prune here fails CI.
_EXTRA_PRUNE_PREFIXES = ("pythonwin/",)
_EXTRA_PRUNE_EXACT = ("pyside6/qtopengl.pyd",)


def _bundle_prune(entry):
    if _qt_prune(entry):
        return True
    low = entry[0].replace("\\", "/").lower()
    return low.startswith(_EXTRA_PRUNE_PREFIXES) or low in _EXTRA_PRUNE_EXACT


for _attr in ("binaries", "datas"):
    _toc = getattr(a, _attr)
    _dropped = [e[0] for e in _toc if _bundle_prune(e)]
    setattr(a, _attr, [e for e in _toc if not _bundle_prune(e)])
    if _dropped:
        print(
            f"sayzo-agent.spec: pruned {len(_dropped)} unused {_attr} "
            f"entries (Qt translations / leaf modules / hook over-collection)"
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
        name="Sayzo.app",
        icon=app_icon,
        bundle_identifier="com.sayzo.agent",
        info_plist={
            "CFBundleShortVersionString": _sayzo_version,
            "LSUIElement": True,  # hide from Dock (tray-only background app)
            # PyInstaller's BUNDLE() defaults `LSBackgroundOnly=True`
            # whenever the EXE block has `console=True` (see PyInstaller
            # building/osx.py: "Setting EXE console=True implies
            # LSBackgroundOnly=True"). We need console=True so CLI
            # commands (`sayzo-agent run`, `sayzo-agent first-run`, etc.)
            # work from a Terminal, but `LSBackgroundOnly=True` makes
            # macOS classify the bundle as an agent app that won't show
            # UI — and the TCC subsystem then refuses to present its
            # permission dialog. Symptom: every Mac user reports the
            # mic-permission dialog never appears and
            # `AVCaptureDevice.requestAccess` silent-denies in <10 ms.
            # This explicit override forces LSBackgroundOnly=False so
            # the merged Info.plist matches a real LSUIElement (menu-
            # bar) app — which CAN show TCC dialogs. CFBundleDisplayName
            # is also explicit (Apple Forum 30364: "if it is null, the
            # system will never know what app is asking for permission").
            "LSBackgroundOnly": False,
            "CFBundleDisplayName": "Sayzo",
            "CFBundleName": "Sayzo",
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

    # SMAppService.agent requires the LaunchAgent plist to live inside the
    # app bundle at Contents/Library/LaunchAgents/<label>.plist. When the
    # plist is registered through SMAppService instead of being dropped
    # straight into ~/Library/LaunchAgents/, macOS attributes the BTM
    # "Background items added — '…' added items that can run in the
    # background" notification (and the System Settings -> Login Items
    # entry) to the OWNING APP rather than the Developer-ID team
    # identity. Without this file in this exact location, SMAppService
    # falls back to the team name ("Sheen Santos Capadngan") which is
    # confusing for end users.
    #
    # PyInstaller's BUNDLE() only writes Info.plist; auxiliary plists
    # have to be copied in post-build. Doing it here in the spec means
    # the plist is in place BEFORE the CI signs and notarizes the
    # bundle (codesign hashes everything under Contents/, including
    # Library/LaunchAgents/, so the file must exist pre-sign).
    _bundle_path = Path("dist") / "Sayzo.app"
    _launch_agents_dir = _bundle_path / "Contents" / "Library" / "LaunchAgents"
    _launch_agents_dir.mkdir(parents=True, exist_ok=True)
    _src_plist = Path("installer/macos/com.sayzo.agent.plist")
    _dst_plist = _launch_agents_dir / "com.sayzo.agent.plist"
    _dst_plist.write_bytes(_src_plist.read_bytes())
    print(f"sayzo-agent.spec: bundled LaunchAgent plist at {_dst_plist}")

    # Auto-update apply helper (Phase B). Lives at Contents/Resources/ so the
    # Python wrapper in sayzo_agent/update_apply_mac.py can locate it via
    # sys.executable -> ../Resources/apply_update.sh. PyInstaller's `datas`
    # mechanism copies files but doesn't preserve the executable bit on every
    # filesystem; the post-build write + chmod pattern guarantees +x survives
    # the path from a git-checked-out source file to the signed .app. Must
    # happen BEFORE codesign — Apple's signature covers every byte under
    # Contents/.
    _resources_dir = _bundle_path / "Contents" / "Resources"
    _resources_dir.mkdir(parents=True, exist_ok=True)
    _src_apply = Path("installer/macos/apply_update.sh")
    _dst_apply = _resources_dir / "apply_update.sh"
    _dst_apply.write_bytes(_src_apply.read_bytes())
    _dst_apply.chmod(0o755)
    print(f"sayzo-agent.spec: bundled apply_update.sh at {_dst_apply}")

