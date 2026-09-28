# deskpt-dev.ps1 - Quick launch dev environment (self-bootstrapping)
# Usage: .\deskpt-dev.ps1   (or double-click deskpt-dev.bat)
#
# First run: automatically creates the Python venv, installs gateway deps,
# and runs `npm install` for the Electron shell. Later runs skip these.
# Local llama-server / model GGUF are OPTIONAL - without them the pet runs
# in API mode: configure a provider in Settings -> Model Providers.

$ErrorActionPreference = "Continue"
$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path
$SIDECAR = Join-Path $ROOT "pet-sidecar"
$GATEWAY_PY = Join-Path $SIDECAR ".venv\Scripts\python.exe"
$ELECTRON = Join-Path $ROOT "clawd-on-desk"
$LLAMA = Join-Path $SIDECAR "bin\win-x64\llama-server.exe"
$PORT = 18765

Write-Host "======================================" -ForegroundColor Cyan
Write-Host "  Desk Pet Dev Launcher" -ForegroundColor Cyan
Write-Host "======================================" -ForegroundColor Cyan

# -- 1. Node is the only hard requirement --
Write-Host "[1/5] Checking environment..." -ForegroundColor Gray
$nodeCmd = Get-Command "node" -ErrorAction SilentlyContinue
$nodePath = if ($nodeCmd -and $nodeCmd.CommandType -eq "Application") { $nodeCmd.Source } else { $null }
if (-not $nodePath) {
    Write-Host "[FAIL] node not found in PATH - install Node.js first: https://nodejs.org" -ForegroundColor Red
    exit 1
}

# Find a Python 3.10-3.12 for the gateway venv (3.13+ is rejected by pyproject)
$pyCmd = $null
foreach ($cand in @("python", "python3")) {
    $cmd = Get-Command $cand -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.CommandType -eq "Application") {
        $vraw = (& $cmd.Source --version 2>$null) -replace "[^0-9.]"
        $parts = ($vraw -split "\.")
        if ($parts.Count -ge 2 -and $parts[0] -eq "3") {
            $minor = 0; [void][int32]::TryParse($parts[1], [ref]$minor)
            if ($minor -ge 10 -and $minor -le 12) { $pyCmd = $cmd.Source; break }
        }
    }
}
if (-not $pyCmd -and (Test-Path $GATEWAY_PY)) { $pyCmd = $GATEWAY_PY }  # venv already exists, good enough
if (-not $pyCmd) {
    Write-Host "[FAIL] Python 3.10-3.12 not found in PATH - install it from https://www.python.org/downloads/" -ForegroundColor Red
    Write-Host "       (3.13 is NOT supported by the gateway yet)" -ForegroundColor Red
    exit 1
}

# -- 2. Bootstrap: venv + npm install (first run only) --
Write-Host "[2/5] Bootstrap (skipped when already done)..." -ForegroundColor Gray
if (-not (Test-Path $GATEWAY_PY)) {
    Write-Host "  [BOOT] Creating Python venv with $pyCmd ..." -ForegroundColor Yellow
    & $pyCmd -m venv (Join-Path $SIDECAR ".venv")
    if ($LASTEXITCODE -ne 0) { Write-Host "[FAIL] venv creation failed" -ForegroundColor Red; exit 1 }
}
if (-not (Test-Path (Join-Path $SIDECAR "gateway"))) {
    Write-Host "[FAIL] gateway source missing at $SIDECAR\gateway" -ForegroundColor Red
    exit 1
}
# Verify gateway deps importable; install if missing (venv exists but deps not installed)
$depCheck = & $GATEWAY_PY -c "import fastapi, uvicorn, httpx" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  [BOOT] Installing gateway dependencies (pip, ~1 min)..." -ForegroundColor Yellow
    & $GATEWAY_PY -m pip install --quiet --disable-pip-version-check -e $SIDECAR
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[FAIL] pip install failed - check network/proxy" -ForegroundColor Red
        exit 1
    }
    Write-Host "  [BOOT] Gateway dependencies OK" -ForegroundColor Green
}
if (-not (Test-Path (Join-Path $ELECTRON "node_modules"))) {
    Write-Host "  [BOOT] npm install for Electron shell (first run, several minutes, please wait)..." -ForegroundColor Yellow
    Push-Location $ELECTRON
    & npm install --no-audit --no-fund --loglevel=error
    $npmCode = $LASTEXITCODE
    Pop-Location
    if ($npmCode -ne 0) { Write-Host "[FAIL] npm install failed" -ForegroundColor Red; exit 1 }
    Write-Host "  [BOOT] Electron dependencies OK" -ForegroundColor Green
}

# -- 3. Local inference is OPTIONAL (API mode works without it) --
Write-Host "[3/5] Local inference check..." -ForegroundColor Gray
if (-not (Test-Path $LLAMA)) {
    Write-Host "[INFO] llama-server not found at $LLAMA" -ForegroundColor Yellow
    Write-Host "       Running in API mode: chat goes through providers configured in" -ForegroundColor Yellow
    Write-Host "       Settings -> Model Providers (any OpenAI-compatible API)." -ForegroundColor Yellow
    Write-Host "       To enable local inference later, run:" -ForegroundColor Yellow
    Write-Host "       powershell -File pet-sidecar\scripts\fetch-llama-release.ps1" -ForegroundColor Yellow
}

# -- 4. Kill stale project processes --
Write-Host "[4/5] Stopping previous instances..." -ForegroundColor Gray
Get-Process -Name "electron" -ErrorAction SilentlyContinue | Where-Object {
    $_.Path -and $_.Path -match [regex]::Escape($ROOT)
} | Stop-Process -Force
Get-Process -Name "python*" -ErrorAction SilentlyContinue | Where-Object {
    $_.Path -and $_.Path -match [regex]::Escape("pet-sidecar")
} | Stop-Process -Force
Start-Sleep -Seconds 1

# -- 5. Start Electron (it owns the gateway lifecycle) --
# NOTE: we deliberately do NOT start the gateway ourselves. The gateway has
# a ParentWatchdog that kills it ~2s after its parent process dies, and a
# Start-Process child's parent is THIS script's PowerShell - which exits as
# soon as the script ends. The Electron shell spawns and supervises the
# sidecar itself (its own pid is the watched parent), so let it do that.
Write-Host "[5/5] Starting Electron (it spawns the sidecar itself)..." -ForegroundColor Yellow
$env:PET_LLAMA_SERVER = $LLAMA
try {
    $proc = Start-Process -FilePath $nodePath -ArgumentList "launch.js" -WorkingDirectory $ELECTRON -PassThru
    Write-Host "  Electron PID: $($proc.Id)" -ForegroundColor Gray
} catch {
    Write-Host "[FAIL] Failed to start Electron: $_" -ForegroundColor Red
    exit 1
}

# -- Wait for the sidecar Electron just spawned to answer /api/health --
Write-Host "      Waiting for sidecar health..." -ForegroundColor Gray
$healthy = $false
$startTime = Get-Date
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 2
    try {
        $r = Invoke-RestMethod "http://127.0.0.1:$PORT/api/health" -TimeoutSec 2
        $elapsed = [math]::Round(((Get-Date) - $startTime).TotalSeconds)
        Write-Host "  Sidecar OK after ${elapsed}s (alive=$($r.alive))" -ForegroundColor Green
        $healthy = $true
        break
    } catch {
        if (($i + 1) % 5 -eq 0) {
            $elapsed = [math]::Round(((Get-Date) - $startTime).TotalSeconds)
            Write-Host "  still waiting... (${elapsed}s)" -ForegroundColor DarkGray
        }
    }
}
if (-not $healthy) {
    Write-Host "[WARN] Sidecar did not answer within 60s." -ForegroundColor Yellow
    Write-Host "       Check the pet's own log: %LOCALAPPDATA%\deskpt\logs\" -ForegroundColor Yellow
}

Write-Host "Done. Pet should appear on desktop." -ForegroundColor Green
if (-not (Test-Path "$env:USERPROFILE\.deskpt\providers.json")) {
    Write-Host "" -ForegroundColor White
    Write-Host "FIRST RUN? No API provider configured yet." -ForegroundColor Cyan
    Write-Host "  Right-click the pet -> Settings -> Model Providers -> add your API" -ForegroundColor Cyan
    Write-Host "  (DeepSeek / Qwen / OpenRouter / any OpenAI-compatible endpoint)." -ForegroundColor Cyan
    Write-Host "  Chat falls back to the API provider automatically - no local model needed." -ForegroundColor Cyan
}
