"""Redirect comtypes' runtime stub cache to a stable, per-user location.

Background
----------

``comtypes`` generates Python wrappers for COM type libraries on first
use. The cache logic in ``comtypes.client._code_cache._find_gen_dir``
picks a destination in this order:

1. ``<comtypes-install>/gen/``                       — if writable
2. (frozen exe) ``%TEMP%\\comtypes_cache\\<exe>-<py>`` — fallback

For our PyInstaller-frozen build under
``%LOCALAPPDATA%\\Programs\\Sayzo\\`` (v2.8.0+; pre-v2.8.0 was
``C:\\Program Files\\Sayzo\\``), the install directory is theoretically
writable now, but ``comtypes`` still treats PyInstaller bundle paths as
immutable, so the fallback at ``%TEMP%`` is what gets used. ``%TEMP%`` is volatile — Storage Sense, antivirus,
profile resets, and manual "Disk Cleanup" all blow it away. The next
launch tries to regenerate the stubs, and any failure during regen
(typelib missing on this Windows version, race with AV, write-perm
glitch) surfaces to the user as a generic OS-level "unhandled
exception" dialog with no detail.

The CI build also pre-bakes the typelibs we know we need
(``scripts/prebake_comtypes.py`` runs before PyInstaller, materializing
``comtypes.gen.UIAutomationClient`` + ``comtypes.gen.stdole`` as static
.py files inside the bundle). With pre-baked stubs the runtime
fallback rarely fires — this module is defense in depth for cases
where new Windows versions or future Sayzo code introduce a typelib
we didn't anticipate.

Mechanism
---------

We append a stable per-user directory (``data_dir/comtypes_cache``)
to ``comtypes.gen.__path__`` before any pycaw / uiautomation /
``comtypes.client.GetModule`` call. ``_find_gen_dir`` returns
``gen.__path__[-1]`` whenever any path in the list is writable, so
appending ours makes it the canonical generation target — the
``%TEMP%`` fallback only runs if our directory is also unwritable
(disk full, permissions broken — at which point the user has bigger
problems).

Must be invoked **before** any module that pulls in comtypes (notably
``arm.platform_win`` via the whitelist watcher and
``capture.system_win_process``). The CLI entry points call it right
after logging setup.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)

_CONFIGURED = False


def configure_comtypes_cache(cache_dir: Path) -> None:
    """Point comtypes' runtime stub cache at ``cache_dir``.

    Idempotent. No-op on non-Windows platforms (comtypes is
    Windows-only) and when comtypes isn't importable (dev environment
    without pycaw / uiautomation installed).
    """
    global _CONFIGURED
    if _CONFIGURED or sys.platform != "win32":
        return

    try:
        from comtypes import gen  # noqa: F401  -- imported for its __path__ side-effect
    except ImportError:
        log.debug("[comtypes] not installed; cache redirect skipped")
        return

    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning(
            "[comtypes] failed to create cache dir %s: %s — falling back to default",
            cache_dir, e,
        )
        return

    cache_str = str(cache_dir.resolve())

    # Append our writable directory to ``comtypes.gen.__path__``.
    # comtypes' ``_find_gen_dir`` uses ``gen.__path__[-1]`` as the
    # canonical generation target whenever any path in the list is
    # writable; appending ours makes it the destination of new stubs
    # without disturbing the bundled / pre-baked ones (those resolve
    # via the earlier entries in ``__path__``).
    from comtypes import gen as _gen  # re-import for the binding inside the function
    gen_path = list(_gen.__path__)
    if cache_str not in gen_path:
        gen_path.append(cache_str)
        _gen.__path__ = gen_path  # type: ignore[assignment]
        log.info("[comtypes] runtime cache redirected to %s", cache_str)

    _CONFIGURED = True
