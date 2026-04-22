; Sayzo Agent NSIS Installer
; Installs the PyInstaller bundle, creates auto-start Task Scheduler entry,
; and registers with Add/Remove Programs.

!include "MUI2.nsh"
!include "nsDialogs.nsh"
!include "WinMessages.nsh"
!include "WordFunc.nsh"

; Vendored NSIS plugins (see installer/windows/nsis-plugins/README.md).
; We ship ApplicationID.dll so toast notifications work on Win10 — the
; chocolatey `nsis` package on CI doesn't bundle third-party plugins.
!addplugindir /x86-unicode "nsis-plugins\x86-unicode"

; ---------------------------------------------------------------------------
; Configuration
; ---------------------------------------------------------------------------

!define PRODUCT_NAME "Sayzo Agent"
!define PRODUCT_PUBLISHER "Sayzo"
; PRODUCT_VERSION is normally injected by CI via `makensis /DPRODUCT_VERSION=$VERSION ...`
; where $VERSION is read from pyproject.toml (the single source of truth for Phase A
; auto-update). The !ifndef guard keeps local/dev `makensis ...` invocations working
; without requiring the flag — they'll just produce an installer labelled 0.0.0-dev.
!ifndef PRODUCT_VERSION
    !define PRODUCT_VERSION "0.0.0-dev"
!endif
!define PRODUCT_EXE "sayzo-agent.exe"
!define SERVICE_EXE "sayzo-agent-service.exe"
!define INSTALL_DIR "$PROGRAMFILES64\Sayzo\Agent"
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}"

Name "${PRODUCT_NAME} ${PRODUCT_VERSION}"
OutFile "sayzo-agent-setup.exe"
InstallDir "${INSTALL_DIR}"
RequestExecutionLevel admin
SetCompressor /SOLID lzma
Icon "..\..\installer\assets\logo.ico"
UninstallIcon "..\..\installer\assets\logo.ico"

; ---------------------------------------------------------------------------
; UI
; ---------------------------------------------------------------------------

!define MUI_ABORTWARNING
!define MUI_ICON "..\..\installer\assets\logo.ico"
!define MUI_UNICON "..\..\installer\assets\logo.ico"
!insertmacro MUI_PAGE_INSTFILES

; Finish page with a "Launch Sayzo Agent" checkbox that runs the windowless
; service exe (console=False per sayzo-agent.spec). The service detects
; missing setup signals and opens its own first-run GUI if needed — that's
; the whole point of the GUI installer path. See ~/.claude/plans/i-created-a-memory-quizzical-cosmos.md.
; The MUI_FINISHPAGE_RUN_* defines must come BEFORE MUI_PAGE_FINISH.
!define MUI_FINISHPAGE_RUN "$INSTDIR\${SERVICE_EXE}"
; --force-setup makes the service open the GUI regardless of detect_setup's
; verdict — users get visual confirmation right after install even if they
; had prior-install state from a previous CLI run.
!define MUI_FINISHPAGE_RUN_PARAMETERS "service --force-setup"
!define MUI_FINISHPAGE_RUN_TEXT "Launch Sayzo Agent"
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

; ---------------------------------------------------------------------------
; Install
; ---------------------------------------------------------------------------

Section "Install"
    SetOutPath "$INSTDIR"

    ; Stop any running agent before overwriting its exes. Without this, a user
    ; re-running the installer (the Phase A "Download update" path) hits
    ; ERROR_SHARING_VIOLATION on sayzo-agent.exe / sayzo-agent-service.exe mid
    ; File /r and the install aborts with files half-replaced. Idempotent —
    ; both commands exit non-zero on "task/process not found" and we ignore
    ; the return. The Task Scheduler task itself is re-created below with /F.
    nsExec::ExecToLog 'schtasks /End /TN "Sayzo Agent"'
    nsExec::ExecToLog 'taskkill /IM sayzo-agent-service.exe /F'
    nsExec::ExecToLog 'taskkill /IM sayzo-agent.exe /F'

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
    ; The !if /FileExists guard makes the bootstrapper optional — if the
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

    ; Bootstrap Microsoft Visual C++ Redistributable (2015–2022, x64). torch's
    ; c10.dll depends on msvcp140/vcruntime140 and we strip those from the
    ; PyInstaller bundle (see sayzo-agent.spec) so Windows loads matched
    ; versions from the Redist instead of PyInstaller's mismatched copies.
    ; /install /quiet /norestart is idempotent — skips fast if up to date.
    !if /FileExists "VC_redist.x64.exe"
        DetailPrint "Installing Visual C++ Redistributable..."
        File "VC_redist.x64.exe"
        ExecWait '"$INSTDIR\VC_redist.x64.exe" /install /quiet /norestart'
        Delete "$INSTDIR\VC_redist.x64.exe"
    !endif

    ; Add install dir to user PATH so `sayzo-agent` works from any terminal.
    ReadRegStr $0 HKCU "Environment" "Path"
    WriteRegStr HKCU "Environment" "Path" "$0;$INSTDIR"
    SendMessage ${HWND_BROADCAST} ${WM_WININICHANGE} 0 "STR:Environment" /TIMEOUT=5000

    ; Create Task Scheduler entry: run at login, hidden, restart on failure.
    ; Points at the windowless service exe so no console window pops up.
    ; /SC ONLOGON: trigger at user login
    ; /RL HIGHEST: run with highest privileges (needed for WASAPI on some systems)
    ; /F: force overwrite if exists
    nsExec::ExecToLog 'schtasks /Create /TN "Sayzo Agent" /TR "\"$INSTDIR\${SERVICE_EXE}\" service" /SC ONLOGON /RL HIGHEST /F'

    ; Start Menu shortcut — also uses the windowless exe so clicking it doesn't
    ; pop a terminal. The console exe stays available on PATH for CLI use.
    CreateDirectory "$SMPROGRAMS\${PRODUCT_NAME}"
    CreateShortCut "$SMPROGRAMS\${PRODUCT_NAME}\${PRODUCT_NAME}.lnk" "$INSTDIR\${SERVICE_EXE}" "service"
    CreateShortCut "$SMPROGRAMS\${PRODUCT_NAME}\Uninstall.lnk" "$INSTDIR\uninstall.exe"

    ; Set AppUserModelID (AUMID) on the Start Menu shortcut. Windows 10
    ; silently drops WinRT toasts from apps without a registered AUMID-on-
    ; shortcut, so this is required for desktop-notifier to show anything on
    ; Win10. Must match the `app_name` passed to DesktopNotifier() in
    ; sayzo_agent/__main__.py. Plugin is vendored under nsis-plugins/.
    ApplicationID::Set "$SMPROGRAMS\${PRODUCT_NAME}\${PRODUCT_NAME}.lnk" "Sayzo.Agent"
    Pop $0

    ; Write uninstall information to registry.
    WriteRegStr HKLM "${UNINSTALL_KEY}" "DisplayName" "${PRODUCT_NAME}"
    WriteRegStr HKLM "${UNINSTALL_KEY}" "UninstallString" '"$INSTDIR\uninstall.exe"'
    WriteRegStr HKLM "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
    WriteRegStr HKLM "${UNINSTALL_KEY}" "Publisher" "${PRODUCT_PUBLISHER}"
    WriteRegStr HKLM "${UNINSTALL_KEY}" "DisplayVersion" "${PRODUCT_VERSION}"
    WriteRegDWORD HKLM "${UNINSTALL_KEY}" "NoModify" 1
    WriteRegDWORD HKLM "${UNINSTALL_KEY}" "NoRepair" 1

    ; Write uninstaller.
    WriteUninstaller "$INSTDIR\uninstall.exe"
SectionEnd

; ---------------------------------------------------------------------------
; Uninstall
; ---------------------------------------------------------------------------

Section "Uninstall"
    ; Stop the running agent.
    nsExec::ExecToLog 'schtasks /End /TN "Sayzo Agent"'
    nsExec::ExecToLog 'schtasks /Delete /TN "Sayzo Agent" /F'

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

    ; Remove registry keys.
    DeleteRegKey HKLM "${UNINSTALL_KEY}"
SectionEnd
