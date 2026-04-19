; Sayzo Agent NSIS Installer
; Installs the PyInstaller bundle, creates auto-start Task Scheduler entry,
; and registers with Add/Remove Programs.

!include "MUI2.nsh"
!include "nsDialogs.nsh"
!include "WinMessages.nsh"
!include "WordFunc.nsh"

; ---------------------------------------------------------------------------
; Configuration
; ---------------------------------------------------------------------------

!define PRODUCT_NAME "Sayzo Agent"
!define PRODUCT_PUBLISHER "Sayzo"
!define PRODUCT_VERSION "0.1.0"
!define PRODUCT_EXE "sayzo-agent.exe"
!define SERVICE_EXE "sayzo-agent-service.exe"
!define INSTALL_DIR "$PROGRAMFILES64\Sayzo\Agent"
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}"

Name "${PRODUCT_NAME} ${PRODUCT_VERSION}"
OutFile "sayzo-agent-setup-${PRODUCT_VERSION}.exe"
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
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

; ---------------------------------------------------------------------------
; Install
; ---------------------------------------------------------------------------

Section "Install"
    SetOutPath "$INSTDIR"

    ; Copy the entire PyInstaller bundle directory.
    ; The NSIS script must be invoked from the repo root where dist/sayzo-agent/ exists.
    File /r "..\..\dist\sayzo-agent\*.*"

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

    ; Set AppUserModelID (AUMID) on the Start Menu shortcut. Windows 10 silently
    ; drops WinRT toasts from apps without a registered AUMID-on-shortcut, so
    ; this is required for desktop-notifier to show anything on Win10. Must
    ; match the `app_name` passed to DesktopNotifier() in sayzo_agent/__main__.py.
    ; Requires the ApplicationID NSIS plugin (ships with many NSIS builds; if
    ; missing, install it or use a registry-based fallback).
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
