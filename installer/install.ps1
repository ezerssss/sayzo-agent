# Sayzo — Windows one-liner installer (dev / power-user path)
# Usage: irm https://sayzo.app/releases/windows/install.ps1 | iex
#
# Downloads the NSIS installer and runs it silently. The NSIS finish page
# auto-launches sayzo-agent-service.exe, which detects missing setup signals
# and opens its own GUI setup window — so this script no longer needs to
# invoke `first-run` itself.

$ErrorActionPreference = "Stop"

$arch = if ([System.Environment]::Is64BitOperatingSystem) { "x64" } else { "x86" }
$installerName = "sayzo-setup.exe"
$downloadUrl = "https://sayzo.app/releases/windows/${installerName}"
$tempDir = Join-Path $env:TEMP "sayzo-install"
$installerPath = Join-Path $tempDir $installerName

Write-Host ""
Write-Host "  Sayzo Installer" -ForegroundColor Cyan
Write-Host "  ===============" -ForegroundColor Cyan
Write-Host ""

# Create temp directory.
if (-not (Test-Path $tempDir)) {
    New-Item -ItemType Directory -Path $tempDir -Force | Out-Null
}

# Download installer.
Write-Host "  Downloading Sayzo..." -ForegroundColor White
try {
    Invoke-WebRequest -Uri $downloadUrl -OutFile $installerPath -UseBasicParsing
} catch {
    Write-Host "  Download failed: $_" -ForegroundColor Red
    Write-Host "  URL: $downloadUrl" -ForegroundColor Red
    exit 1
}
Write-Host "  Downloaded to $installerPath" -ForegroundColor Green

# Run installer silently.
Write-Host "  Installing..." -ForegroundColor White
$process = Start-Process -FilePath $installerPath -ArgumentList "/S" -Wait -PassThru
if ($process.ExitCode -ne 0) {
    Write-Host "  Installation failed (exit code $($process.ExitCode))" -ForegroundColor Red
    exit 1
}
Write-Host "  Installed successfully." -ForegroundColor Green

# Clean up installer.
Remove-Item -Path $tempDir -Recurse -Force -ErrorAction SilentlyContinue

# NSIS silent install (/S) skips the finish page, so MUI_FINISHPAGE_RUN
# doesn't fire — we have to launch the service ourselves. --force-setup
# matches the GUI-install path.
#
# v2.8.0+: install location moved to %LOCALAPPDATA%\Programs\Sayzo (per-user,
# no admin required). We keep the legacy $env:ProgramFiles probe as a fallback
# so this script also works against a transitional state where the old admin
# install is still present (the v2.8.0 installer's migration block removes it,
# but if a user runs this script before that ships, we want to do something
# sensible).
$localServicePath = Join-Path $env:LOCALAPPDATA "Programs\Sayzo\sayzo-agent-service.exe"
$legacyServicePath = Join-Path $env:ProgramFiles "Sayzo\sayzo-agent-service.exe"
if (Test-Path $localServicePath) {
    Write-Host "  Opening setup window..." -ForegroundColor Cyan
    Start-Process -FilePath $localServicePath -ArgumentList "service", "--force-setup"
} elseif (Test-Path $legacyServicePath) {
    Write-Host "  Opening setup window..." -ForegroundColor Cyan
    Start-Process -FilePath $legacyServicePath -ArgumentList "service", "--force-setup"
} else {
    Write-Host "  Warning: could not find Sayzo at $localServicePath or $legacyServicePath" -ForegroundColor Yellow
    Write-Host "  Launch Sayzo from the Start Menu to complete setup." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "  Done! Complete setup in the window that appears." -ForegroundColor Green
Write-Host "  Sayzo will then start automatically on every login." -ForegroundColor Green
Write-Host ""
