; Sayzo NSIS Installer
; Installs the PyInstaller bundle, creates auto-start Task Scheduler entry,
; and registers with Add/Remove Programs.

; Build a Unicode installer. NOTE: NSIS 3 does NOT auto-detect UTF-8 source
; files without a BOM (it falls back to the active code page, Windows-1252
; for English locales), so this directive alone does not make em-dashes /
; smart punctuation in source render correctly at install time. To stay
; portable across CI hosts and editor settings, keep this file pure ASCII
; (use plain hyphens, straight quotes, etc.). Earlier versions had "Sayzo +
; em-dash" in the welcome title and shipped the mojibake "Sayzo a-euro".
Unicode true

!include "MUI2.nsh"
!include "nsDialogs.nsh"
!include "WinMessages.nsh"
!include "WordFunc.nsh"

; Vendored NSIS plugins (see installer/windows/nsis-plugins/README.md).
; We ship ApplicationID.dll so toast notifications work on Win10 - the
; chocolatey `nsis` package on CI doesn't bundle third-party plugins.
!addplugindir /x86-unicode "nsis-plugins\x86-unicode"

; ---------------------------------------------------------------------------
; Configuration
; ---------------------------------------------------------------------------

!define PRODUCT_NAME "Sayzo"
!define PRODUCT_PUBLISHER "Sayzo"
; PRODUCT_VERSION is normally injected by CI via `makensis /DPRODUCT_VERSION=$VERSION ...`
; where $VERSION is read from pyproject.toml (the single source of truth for Phase A
; auto-update). The !ifndef guard keeps local/dev `makensis ...` invocations working
; without requiring the flag - they'll just produce an installer labelled 0.0.0-dev.
!ifndef PRODUCT_VERSION
    !define PRODUCT_VERSION "0.0.0-dev"
!endif
!define PRODUCT_EXE "sayzo-agent.exe"
!define SERVICE_EXE "sayzo-agent-service.exe"

; Per-user install (Stage 0 of the auto-update plan). %LOCALAPPDATA%\Programs
; mirrors Slack, Discord, VS Code, Microsoft Teams, GitHub Desktop etc. - any
; modern Win app whose updater wants to swap files without an admin prompt.
; Switching here means:
;   - RequestExecutionLevel user (no UAC on install / update / uninstall)
;   - Task Scheduler entry runs at the user's normal privilege level (no
;     /RL HIGHEST). Sayzo's runtime needs WASAPI loopback + pycaw mic-session
;     enumeration + pynput global hotkey + UIAutomation browser-tab reads —
;     none of those require elevation in practice (confirmed in dev runs).
;   - Uninstall registry entry lives in HKCU (Settings -> Apps shows it for
;     the current user only — appropriate for a per-user install).
!define INSTALL_DIR "$LOCALAPPDATA\Programs\Sayzo"
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}"

; Legacy admin install location (pre-v2.8.0). The migration block in .onInit
; detects this and runs the legacy uninstaller elevated before the per-user
; install proceeds. After v2.8.0 has rolled to all users this block can be
; deleted entirely.
!define LEGACY_INSTALL_DIR "$PROGRAMFILES64\Sayzo"
!define LEGACY_UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}"

Name "${PRODUCT_NAME} ${PRODUCT_VERSION}"
OutFile "sayzo-setup.exe"
InstallDir "${INSTALL_DIR}"
RequestExecutionLevel user
SetCompressor /SOLID lzma
Icon "..\..\installer\assets\logo.ico"
UninstallIcon "..\..\installer\assets\logo.ico"

; ---------------------------------------------------------------------------
; UI
; ---------------------------------------------------------------------------

!define MUI_ABORTWARNING
!define MUI_ICON "..\..\installer\assets\logo.ico"
!define MUI_UNICON "..\..\installer\assets\logo.ico"

; Welcome page - introduces the armed-only model so users understand what
; they're installing before files hit disk. Copy is the approved draft from
; installer/copy_draft.md; mirror any future revisions of that file here.
!define MUI_WELCOMEPAGE_TITLE "Sayzo - the English speaking coach you bring to your meetings."
!define MUI_WELCOMEPAGE_TEXT "Sayzo captures conversations from your meetings and turns them into personalized English-speaking drills. It only listens when you say so: press a keyboard shortcut, or say yes to a prompt when Sayzo notices you're in a meeting.$\r$\n$\r$\nYour microphone stays off until then."
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_INSTFILES

; Finish page with a "Launch Sayzo" checkbox that runs the windowless
; service exe (console=False per sayzo-agent.spec). The service detects
; missing setup signals and opens its own first-run GUI if needed.
; The MUI_FINISHPAGE_RUN_* defines must come BEFORE MUI_PAGE_FINISH.
!define MUI_FINISHPAGE_TITLE "Sayzo is ready."
!define MUI_FINISHPAGE_TEXT "We'll open a short setup window to get Sayzo ready. Nothing records until you say so."
!define MUI_FINISHPAGE_RUN "$INSTDIR\${SERVICE_EXE}"
; --force-setup forces the GUI on every install for a visual confirmation,
; including upgrade re-installs. App.tsx::initialScreen short-circuits to
; Done when detect_setup says is_complete=true; the user clicks Got it.
!define MUI_FINISHPAGE_RUN_PARAMETERS "service --force-setup"
!define MUI_FINISHPAGE_RUN_TEXT "Launch Sayzo"
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

; ---------------------------------------------------------------------------
; Legacy admin-install migration (Stage 0 of the auto-update plan).
;
; A user upgrading from v2.7.x has Sayzo installed under Program Files via the
; admin installer. v2.8.0+ installs to %LOCALAPPDATA% with no admin needed.
; We detect the legacy install via its HKLM uninstall key and run the OLD
; uninstall.exe elevated via ShellExecute "runas" -- the user sees ONE UAC
; prompt total for the migration; every future update is admin-free.
;
; Caveats:
;   - If the legacy agent is still running when the legacy uninstall fires,
;     locked files (sayzo-agent.exe + loaded DLLs) may remain orphan in
;     C:\Program Files\Sayzo. They're harmless: the Task Scheduler entry was
;     deleted, no HKLM/HKCU reference remains, nothing will relaunch them.
;     The user can manually delete the folder if they care about disk space.
;   - This block is dead code once every v2.7.x user has migrated. Safe to
;     remove in a future release.
; ---------------------------------------------------------------------------

Function MigrateLegacyInstall
    ; Pre-v2.8.0 installs wrote their uninstall metadata to the HKLM uninstall
    ; key. v2.8.0+ writes to HKCU, so an HKLM read returning a string here is
    ; an unambiguous "legacy admin install present" signal.
    ReadRegStr $0 HKLM "${LEGACY_UNINSTALL_KEY}" "UninstallString"
    StrCmp $0 "" no_legacy

    ; Strip surrounding quotes from the UninstallString. NSIS's WriteRegStr
    ; quotes the path for shell-friendliness (so users running it from a
    ; cmd.exe with spaces in the path work), but ExecShellWait takes the
    ; path and args as separate args, so the quotes get in the way.
    StrCpy $1 $0 1
    StrCmp $1 '"' 0 no_strip
        StrCpy $0 $0 "" 1
        StrCpy $0 $0 -1
    no_strip:

    DetailPrint "Sayzo is moving to a per-user install location."
    DetailPrint "Approve the next prompt to clean up the previous admin install."
    DetailPrint "(This is the last UAC prompt you'll see for Sayzo updates.)"

    ; /S = silent. ExecShellWait blocks until the elevated subprocess exits.
    ; Errors from the legacy uninstaller are non-fatal — even a partial cleanup
    ; (e.g. some locked files left behind) puts us in a strictly better state
    ; than before this migration ran. We proceed to the per-user install
    ; either way; the new install never references the legacy location.
    ExecShellWait "runas" "$0" "/S" SW_HIDE

    no_legacy:
FunctionEnd

; ---------------------------------------------------------------------------
; Install
; ---------------------------------------------------------------------------

Section "Install"
    Call MigrateLegacyInstall

    ; v2.8.2: install-in-progress lock. The agent's boot path
    ; (``_wait_for_install_lock_release`` in ``__main__.py``) reads this
    ; file and waits, so a Start-Menu click during the install dead zone
    ; doesn't race File /r on the bundle. mtime doubles as a staleness
    ; signal — a crashed installer's orphan lock is treated as dead after
    ; 5 min and overwritten on the next install.
    ;
    ; Only relevant for update installs (where a running agent might
    ; race). On fresh installs the data dir doesn't exist yet; skip
    ; silently.
    IfFileExists "$PROFILE\.sayzo\agent\*.*" install_lock_write install_lock_skip
    install_lock_write:
        FileOpen $1 "$PROFILE\.sayzo\agent\install_in_progress.lock" w
        FileWrite $1 "v${PRODUCT_VERSION}"
        FileClose $1
    install_lock_skip:

    SetOutPath "$INSTDIR"

    ; Stop any running agent before overwriting its exes. Without this, a user
    ; re-running the installer (the Phase A "Download update" path) hits
    ; ERROR_SHARING_VIOLATION on sayzo-agent.exe / sayzo-agent-service.exe mid
    ; File /r and the install aborts with files half-replaced. Idempotent -
    ; both commands exit non-zero on "task/process not found" and we ignore
    ; the return. /T also kills child processes (e.g. the Settings pywebview
    ; subprocess holding its own DLL handles). The Task Scheduler task itself
    ; is re-created below with /F.
    nsExec::ExecToLog 'schtasks /End /TN "Sayzo"'
    nsExec::ExecToLog 'taskkill /IM sayzo-agent-service.exe /F /T'
    nsExec::ExecToLog 'taskkill /IM sayzo-agent.exe /F /T'

    ; Wait for Windows to fully release the killed processes' file handles
    ; before File /r tries to overwrite them. taskkill /F returns immediately
    ; but the kernel still needs time to tear down DLL imports and close the
    ; main exe handle - typically 100-500 ms but observed up to 1.5 s on
    ; busy systems. Without this gap, File /r races the cleanup: most files
    ; replace fine, but sayzo-agent.exe (locked the longest, since it's the
    ; loader for python3xx.dll plus every loaded .pyd) silently doesn't get
    ; overwritten, and the user ends up with a registry/uninstaller that
    ; says vN+1 while the running exe still reports vN.
    ;
    ; Then explicitly Delete the exes as a probe: if a handle is still open
    ; past the first sleep, Delete sets the error flag, and we retry with
    ; a longer sleep before falling through to File /r. This makes the
    ; partial-replace failure mode self-recovering instead of silently
    ; corrupting the install.
    Sleep 2000
    ClearErrors
    Delete "$INSTDIR\${PRODUCT_EXE}"
    Delete "$INSTDIR\${SERVICE_EXE}"
    IfErrors 0 +4
        DetailPrint "Sayzo executables still locked - waiting 3 more seconds..."
        Sleep 3000
        Delete "$INSTDIR\${PRODUCT_EXE}"
        Delete "$INSTDIR\${SERVICE_EXE}"

    ; Wipe _internal/ before extracting. NSIS File /r writes new files but
    ; does NOT remove files absent from the source dir; without this, stale
    ; dist-info dirs from prior installs coexist and importlib.metadata
    ; picks whichever the FS iterates first (NTFS = oldest), so About
    ; reports the old version after upgrade.
    RMDir /r "$INSTDIR\_internal"

    ; Copy the entire PyInstaller bundle directory.
    ; The NSIS script must be invoked from the repo root where dist/sayzo-agent/ exists.
    File /r "..\..\dist\sayzo-agent\*.*"

    ; Bootstrap WebView2 Evergreen Runtime when missing. pywebview's
    ; edgechromium backend (used by the first-run GUI) needs it. Win10 21H2+
    ; and all Win11 ship it preinstalled; older Win10 doesn't.
    ;
    ; Drop MicrosoftEdgeWebview2Setup.exe (~120 KB, downloads from MS CDN at
    ; install time) into installer/windows/ to enable bundling. Get it from:
    ;   https://go.microsoft.com/fwlink/p/?LinkId=2124703
    ; The !if /FileExists guard makes the bootstrapper optional - if the
    ; file isn't there at compile time the installer skips this whole block
    ; and assumes the target machine already has the runtime.
    !if /FileExists "MicrosoftEdgeWebview2Setup.exe"
        ReadRegStr $1 HKLM "SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" "pv"
        StrCmp $1 "" webview2_install webview2_skip
        webview2_install:
            DetailPrint "Installing WebView2 Runtime..."
            File "MicrosoftEdgeWebview2Setup.exe"
            ExecWait '"$INSTDIR\MicrosoftEdgeWebview2Setup.exe" /silent /install'
            Delete "$INSTDIR\MicrosoftEdgeWebview2Setup.exe"
        webview2_skip:
    !endif

    ; Bootstrap Microsoft Visual C++ Redistributable (2015-2022, x64). torch's
    ; c10.dll depends on msvcp140/vcruntime140 and we strip those from the
    ; PyInstaller bundle (see sayzo-agent.spec) so Windows loads matched
    ; versions from the Redist instead of PyInstaller's mismatched copies.
    ;
    ; v2.8.2: skip when already installed. The redist installer self-elevates
    ; regardless of /quiet /norestart, popping UAC on every run — surprising
    ; to users mid auto-update who see an unbranded Microsoft prompt with no
    ; Sayzo context. ``Installed=1`` at this VS 14.0 Runtimes key is
    ; Microsoft's documented "is the redist present" signal (same key
    ; vcredist.exe writes on successful install). HKLM is world-readable so
    ; a user-scope installer can probe it without elevation.
    !if /FileExists "VC_redist.x64.exe"
        ReadRegDWORD $1 HKLM "SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" "Installed"
        IntCmp $1 1 vcredist_skip vcredist_install vcredist_install
        vcredist_install:
            DetailPrint "Installing Visual C++ Redistributable..."
            File "VC_redist.x64.exe"
            ExecWait '"$INSTDIR\VC_redist.x64.exe" /install /quiet /norestart'
            Delete "$INSTDIR\VC_redist.x64.exe"
        vcredist_skip:
    !endif

    ; Add install dir to user PATH so `sayzo-agent` works from any terminal.
    ReadRegStr $0 HKCU "Environment" "Path"
    WriteRegStr HKCU "Environment" "Path" "$0;$INSTDIR"
    SendMessage ${HWND_BROADCAST} ${WM_WININICHANGE} 0 "STR:Environment" /TIMEOUT=5000

    ; v2.8.1: register auto-start via the HKCU Run key instead of Task
    ; Scheduler. Background: in v2.8.0 we used ``schtasks /Create`` and on
    ; some user accounts (group-policy locked, antivirus-hardened, EDR-
    ; managed) it failed silently with "Access is denied" even though the
    ; install was user-scope. The HKCU Run key is the per-user-app standard
    ; (Slack, Discord, VS Code, GitHub Desktop) — no Task Scheduler service
    ; involvement, no policy interaction, no race with the legacy migration
    ; uninstaller's ``schtasks /Delete``.
    ;
    ; The ``--from-autostart`` flag tells the agent to suppress the user-
    ; click Settings auto-open (``looks_user_launched()`` would otherwise
    ; trigger because explorer.exe is the parent of every Run-key-launched
    ; process; without the flag, Settings would pop on every login).
    ;
    ; Defensive cleanup: pre-emptively delete any leftover Task Scheduler
    ; entry from v2.8.0 install attempts OR a legacy ``/RL HIGHEST`` task
    ; the migration block's elevated uninstaller didn't fully purge. Both
    ; exit non-zero when the task isn't present — harmless.
    nsExec::ExecToLog 'schtasks /End /TN "Sayzo"'
    nsExec::ExecToLog 'schtasks /Delete /TN "Sayzo" /F'

    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "Sayzo" '"$INSTDIR\${SERVICE_EXE}" service --from-autostart'

    ; Start Menu shortcut - also uses the windowless exe so clicking it doesn't
    ; pop a terminal. The console exe stays available on PATH for CLI use.
    CreateDirectory "$SMPROGRAMS\${PRODUCT_NAME}"
    CreateShortCut "$SMPROGRAMS\${PRODUCT_NAME}\${PRODUCT_NAME}.lnk" "$INSTDIR\${SERVICE_EXE}" "service"
    CreateShortCut "$SMPROGRAMS\${PRODUCT_NAME}\Uninstall.lnk" "$INSTDIR\uninstall.exe"

    ; Set AppUserModelID (AUMID) on the Start Menu shortcut. Windows 10
    ; silently drops WinRT toasts from apps without a registered AUMID-on-
    ; shortcut, so this is required for desktop-notifier to show anything on
    ; Win10. Must match the `app_name` passed to DesktopNotifier() in
    ; sayzo_agent/__main__.py. Plugin is vendored under nsis-plugins/.
    ApplicationID::Set "$SMPROGRAMS\${PRODUCT_NAME}\${PRODUCT_NAME}.lnk" "Sayzo"
    Pop $0

    ; Write uninstall information to registry under HKCU (per-user). The
    ; "Apps & Features" / "Programs and Features" UI surfaces both HKLM
    ; and HKCU entries; using HKCU means this entry only appears for the
    ; user who installed Sayzo (the only user it serves) and no admin
    ; rights are required to write or delete it.
    WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayName" "${PRODUCT_NAME}"
    WriteRegStr HKCU "${UNINSTALL_KEY}" "UninstallString" '"$INSTDIR\uninstall.exe"'
    WriteRegStr HKCU "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
    WriteRegStr HKCU "${UNINSTALL_KEY}" "Publisher" "${PRODUCT_PUBLISHER}"
    WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayVersion" "${PRODUCT_VERSION}"
    ; DisplayIcon drives the icon rendered in Settings -> Apps & Features and
    ; the legacy Programs and Features control panel. Point at the service
    ; exe's embedded icon resource (index 0) - PyInstaller writes logo.ico
    ; into both exes via the spec's `icon=app_icon`, so no separate .ico
    ; needs to be shipped. Service exe (not the CLI exe) matches every other
    ; user-facing reference (Start Menu shortcut, HKCU Run autostart).
    WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayIcon" "$INSTDIR\${SERVICE_EXE},0"
    WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoModify" 1
    WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoRepair" 1

    ; Write uninstaller.
    WriteUninstaller "$INSTDIR\uninstall.exe"

    ; Release the install-progress lock so any agent processes that
    ; queued up during the install (Fix 5) can proceed past their boot
    ; wait. Order matters: release BEFORE the silent relaunch below so
    ; the relaunched agent doesn't see its own install lock and wait
    ; pointlessly for 60 s.
    Delete "$PROFILE\.sayzo\agent\install_in_progress.lock"

    ; Silent-install fallback (v2.8.2). MUI_FINISHPAGE_RUN only fires in
    ; interactive mode; on /S (the auto-update apply path via
    ; update_apply_win.spawn_installer_and_exit) it's skipped and the
    ; user is left with an empty tray until next login. Explicitly
    ; relaunch the new agent here. --open-settings tells the agent to
    ; surface Settings on boot — matches the user's mental model since
    ; they were just in Settings when they clicked Install.
    IfSilent silent_relaunch fini_install
    silent_relaunch:
        Exec '"$INSTDIR\${SERVICE_EXE}" service --open-settings'
    fini_install:
SectionEnd

; ---------------------------------------------------------------------------
; Uninstall
; ---------------------------------------------------------------------------

Section "Uninstall"
    ; Stop the running agent. v2.8.1+ auto-starts via HKCU Run key (no
    ; Task Scheduler), so the canonical stop is taskkill. The schtasks
    ; lines below are defensive cleanup for installs that came through
    ; v2.8.0 (which attempted to create a task) or a legacy ``/RL HIGHEST``
    ; entry that survived the migration uninstaller.
    nsExec::ExecToLog 'taskkill /IM sayzo-agent-service.exe /F /T'
    nsExec::ExecToLog 'taskkill /IM sayzo-agent.exe /F /T'
    DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "Sayzo"
    nsExec::ExecToLog 'schtasks /End /TN "Sayzo"'
    nsExec::ExecToLog 'schtasks /Delete /TN "Sayzo" /F'

    ; Remove PATH entry.
    ReadRegStr $0 HKCU "Environment" "Path"
    ${WordReplace} $0 ";$INSTDIR" "" "+*" $0
    ${WordReplace} $0 "$INSTDIR" "" "+*" $0
    WriteRegStr HKCU "Environment" "Path" "$0"
    SendMessage ${HWND_BROADCAST} ${WM_WININICHANGE} 0 "STR:Environment" /TIMEOUT=5000

    ; Remove Start Menu shortcuts.
    RMDir /r "$SMPROGRAMS\${PRODUCT_NAME}"

    ; Remove install directory.
    RMDir /r "$INSTDIR"

    ; Remove registry keys. HKCU mirrors the install-time write (Stage 0
    ; per-user install).
    DeleteRegKey HKCU "${UNINSTALL_KEY}"
SectionEnd
