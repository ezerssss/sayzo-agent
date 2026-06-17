"""Pre-generate comtypes type-library stubs at build time.

Runs in CI before PyInstaller. The end goal: comtypes.gen submodules
ship as static .py files in the frozen bundle so the app never has to
regenerate them at runtime.

Why this exists
---------------

``comtypes`` generates Python stubs from COM type libraries on first
use, into ``comtypes/gen/``. On a normal Python install the stubs
land alongside the comtypes package — writable, persistent. On a
PyInstaller bundle in ``C:\\Program Files\\Sayzo\\``, the install
directory is read-only, so comtypes falls back to
``%TEMP%\\comtypes_cache\\<exe>-<pid>`` for the stubs.

That fallback is the failure mode user logs keep showing:

    INFO  comtypes.client._code_cache  Imported existing <module 'comtypes.gen' from 'C:\\Program Files\\Sayzo\\_internal\\comtypes\\gen\\__init__.py'>
    INFO  comtypes.client._code_cache  Creating writeable comtypes cache directory: 'C:\\Users\\USER\\AppData\\Local\\Temp\\comtypes_cache\\sayzo-agent-service-312'
    INFO  comtypes.client._generate  Could not import comtypes.gen._944DE083_8FB8_45CF_BCB7_C477ACB2F897_0_1_0: ...
    INFO  comtypes.client._generate  # Generating comtypes.gen._944DE083_8FB8_45CF_BCB7_C477ACB2F897_0_1_0
    INFO  comtypes.client._generate  # Generating comtypes.gen.UIAutomationClient

%TEMP% is volatile (Storage Sense, antivirus, manual cleanup, profile
reset). When the cache disappears, the next launch tries to regenerate;
any failure during regeneration becomes an unhandled exception. The
right answer is to never regenerate at the user's machine.

By calling ``comtypes.client.GetModule`` here in CI before PyInstaller
runs, we materialize the generated stubs as static .py files inside
the venv's ``comtypes/gen/`` directory. PyInstaller's
``collect_submodules('comtypes')`` (in sayzo-agent.spec) then bundles
them. The end-user bundle ships UIAutomationClient.py and friends as
already-generated modules — the runtime ``import`` succeeds, no
generation needed, no %TEMP% touched.

Type libraries we pre-bake
--------------------------

* ``UIAutomationCore.dll`` — the IUIAutomation COM API used in
  ``arm/platform_win.py`` for browser-tab URL extraction. The user
  log shows this generating ``UIAutomationClient`` plus its dependency
  GUID stub ``_944DE083_*``.

* ``stdole2.tlb`` — the OLE Automation type library, a transitive
  dependency UIAutomation pulls in (the second batch of ``Generating``
  log lines: ``_00020430_*`` + ``stdole``). Pre-baking explicitly
  rather than relying on transitive generation makes the dependency
  graph explicit and gives a clear failure mode if stdole2 ever moves.

This script is a no-op on non-Windows platforms — comtypes itself
fails to import there, and the macOS / Linux builds don't ship pycaw
or UIAutomation anyway.
"""
from __future__ import annotations

import sys


def main() -> int:
    if sys.platform != "win32":
        print("[prebake_comtypes] non-Windows platform; nothing to do.")
        return 0

    try:
        import comtypes.client
    except ImportError as e:
        print(f"[prebake_comtypes] comtypes not installed: {e}", file=sys.stderr)
        return 1

    typelibs = [
        "UIAutomationCore.dll",
        "stdole2.tlb",
    ]
    for tlb in typelibs:
        print(f"[prebake_comtypes] generating stubs for {tlb} ...")
        try:
            mod = comtypes.client.GetModule(tlb)
        except Exception as e:
            print(
                f"[prebake_comtypes] FAILED for {tlb}: {e}", file=sys.stderr,
            )
            return 1
        print(f"[prebake_comtypes]   -> generated {mod.__name__}")

    # Print where the stubs landed so CI logs make the location obvious
    # if PyInstaller fails to pick them up later.
    import comtypes
    gen_dir = next(
        (p for p in comtypes.__path__ for sub in ["gen"] if p),
        None,
    )
    if gen_dir:
        from pathlib import Path
        gen_path = Path(gen_dir) / "gen"
        if gen_path.exists():
            files = sorted(p.name for p in gen_path.glob("*.py"))
            print(f"[prebake_comtypes] gen/ contents ({len(files)} files): {files[:10]}")
            if len(files) > 10:
                print(f"[prebake_comtypes]   ... and {len(files) - 10} more")

    print("[prebake_comtypes] OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
