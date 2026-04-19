# PyInstaller hook for desktop-notifier.
#
# desktop_notifier.common imports `importlib.resources.files("desktop_notifier.resources")`
# to load packaged icons/templates at runtime. PyInstaller's static analysis
# doesn't pick up resource-only subpackages, so we explicitly collect the
# whole package.
from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = collect_all("desktop_notifier")
