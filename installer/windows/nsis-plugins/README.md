# Vendored NSIS plugins

NSIS on CI (chocolatey `nsis` package) ships with only the built-in plugins.
We vendor third-party plugin DLLs here so the installer builds are
self-contained and don't depend on flaky mirrors at build time.

## What goes here

- `x86-unicode/ApplicationID.dll` — sets AUMID on the Start Menu shortcut so
  Windows 10 WinRT toast notifications fire. Without this, the
  `desktop-notifier` package silently drops toasts on every Win10 build
  (1809 through 22H2). Win11 doesn't need it.

## Where to get ApplicationID.dll

1. Visit the NSIS Wiki page: https://nsis.sourceforge.io/ApplicationID_plug-in
2. Click the **Download** link near the top.
3. If that's broken, try any of these mirrors (current as of writing):
   - https://github.com/connoryan/NSIS_Plugins_ApplicationID (unofficial fork)
   - Any NSIS-based installer project that bundles it, e.g. Squirrel.Windows
4. Extract the ZIP; the DLL lives at `Release/ApplicationID.dll` inside the archive.
5. Drop it at `installer/windows/nsis-plugins/x86-unicode/ApplicationID.dll`.

The `.gitignore` already has an exception for this directory. `git add` the
DLL and commit.

## How the NSIS script finds it

`installer/windows/sayzo-agent.nsi` declares at the top:

```nsis
!addplugindir /x86-unicode "nsis-plugins\x86-unicode"
```

which tells `makensis` to search this directory in addition to the default
plugins dir. Any DLL dropped in here becomes callable via `PluginName::Method`.
