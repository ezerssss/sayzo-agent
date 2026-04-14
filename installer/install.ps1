# Eloquy Agent — Windows one-liner installer
# Usage: irm https://eloquy.threadlify.io/releases/windows/install.ps1 | iex
#
# Downloads the NSIS installer, runs it silently, then launches first-run setup.

$ErrorActionPreference = "Stop"

$version = "0.1.0"
$arch = if ([System.Environment]::Is64BitOperatingSystem) { "x64" } else { "x86" }
$installerName = "eloquy-agent-setup-${version}.exe"
$downloadUrl = "https://eloquy.threadlify.io/releases/windows/${installerName}"
$tempDir = Join-Path $env:TEMP "eloquy-install"
$installerPath = Join-Path $tempDir $installerName

Write-Host ""
Write-Host "  Eloquy Agent Installer" -ForegroundColor Cyan
Write-Host "  ======================" -ForegroundColor Cyan
Write-Host ""

# Create temp directory.
if (-not (Test-Path $tempDir)) {
    New-Item -ItemType Directory -Path $tempDir -Force | Out-Null
}

# Download installer.
Write-Host "  Downloading Eloquy Agent v${version}..." -ForegroundColor White
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

# Launch first-run setup.
$exePath = Join-Path $env:ProgramFiles "Eloquy\Agent\eloquy-agent.exe"
if (Test-Path $exePath) {
    Write-Host ""
    Write-Host "  Launching first-time setup..." -ForegroundColor Cyan
    & $exePath first-run
} else {
    Write-Host "  Warning: Could not find $exePath" -ForegroundColor Yellow
    Write-Host "  Run 'eloquy-agent first-run' manually to complete setup." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "  Done! Eloquy Agent will start automatically on login." -ForegroundColor Green
Write-Host ""
