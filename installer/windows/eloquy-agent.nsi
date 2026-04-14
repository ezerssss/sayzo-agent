; Eloquy Agent NSIS Installer
; Installs the PyInstaller bundle, creates auto-start Task Scheduler entry,
; and registers with Add/Remove Programs.

!include "MUI2.nsh"
!include "nsDialogs.nsh"
!include "WinMessages.nsh"
!include "WordFunc.nsh"

; ---------------------------------------------------------------------------
; Configuration
; ---------------------------------------------------------------------------

!define PRODUCT_NAME "Eloquy Agent"
!define PRODUCT_PUBLISHER "Eloquy"
!define PRODUCT_VERSION "0.1.0"
!define PRODUCT_EXE "eloquy-agent.exe"
!define INSTALL_DIR "$PROGRAMFILES64\Eloquy\Agent"
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}"

Name "${PRODUCT_NAME} ${PRODUCT_VERSION}"
OutFile "eloquy-agent-setup-${PRODUCT_VERSION}.exe"
InstallDir "${INSTALL_DIR}"
RequestExecutionLevel admin
SetCompressor /SOLID lzma

; ---------------------------------------------------------------------------
; UI
; ---------------------------------------------------------------------------

!define MUI_ABORTWARNING
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
    ; The NSIS script must be invoked from the repo root where dist/eloquy-agent/ exists.
    File /r "..\..\dist\eloquy-agent\*.*"

    ; Add install dir to user PATH so `eloquy-agent` works from any terminal.
    ReadRegStr $0 HKCU "Environment" "Path"
    WriteRegStr HKCU "Environment" "Path" "$0;$INSTDIR"
    SendMessage ${HWND_BROADCAST} ${WM_WININICHANGE} 0 "STR:Environment" /TIMEOUT=5000

    ; Create Task Scheduler entry: run at login, hidden, restart on failure.
    ; /SC ONLOGON: trigger at user login
    ; /RL HIGHEST: run with highest privileges (needed for WASAPI on some systems)
    ; /F: force overwrite if exists
    nsExec::ExecToLog 'schtasks /Create /TN "Eloquy Agent" /TR "\"$INSTDIR\${PRODUCT_EXE}\" service" /SC ONLOGON /RL HIGHEST /F'

    ; Start Menu shortcut
    CreateDirectory "$SMPROGRAMS\${PRODUCT_NAME}"
    CreateShortCut "$SMPROGRAMS\${PRODUCT_NAME}\${PRODUCT_NAME}.lnk" "$INSTDIR\${PRODUCT_EXE}" "service"
    CreateShortCut "$SMPROGRAMS\${PRODUCT_NAME}\Uninstall.lnk" "$INSTDIR\uninstall.exe"

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
    nsExec::ExecToLog 'schtasks /End /TN "Eloquy Agent"'
    nsExec::ExecToLog 'schtasks /Delete /TN "Eloquy Agent" /F'

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
