"""Sayzo local listening agent."""
from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("sayzo-agent")
except PackageNotFoundError:
    # Running from a source tree without an installed dist-info (rare — covered
    # by `pip install -e .` in normal dev; PyInstaller bundles dist-info via
    # copy_metadata in sayzo-agent.spec). Keep a distinguishable fallback so
    # any update-check comparison fails safe rather than claiming parity.
    __version__ = "0.0.0-dev"
