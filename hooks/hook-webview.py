# PyInstaller hook for pywebview.
#
# Pulls in all of webview's package data (HTML/JS/CSS used by the various
# platform backends) and submodules. The Edge/Chromium backend on Windows
# resolves WebView2 DLLs via clr_loader at runtime, so its data files need
# to land in the bundle too.
from PyInstaller.utils.hooks import collect_all, collect_data_files

datas, binaries, hiddenimports = collect_all("webview")

try:
    datas += collect_data_files("clr_loader")
except Exception:
    # clr_loader is only present on Windows (pythonnet dependency). Ignore
    # cleanly on platforms where it's not installed.
    pass
