# Start/stop the optional Laya sidecar (Windows).
#
# Default action: start laya-serve on 127.0.0.1:8765, wait for /health, then
# send one warmup POST to /v1/systemone.  Exit 0 only when both succeeded.
#
# This script never downloads anything.  Run bootstrap.ps1 first, or point it
# at the spike venv/model for a smoke test:
#   .\start.ps1 -Python ..\..\.tmp-laya\venv\Scripts\python.exe `
#               -ModelPath ..\..\.tmp-laya\models\laya-multilingual
#
# ASCII-only on purpose: Windows PowerShell 5.1 reads BOM-less .ps1 files as
# ANSI.

[CmdletBinding()]
param(
    [int]$Port = 8765,
    [string]$Python = $env:LAYA_SIDECAR_PYTHON,
    [string]$ModelPath = $env:LAYA_MODEL_PATH,
    [switch]$Stop,
    [switch]$Status,
    [switch]$Foreground,
    [int]$TimeoutSec = 240
)

$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
if (-not $Root) { $Root = Split-Path -Parent $MyInvocation.MyCommand.Path }
$RepoRoot = Split-Path -Parent (Split-Path -Parent $Root)
$RunDir = Join-Path $Root 'run'
$PidFile = Join-Path $RunDir 'laya-serve.pid'
$PortFile = Join-Path $RunDir 'laya-serve.port'
$OutLog = Join-Path $RunDir 'laya-serve.out.log'
$ErrLog = Join-Path $RunDir 'laya-serve.err.log'
$ModelFiles = @(
    'config.json',
    'rl_agent_config.json',
    'model.safetensors',
    'encoder\config.json',
    'tokenizer\tokenizer.json',
    'tokenizer\tokenizer_config.json'
)
$script:StartedPid = 0

function Write-Step([string]$Message) {
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Ok([string]$Message) {
    Write-Host "[ok] $Message" -ForegroundColor Green
}

function Write-Warn([string]$Message) {
    Write-Host "[warn] $Message" -ForegroundColor Yellow
}

function Test-PortListening {
    param([int]$PortNumber)
    # Get-NetTCPConnection throws when nothing matches, so -ErrorAction Stop plus
    # a catch would treat "no listener" as "use netstat", and netstat also shows
    # TIME_WAIT sockets.  Check for the cmdlet once, then only look at Listen.
    if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
        $conn = Get-NetTCPConnection -State Listen -LocalPort $PortNumber -ErrorAction SilentlyContinue
        return [bool]$conn
    }
    $lines = & netstat -ano 2>$null | Select-String -Pattern ":$PortNumber\s+.*LISTENING"
    return [bool]$lines
}

function Get-PortOwner {
    param([int]$PortNumber)
    try {
        $conn = Get-NetTCPConnection -State Listen -LocalPort $PortNumber -ErrorAction Stop |
            Select-Object -First 1
        if ($conn) { return [int]$conn.OwningProcess }
    }
    catch {
        return 0
    }
    return 0
}

function Read-ServerPid {
    if (-not (Test-Path -LiteralPath $PidFile -PathType Leaf)) { return 0 }
    $raw = Get-Content -LiteralPath $PidFile -Raw
    if (-not $raw) { return 0 }
    $raw = $raw.Trim()
    $number = 0
    if ([int]::TryParse($raw, [ref]$number)) { return $number }
    return 0
}

function Read-ServerPort {
    param([int]$DefaultPort)
    if (-not (Test-Path -LiteralPath $PortFile -PathType Leaf)) { return $DefaultPort }
    $raw = Get-Content -LiteralPath $PortFile -Raw
    if (-not $raw) { return $DefaultPort }
    $number = 0
    if ([int]::TryParse($raw.Trim(), [ref]$number) -and $number -gt 0 -and $number -lt 65536) {
        return $number
    }
    return $DefaultPort
}

function Test-IsLayaProcess {
    param([int]$ProcessId)
    try {
        $info = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction Stop
    }
    catch {
        # CIM can be unavailable in restricted environments; the PID file is ours.
        return $true
    }
    if (-not $info) { return $false }
    if (-not $info.CommandLine) { return $true }
    return ($info.CommandLine -match 'laya[.-]serve|laya\.serve')
}

function Wait-Health {
    param(
        [int]$PortNumber,
        [int]$TimeoutSeconds,
        [int]$ProcessId = 0
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $processGoneAt = $null
    while ((Get-Date) -lt $deadline) {
        if ($ProcessId -gt 0) {
            $proc = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
            if (-not $proc) {
                # A uv launcher can exit while its child keeps serving; give the
                # child a short grace period to answer /health before failing.
                if (-not $processGoneAt) { $processGoneAt = Get-Date }
                elseif ((Get-Date) -gt $processGoneAt.AddSeconds(5)) { return $null }
            }
        }
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$PortNumber/health" -TimeoutSec 2 -ErrorAction Stop
            if ($health -and $health.status -eq 'ok') { return $health }
        }
        catch {
            # Not listening yet, or still preloading the checkpoint.
        }
        Start-Sleep -Milliseconds 500
    }
    return $null
}

function Invoke-Warmup {
    param([int]$PortNumber)
    $body = @'
{"model":"zh-tier","state":"ping","questions":{"thinking":{"type":"choice","instructions":"How much thinking budget does this message need?","criteria":{"fast":"greeting or trivial","normal":"simple chat or a short factual answer","deep":"code, error analysis, or multi-step work","max":"architecture or an explicitly long design task"}}}}
'@
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($body)
    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    $response = Invoke-RestMethod -Method Post `
        -Uri "http://127.0.0.1:$PortNumber/v1/systemone" `
        -ContentType 'application/json; charset=utf-8' `
        -Body $bytes `
        -TimeoutSec 120 `
        -ErrorAction Stop
    $watch.Stop()
    $choice = $response.answers.thinking.choice
    if (-not $choice) {
        throw 'warmup response has no answers.thinking.choice'
    }
    return [pscustomobject]@{
        Choice   = $choice
        Ms       = $watch.Elapsed.TotalMilliseconds
        Response = $response
    }
}

function Test-ModelDir {
    param([string]$Path)
    if (-not $Path) { return $false }
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) { return $false }
    foreach ($rel in $ModelFiles) {
        if (-not (Test-Path -LiteralPath (Join-Path $Path $rel) -PathType Leaf)) { return $false }
    }
    if ((Get-Item -LiteralPath (Join-Path $Path 'model.safetensors')).Length -lt 600000000) { return $false }
    if ((Get-Item -LiteralPath (Join-Path $Path 'tokenizer\tokenizer.json')).Length -lt 30000000) { return $false }
    try {
        $cfg = Get-Content -Raw -LiteralPath (Join-Path $Path 'rl_agent_config.json') | ConvertFrom-Json
        if (-not $cfg.encoder) { return $false }
    }
    catch {
        return $false
    }
    return $true
}

function Resolve-ModelPath {
    param([string]$Explicit)
    $candidates = @()
    if ($Explicit) {
        $candidates += [pscustomobject]@{ Path = $Explicit; Explicit = $true }
    }
    $candidates += [pscustomobject]@{ Path = (Join-Path $Root 'models\laya-multilingual'); Explicit = $false }
    $candidates += [pscustomobject]@{ Path = (Join-Path $RepoRoot '.tmp-laya\models\laya-multilingual'); Explicit = $false }

    foreach ($candidate in $candidates) {
        if (Test-ModelDir $candidate.Path) {
            return (Resolve-Path -LiteralPath $candidate.Path).Path
        }
        if ($candidate.Explicit) {
            throw "Model path is not a complete laya-multilingual checkpoint: $($candidate.Path)"
        }
        if (Test-Path -LiteralPath $candidate.Path) {
            Write-Warn "Ignoring incomplete model directory: $($candidate.Path)"
        }
    }
    throw 'No complete laya-multilingual checkpoint found. Run bootstrap.ps1 first, or pass -ModelPath.'
}

function Test-PythonImports {
    param([string]$Path)
    & $Path -c "import laya, laya.serve, torch, fastapi, uvicorn" 2>$null
    return ($LASTEXITCODE -eq 0)
}

function Resolve-Python {
    param([string]$Explicit)
    $candidates = @()
    if ($Explicit) {
        $candidates += [pscustomobject]@{ Path = $Explicit; Explicit = $true }
    }
    else {
        $candidates += [pscustomobject]@{ Path = (Join-Path $Root '.venv\Scripts\python.exe'); Explicit = $false }
        $candidates += [pscustomobject]@{ Path = (Join-Path $RepoRoot '.tmp-laya\venv\Scripts\python.exe'); Explicit = $false }
    }

    foreach ($candidate in $candidates) {
        if (-not (Test-Path -LiteralPath $candidate.Path -PathType Leaf)) {
            if ($candidate.Explicit) {
                throw "Python not found: $($candidate.Path)"
            }
            continue
        }
        if (Test-PythonImports -Path $candidate.Path) {
            if (-not $candidate.Explicit -and $candidate.Path -match '\\\.tmp-laya\\') {
                Write-Warn "Sidecar venv missing; using spike venv: $($candidate.Path)"
            }
            return (Resolve-Path -LiteralPath $candidate.Path).Path
        }
        if ($candidate.Explicit) {
            throw "Python exists but cannot import laya, torch, fastapi and uvicorn: $($candidate.Path)"
        }
        Write-Warn "Ignoring Python without laya[serve]/torch: $($candidate.Path)"
    }
    throw 'No usable sidecar Python. Run bootstrap.ps1, or pass -Python.'
}

function Stop-ProcessTree {
    param([int]$ProcessId)
    # A uv-managed venv's python.exe is a small launcher whose child is the real
    # interpreter (and therefore the actual uvicorn server).  taskkill /T is the
    # reliable way to take both down on Windows.
    $taskkill = Join-Path $env:SystemRoot 'System32\taskkill.exe'
    if (Test-Path -LiteralPath $taskkill -PathType Leaf) {
        & $taskkill /PID $ProcessId /T /F 2>$null | Out-Null
    }
    else {
        Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
    }
    $deadline = (Get-Date).AddSeconds(5)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 200
    }
    if (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue) {
        Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
    }
}

function Stop-ManagedProcess {
    param(
        [int]$ProcessId,
        [int]$PortNumber
    )
    $proc = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if (-not $proc) {
        # The launcher may already be gone while its child still owns the port.
        $owner = Get-PortOwner -PortNumber $PortNumber
        if ($owner -gt 0 -and (Test-IsLayaProcess -ProcessId $owner)) {
            Write-Warn "launcher $ProcessId is gone; stopping listener $owner"
            Stop-ProcessTree -ProcessId $owner
        }
        if (Test-Path -LiteralPath $PidFile) { Remove-Item -LiteralPath $PidFile -Force }
        if (Test-Path -LiteralPath $PortFile) { Remove-Item -LiteralPath $PortFile -Force }
        return
    }
    if (-not (Test-IsLayaProcess -ProcessId $ProcessId)) {
        throw "PID $ProcessId is not a laya-serve process; refusing to stop it. Remove $PidFile manually if stale."
    }
    Stop-ProcessTree -ProcessId $ProcessId

    # A child can outlive the launcher; stop it too before declaring the port free.
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-Date) -lt $deadline) {
        if (-not (Test-PortListening -PortNumber $PortNumber)) { break }
        $owner = Get-PortOwner -PortNumber $PortNumber
        if ($owner -gt 0 -and (Test-IsLayaProcess -ProcessId $owner)) {
            Stop-Process -Id $owner -Force -ErrorAction SilentlyContinue
        }
        Start-Sleep -Milliseconds 200
    }

    if (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue) {
        throw "process $ProcessId is still alive after taskkill /T"
    }
    if (Test-PortListening -PortNumber $PortNumber) {
        $owner = Get-PortOwner -PortNumber $PortNumber
        throw "port $PortNumber is still listening after stop (owner PID: $owner)"
    }
    if (Test-Path -LiteralPath $PidFile) { Remove-Item -LiteralPath $PidFile -Force }
    if (Test-Path -LiteralPath $PortFile) { Remove-Item -LiteralPath $PortFile -Force }
}

function Show-Logs {
    foreach ($file in @($OutLog, $ErrLog)) {
        if (Test-Path -LiteralPath $file -PathType Leaf) {
            Write-Host "--- $file (last 40 lines) ---" -ForegroundColor DarkGray
            Get-Content -LiteralPath $file -Tail 40
        }
    }
}

function Set-SidecarEnvironment {
    param(
        [string]$ModelDir,
        [int]$PortNumber
    )
    $extra = @{}
    $extra['zh-tier'] = $ModelDir
    $env:LAYA_HOST = '127.0.0.1'
    $env:LAYA_PORT = [string]$PortNumber
    $env:LAYA_DEVICE = 'cpu'
    $env:LAYA_EXTRA_MODELS = ($extra | ConvertTo-Json -Compress)
    $env:LAYA_MODELS = 'zh-tier'
    $env:LAYA_DEFAULT_MODEL = 'zh-tier'
    $env:LAYA_PRELOAD = '1'
    $env:LAYA_MAX_LOADED = '1'
    $env:LAYA_IDLE_UNLOAD_SECONDS = '0'
    $env:LAYA_LOG_LEVEL = 'info'
    if ([string]::IsNullOrWhiteSpace($env:LAYA_THREADS)) { $env:LAYA_THREADS = '4' }
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    # The checkpoint is local; never let a preload path phone home.
    $env:HF_HUB_OFFLINE = '1'
    # This managed sidecar is localhost-only and unauthenticated by design.
    if ($env:LAYA_API_KEY) {
        Write-Warn 'Removing LAYA_API_KEY for the managed local sidecar.'
        Remove-Item Env:LAYA_API_KEY -ErrorAction SilentlyContinue
    }
}

try {
    $modeCount = 0
    if ($Stop) { $modeCount++ }
    if ($Status) { $modeCount++ }
    if ($Foreground) { $modeCount++ }
    if ($modeCount -gt 1) {
        throw 'Use only one of -Stop, -Status, -Foreground.'
    }
    New-Item -ItemType Directory -Force -Path $RunDir | Out-Null

    if ($Stop) {
        $serverPid = Read-ServerPid
        $effectivePort = Read-ServerPort -DefaultPort $Port
        if ($serverPid -le 0) {
            if (Test-PortListening -PortNumber $effectivePort) {
                throw "port $effectivePort is in use (PID $(Get-PortOwner -PortNumber $effectivePort)) but no PID file; refusing to kill an unmanaged process."
            }
            Write-Host 'Laya sidecar is not running.'
            exit 0
        }
        if (-not (Get-Process -Id $serverPid -ErrorAction SilentlyContinue)) {
            Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath $PortFile -Force -ErrorAction SilentlyContinue
            if (Test-PortListening -PortNumber $effectivePort) {
                Write-Warn "stale PID removed, but port $effectivePort is still in use (PID $(Get-PortOwner -PortNumber $effectivePort))."
            }
            Write-Host 'Laya sidecar is not running (stale PID removed).'
            exit 0
        }
        Write-Step "Stopping laya-serve (PID $serverPid, port $effectivePort)"
        Stop-ManagedProcess -ProcessId $serverPid -PortNumber $effectivePort
        Write-Ok "stopped; port $effectivePort is free"
        exit 0
    }

    if ($Status) {
        $serverPid = Read-ServerPid
        $effectivePort = Read-ServerPort -DefaultPort $Port
        $alive = $false
        if ($serverPid -gt 0) {
            $alive = [bool](Get-Process -Id $serverPid -ErrorAction SilentlyContinue)
        }
        $health = Wait-Health -PortNumber $effectivePort -TimeoutSeconds 4
        if ($health) {
            $listener = Get-PortOwner -PortNumber $effectivePort
            if ($serverPid -gt 0 -and $alive) {
                Write-Ok "running: PID $serverPid (listener $listener), http://127.0.0.1:$effectivePort"
            }
            else {
                Write-Warn "health is OK but no managed PID file; the server was started outside start.ps1."
            }
            $health | ConvertTo-Json -Depth 6
            exit 0
        }
        if ($alive) {
            Write-Host "[error] PID $serverPid is alive but /health is not OK on port $effectivePort." -ForegroundColor Red
            Show-Logs
            exit 2
        }
        Write-Host 'Laya sidecar is not running.'
        exit 1
    }

    $modelDir = Resolve-ModelPath -Explicit $ModelPath
    $pythonPath = Resolve-Python -Explicit $Python

    if ($Foreground) {
        if (Test-PortListening -PortNumber $Port) {
            throw "port $Port is already in use (PID $(Get-PortOwner -PortNumber $Port)); stop it before --Foreground."
        }
        Set-SidecarEnvironment -ModelDir $modelDir -PortNumber $Port
        Write-Step "Running in foreground: $pythonPath -m laya.serve"
        & $pythonPath -m laya.serve
        exit $LASTEXITCODE
    }

    $serverPid = Read-ServerPid
    $effectivePort = $Port
    if ($serverPid -gt 0) {
        if (Get-Process -Id $serverPid -ErrorAction SilentlyContinue) {
            if (-not (Test-IsLayaProcess -ProcessId $serverPid)) {
                throw "PID file $PidFile points to PID $serverPid, which is not laya-serve. Remove the file manually."
            }
            $managedPort = Read-ServerPort -DefaultPort $Port
            if ($PSBoundParameters.ContainsKey('Port') -and $managedPort -ne $Port) {
                throw "laya-serve is already running on port $managedPort; run -Stop first or omit -Port."
            }
            $effectivePort = $managedPort
            $health = Wait-Health -PortNumber $effectivePort -TimeoutSeconds 20 -ProcessId $serverPid
            if ($health) {
                Write-Ok "already running: PID $serverPid, http://127.0.0.1:$effectivePort"
                exit 0
            }
            throw "PID $serverPid is alive but /health is not ready on port $effectivePort. Run -Stop or inspect $ErrLog."
        }
        Write-Warn "Removing stale PID file: $PidFile"
        Remove-Item -LiteralPath $PidFile -Force
        Remove-Item -LiteralPath $PortFile -Force -ErrorAction SilentlyContinue
        $effectivePort = $Port
    }

    if (Test-PortListening -PortNumber $effectivePort) {
        throw "port $effectivePort is already in use (PID $(Get-PortOwner -PortNumber $effectivePort)); refusing to start a duplicate."
    }

    # Keep the previous run's log for debugging; the new run gets a fresh file.
    foreach ($file in @($OutLog, $ErrLog)) {
        if (Test-Path -LiteralPath $file -PathType Leaf) {
            Move-Item -LiteralPath $file -Destination "$file.prev" -Force
        }
    }

    Set-SidecarEnvironment -ModelDir $modelDir -PortNumber $effectivePort
    Write-Step "Starting laya-serve on 127.0.0.1:$effectivePort (model: $modelDir)"
    $serverArgs = @('-m', 'laya.serve')
    $process = Start-Process -FilePath $pythonPath `
        -ArgumentList $serverArgs `
        -WorkingDirectory $Root `
        -RedirectStandardOutput $OutLog `
        -RedirectStandardError $ErrLog `
        -WindowStyle Hidden `
        -PassThru
    $script:StartedPid = [int]$process.Id
    Set-Content -LiteralPath $PidFile -Value ([string]$process.Id) -Encoding ASCII
    Set-Content -LiteralPath $PortFile -Value ([string]$effectivePort) -Encoding ASCII
    Write-Host "    PID $($process.Id), log: $ErrLog"

    $health = Wait-Health -PortNumber $effectivePort -TimeoutSeconds $TimeoutSec -ProcessId $process.Id
    if (-not $health) {
        throw "server did not become healthy within ${TimeoutSec}s"
    }
    Write-Ok "health OK (loaded: $($health.loaded -join ', '))"

    $warm = Invoke-Warmup -PortNumber $effectivePort
    $listener = Get-PortOwner -PortNumber $effectivePort
    Write-Ok ("ready: PID {0} (listener {1}), choice={2}, warmup={3:n0} ms" -f $process.Id, $listener, $warm.Choice, $warm.Ms)
    Write-Host "    health : http://127.0.0.1:$effectivePort/health"
    Write-Host "    decide : POST http://127.0.0.1:$effectivePort/v1/systemone  (model=zh-tier)"
    Write-Host "    log    : $OutLog / $ErrLog"
    exit 0
}
catch {
    $message = $_.Exception.Message
    Write-Host ''
    Write-Host "ERROR: $message" -ForegroundColor Red
    if ($script:StartedPid -gt 0) {
        try {
            Stop-ProcessTree -ProcessId $script:StartedPid
        }
        catch {
            Write-Warn "could not stop failed process tree $($script:StartedPid): $($_.Exception.Message)"
        }
        if ($effectivePort -gt 0) {
            $owner = Get-PortOwner -PortNumber $effectivePort
            if ($owner -gt 0 -and (Test-IsLayaProcess -ProcessId $owner)) {
                try {
                    Stop-ProcessTree -ProcessId $owner
                }
                catch {
                    Write-Warn "could not stop listener $owner"
                }
            }
        }
        if (Test-Path -LiteralPath $PidFile) {
            Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        }
        if (Test-Path -LiteralPath $PortFile) {
            Remove-Item -LiteralPath $PortFile -Force -ErrorAction SilentlyContinue
        }
    }
    Show-Logs
    exit 1
}
