# deskpt-dev.ps1 — Quick launch dev environment
# Usage: .\deskpt-dev.ps1

$ErrorActionPreference = "Continue"
$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path
$GATEWAY_PY = Join-Path $ROOT "minicpm-sidecar\.venv\Scripts\python.exe"
$ELECTRON = Join-Path $ROOT "clawd-on-desk"
$LLAMA = Join-Path $ROOT "minicpm-sidecar\bin\win-x64\llama-server.exe"
$PORT = 18765
$GATEWAY_LOG = Join-Path $ROOT "gateway.log"
$GATEWAY_ERR_LOG = Join-Path $ROOT "gateway.err.log"

Write-Host "======================================" -ForegroundColor Cyan
Write-Host "  MiniCPM Desk Pet Dev Launcher" -ForegroundColor Cyan
Write-Host "======================================" -ForegroundColor Cyan

# ── 1. Pre-flight checks ──
Write-Host "[1/5] Pre-flight checks..." -ForegroundColor Gray
$ok = $true
if (-not (Test-Path $GATEWAY_PY)) {
    Write-Host "[FAIL] Python venv not found at: $GATEWAY_PY" -ForegroundColor Red
    $ok = $false
}
if (-not (Test-Path $LLAMA)) {
    Write-Host "[WARN] llama-server not found at: $LLAMA (gateway may use API providers)" -ForegroundColor Yellow
}
if (-not (Test-Path (Join-Path $ELECTRON "node_modules"))) {
    Write-Host "[FAIL] node_modules missing in $ELECTRON — run 'npm install' first" -ForegroundColor Red
    $ok = $false
}
$nodeCmd = Get-Command "node" -ErrorAction SilentlyContinue
$nodePath = if ($nodeCmd -and $nodeCmd.CommandType -eq "Application") { $nodeCmd.Source } else { $null }
if (-not $nodePath) {
    Write-Host "[FAIL] node not found in PATH — install Node.js first" -ForegroundColor Red
    $ok = $false
}
if (-not $ok) { exit 1 }

# ── 2. Kill stale project processes ──
Write-Host "[2/5] Stopping previous instances..." -ForegroundColor Gray
Get-Process -Name "electron" -ErrorAction SilentlyContinue | Where-Object {
    $_.Path -and $_.Path -match [regex]::Escape($ROOT)
} | Stop-Process -Force
Get-Process -Name "python*" -ErrorAction SilentlyContinue | Where-Object {
    $_.Path -and $_.Path -match [regex]::Escape("minicpm-sidecar")
} | Stop-Process -Force
Start-Sleep -Seconds 1

# ── 3. Start gateway ──
Write-Host "[3/5] Starting gateway (port $PORT)..." -ForegroundColor Yellow
$env:MINICPM_LLAMA_SERVER = $LLAMA
try {
    $gateProc = Start-Process -FilePath $GATEWAY_PY -ArgumentList "-m", "gateway", "--port", "$PORT", "--host", "127.0.0.1" -WindowStyle Hidden -RedirectStandardOutput $GATEWAY_LOG -RedirectStandardError $GATEWAY_ERR_LOG -PassThru
    Write-Host "  Gateway PID: $($gateProc.Id)" -ForegroundColor Gray
} catch {
    Write-Host "[FAIL] Failed to start gateway: $_" -ForegroundColor Red
    Write-Host "       Check $GATEWAY_LOG / $GATEWAY_ERR_LOG for details." -ForegroundColor Yellow
    exit 1
}

# ── 4. Wait for health check ──
Write-Host "[4/5] Health check..." -ForegroundColor Gray
$healthy = $false
$startTime = Get-Date
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 2
    try {
        $r = Invoke-RestMethod "http://127.0.0.1:$PORT/api/health" -TimeoutSec 2
        $elapsed = [math]::Round(((Get-Date) - $startTime).TotalSeconds)
        Write-Host "  Gateway OK after ${elapsed}s (alive=$($r.alive))" -ForegroundColor Green
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
    $elapsed = [math]::Round(((Get-Date) - $startTime).TotalSeconds)
    Write-Host "[WARN] Gateway not healthy after ${elapsed}s — check $GATEWAY_LOG / $GATEWAY_ERR_LOG" -ForegroundColor Yellow
    Write-Host "       The pet may still work if you use external API providers." -ForegroundColor Yellow
}

# ── 5. Start Electron ──
Write-Host "[5/5] Starting Electron..." -ForegroundColor Yellow
try {
    $proc = Start-Process -FilePath $nodePath -ArgumentList "launch.js" -WorkingDirectory $ELECTRON -PassThru
    Write-Host "  Electron PID: $($proc.Id)" -ForegroundColor Gray
    Write-Host "Done. Pet should appear on desktop." -ForegroundColor Green
} catch {
    Write-Host "[FAIL] Failed to start Electron: $_" -ForegroundColor Red
    exit 1
}
