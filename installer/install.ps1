# Sayzo Agent — Windows one-liner installer (dev / power-user path)
# Usage: irm https://sayzo.app/releases/windows/install.ps1 | iex
#
# Downloads the NSIS installer and runs it silently. The NSIS finish page
# auto-launches sayzo-agent-service.exe, which detects missing setup signals
# and opens its own GUI setup window — so this script no longer needs to
# invoke `first-run` itself.

$ErrorActionPreference = "Stop"

$version = "0.1.0"
$arch = if ([System.Environment]::Is64BitOperatingSystem) { "x64" } else { "x86" }
$installerName = "sayzo-agent-setup-${version}.exe"
$downloadUrl = "https://sayzo.app/releases/windows/${installerName}"
$tempDir = Join-Path $env:TEMP "sayzo-install"
$installerPath = Join-Path $tempDir $installerName

Write-Host ""
Write-Host "  Sayzo Agent Installer" -ForegroundColor Cyan
Write-Host "  =====================" -ForegroundColor Cyan
Write-Host ""

# Create temp directory.
if (-not (Test-Path $tempDir)) {
    New-Item -ItemType Directory -Path $tempDir -Force | Out-Null
}

# Download installer.
Write-Host "  Downloading Sayzo Agent v${version}..." -ForegroundColor White
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

Write-Host ""
Write-Host "  Done! Complete setup in the window that appears." -ForegroundColor Green
Write-Host "  Sayzo Agent will then start automatically on every login." -ForegroundColor Green
Write-Host ""
