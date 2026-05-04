# Preview the FinishSignup ("finish setting up at sayzo.app") screen in a
# plain browser using a mocked pywebview bridge. No Python agent required.
#
# Usage:
#   .\scripts\preview_finish_signup.ps1                 # default: onboarding_required
#   .\scripts\preview_finish_signup.ps1 -State suspended
#   .\scripts\preview_finish_signup.ps1 -State deleted
#
# Press Ctrl+C in the spawned npm window to stop the dev server.

param(
    [ValidateSet("onboarding_required", "suspended", "deleted")]
    [string]$State = "onboarding_required"
)

$ErrorActionPreference = "Stop"

$webuiDir = Resolve-Path (Join-Path $PSScriptRoot "..\sayzo_agent\gui\webui")
Write-Host "[preview] webui dir: $webuiDir"

if (-not (Test-Path (Join-Path $webuiDir "node_modules"))) {
    Write-Host "[preview] node_modules missing — running 'npm install' first..."
    Push-Location $webuiDir
    try { npm install } finally { Pop-Location }
}

$url = "http://localhost:5173/#preview=finish-signup&state=$State"
Write-Host "[preview] starting vite dev server (mock mode)..."
Write-Host "[preview] once it's up, browser will open at:"
Write-Host "          $url"
Write-Host "[preview] Ctrl+C to stop."
Write-Host ""

# `npm run dev:finish-signup` already passes `--open /#preview=finish-signup`
# to vite, but vite's --open dropped the hash on some Windows shells. Open
# the explicit URL ourselves after a short delay so it works regardless.
Start-Job -ScriptBlock {
    param($url)
    Start-Sleep -Seconds 3
    Start-Process $url
} -ArgumentList $url | Out-Null

Push-Location $webuiDir
try {
    npm run dev:mock
} finally {
    Pop-Location
}
