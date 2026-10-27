# Optional Laya sidecar bootstrap (Windows-first).
#
# Creates .venv, installs laya[onnx,serve]==0.4.15, provisions laya-multilingual, and
# optionally exports ONNX.  Nothing is bundled with the app; every large
# artifact is either downloaded or reused from an existing local cache.
#
# ASCII-only on purpose: Windows PowerShell 5.1 reads BOM-less .ps1 files as
# ANSI, and this script must stay safe to run from cmd.exe / Electron.

[CmdletBinding()]
param(
    [switch]$Check,
    [switch]$ExportOnnx,
    [ValidateSet('auto', 'hf', 'modelscope')]
    [string]$Source = 'auto',
    [string]$Python = $env:LAYA_PYTHON,
    [string]$ModelPath = $env:LAYA_MODEL_PATH,
    [string]$PipIndexUrl = $env:LAYA_PIP_INDEX_URL,
    [switch]$OfficialPyPI
)

$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
if (-not $Root) { $Root = Split-Path -Parent $MyInvocation.MyCommand.Path }
$VenvDir = Join-Path $Root '.venv'
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
$RepoRoot = Split-Path -Parent (Split-Path -Parent $Root)
$ModelFiles = @(
    'config.json',
    'rl_agent_config.json',
    'model.safetensors',
    'encoder\config.json',
    'tokenizer\tokenizer.json',
    'tokenizer\tokenizer_config.json'
)

function Write-Step([string]$Message) {
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Ok([string]$Message) {
    Write-Host "[ok] $Message" -ForegroundColor Green
}

function Write-Warn([string]$Message) {
    Write-Host "[warn] $Message" -ForegroundColor Yellow
}

function Test-PythonCandidate {
    param(
        [string]$Exe,
        [string[]]$Prefix = @()
    )
    if (-not $Exe) { return $null }
    if (($Exe -match '[\\/]') -and -not (Test-Path -LiteralPath $Exe -PathType Leaf)) {
        return $null
    }
    try {
        $raw = & $Exe @Prefix -c "import sys,struct; print(str(sys.version_info[0])+'.'+str(sys.version_info[1])+' '+str(struct.calcsize('P')*8))" 2>$null
    }
    catch {
        return $null
    }
    if ($LASTEXITCODE -ne 0) { return $null }
    $line = @($raw) | Select-Object -Last 1
    if (-not $line) { return $null }
    $parts = ($line.ToString().Trim() -split '\s+')
    if ($parts.Count -lt 2) { return $null }
    $versionParts = $parts[0] -split '\.'
    if ($versionParts.Count -lt 2) { return $null }
    $major = [int]$versionParts[0]
    $minor = [int]$versionParts[1]
    $bits = [int]$parts[1]
    if (($major -eq 3) -and ($minor -ge 10) -and ($minor -le 12) -and ($bits -eq 64)) {
        return [pscustomobject]@{
            Exe     = $Exe
            Prefix  = $Prefix
            Version = "$major.$minor"
            Bits    = $bits
        }
    }
    return $null
}

function Find-Python {
    if ($Python) {
        $found = Test-PythonCandidate -Exe $Python
        if (-not $found) {
            throw "LAYA_PYTHON/-Python must point to a 64-bit Python 3.10-3.12: $Python"
        }
        return $found
    }

    $candidates = @(
        @{ Exe = 'py'; Prefix = @('-3.12') },
        @{ Exe = 'py'; Prefix = @('-3.11') },
        @{ Exe = 'py'; Prefix = @('-3.10') },
        @{ Exe = 'python3.12'; Prefix = @() },
        @{ Exe = 'python3.11'; Prefix = @() },
        @{ Exe = 'python3.10'; Prefix = @() },
        @{ Exe = 'python'; Prefix = @() },
        @{ Exe = 'python3'; Prefix = @() }
    )
    foreach ($candidate in $candidates) {
        $found = Test-PythonCandidate -Exe $candidate.Exe -Prefix $candidate.Prefix
        if ($found) { return $found }
    }

    # uv-managed interpreters are not registered with the py launcher.
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        foreach ($version in @('3.12', '3.11', '3.10')) {
            try {
                $resolved = & uv python find $version 2>$null | Select-Object -Last 1
            }
            catch {
                $resolved = $null
            }
            if ($resolved) {
                $found = Test-PythonCandidate -Exe $resolved.ToString().Trim()
                if ($found) { return $found }
            }
        }
    }

    $common = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python310\python.exe')
    )
    foreach ($path in $common) {
        $found = Test-PythonCandidate -Exe $path
        if ($found) { return $found }
    }

    $uvRoot = Join-Path $env:APPDATA 'uv\python'
    if (Test-Path -LiteralPath $uvRoot -PathType Container) {
        $uvPythons = Get-ChildItem -LiteralPath $uvRoot -Filter 'python.exe' -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match 'cpython-3\.(10|11|12)' }
        foreach ($item in $uvPythons) {
            $found = Test-PythonCandidate -Exe $item.FullName
            if ($found) { return $found }
        }
    }

    throw 'No 64-bit Python 3.10-3.12 found. Install Python 3.12, or pass -Python C:\path\to\python.exe.'
}

function Test-ModelDir {
    param([string]$Path)
    if (-not $Path) { return $false }
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) { return $false }
    foreach ($rel in $ModelFiles) {
        $file = Join-Path $Path $rel
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { return $false }
    }
    $weights = Get-Item -LiteralPath (Join-Path $Path 'model.safetensors')
    if ($weights.Length -lt 600000000) { return $false }
    $tokenizer = Get-Item -LiteralPath (Join-Path $Path 'tokenizer\tokenizer.json')
    if ($tokenizer.Length -lt 30000000) { return $false }
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
    return $null
}

function Find-ExistingOnnx {
    $candidates = @(
        (Join-Path $Root 'models\laya-multilingual.onnx'),
        (Join-Path $RepoRoot '.tmp-laya\laya-multilingual.onnx')
    )
    foreach ($path in $candidates) {
        if ((Test-Path -LiteralPath $path -PathType Leaf) -and ((Get-Item -LiteralPath $path).Length -gt 1000000000)) {
            return (Resolve-Path -LiteralPath $path).Path
        }
    }
    return $null
}

function Resolve-PipIndex {
    if ($PipIndexUrl) { return $PipIndexUrl }
    if ($OfficialPyPI) { return 'https://pypi.org/simple' }
    if ($env:PIP_INDEX_URL) { return $env:PIP_INDEX_URL }
    # Pick a mirror that is usually fast from mainland China.  Override with
    # -PipIndexUrl, -OfficialPyPI, or LAYA_PIP_INDEX_URL.
    return 'https://pypi.tuna.tsinghua.edu.cn/simple'
}

function Show-Check {
    param([string]$ModelDir)
    $ready = $true
    if (Test-Path -LiteralPath $VenvPython -PathType Leaf) {
        try {
            $venvVersion = & $VenvPython -c "import sys; print(str(sys.version_info[0])+'.'+str(sys.version_info[1]))" 2>$null
            if ($LASTEXITCODE -eq 0) {
                Write-Ok "venv: $VenvDir (Python $($venvVersion.ToString().Trim()))"
                $venvPackages = & $VenvPython -c "import laya, torch, fastapi, uvicorn; print('laya='+laya.__version__)" 2>$null
                if ($LASTEXITCODE -eq 0) {
                    Write-Ok "venv packages: $($venvPackages.ToString().Trim()) + torch/fastapi/uvicorn"
                }
                else {
                    Write-Warn "venv exists but lacks laya/torch/fastapi/uvicorn; rerun bootstrap"
                    $ready = $false
                }
            }
            else {
                Write-Warn "venv exists but its Python cannot run: $VenvPython"
                $ready = $false
            }
        }
        catch {
            Write-Warn "venv exists but its Python cannot run: $VenvPython"
            $ready = $false
        }
    }
    else {
        Write-Warn "venv missing: $VenvPython"
        $ready = $false
    }

    if ($ModelDir) {
        $bytes = (Get-Item -LiteralPath (Join-Path $ModelDir 'model.safetensors')).Length +
                 (Get-Item -LiteralPath (Join-Path $ModelDir 'tokenizer\tokenizer.json')).Length
        Write-Ok ("model: {0} ({1:n1} MB of weights+tokenizer)" -f $ModelDir, ($bytes / 1e6))
    }
    else {
        Write-Warn "model missing: run bootstrap.ps1 without -Check"
        $ready = $false
    }

    $onnx = Find-ExistingOnnx
    if ($onnx) {
        Write-Ok ("onnx: {0} ({1:n2} GB, optional, not used by laya-serve)" -f $onnx, ((Get-Item -LiteralPath $onnx).Length / 1e9))
    }
    else {
        Write-Host "[--] onnx: not exported (optional; -ExportOnnx creates it)"
    }

    if ($ready) { return 0 }
    return 1
}

try {
    Write-Step "Laya sidecar: $Root"
    $modelDir = Resolve-ModelPath -Explicit $ModelPath
    if ($Check) {
        exit (Show-Check -ModelDir $modelDir)
    }

    if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
        $py = Find-Python
        Write-Ok "Python $($py.Version) ($($py.Bits)-bit): $($py.Exe)"
        Write-Step "Creating venv: $VenvDir"
        $venvArgs = @($py.Prefix) + @('-m', 'venv', $VenvDir)
        & $py.Exe @venvArgs
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
            throw "python -m venv failed (exit $LASTEXITCODE)"
        }
        Write-Ok "venv created"
    }
    else {
        $venvVersion = & $VenvPython -c "import sys; print(str(sys.version_info[0])+'.'+str(sys.version_info[1]))" 2>$null
        if ($LASTEXITCODE -ne 0) {
            throw "existing venv Python cannot run: $VenvPython (delete .venv and rerun bootstrap)"
        }
        Write-Ok "venv already exists: $VenvDir (Python $($venvVersion.ToString().Trim()))"
    }

    $indexUrl = Resolve-PipIndex
    Write-Step "Installing laya[onnx,serve]==0.4.15 (index: $indexUrl)"
    Write-Host '    torch is a hard dependency of laya; this is expected to be the large step.'
    & $VenvPython -m pip install --disable-pip-version-check --timeout 60 --retries 5 --index-url $indexUrl 'laya[onnx,serve]==0.4.15'
    if ($LASTEXITCODE -ne 0) {
        throw "pip install laya[onnx,serve]==0.4.15 failed (exit $LASTEXITCODE). Check the network/index URL and retry."
    }

    Write-Step 'Verifying imports'
    $verify = & $VenvPython -c "import laya, torch, onnxruntime, fastapi, uvicorn; print('laya='+laya.__version__+' torch='+torch.__version__+' ort='+onnxruntime.__version__)" 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "installed packages failed to import: $verify"
    }
    Write-Ok ($verify | Select-Object -Last 1)

    if (-not $modelDir) {
        $dest = Join-Path $Root 'models\laya-multilingual'
        Write-Step "Downloading laya-multilingual (source: $Source) -> $dest"
        & $VenvPython (Join-Path $Root 'provision.py') download --source $Source --dest $dest
        if ($LASTEXITCODE -ne 0) {
            throw "model download failed (exit $LASTEXITCODE); the model directory was left in place for a resume"
        }
        if (-not (Test-ModelDir $dest)) {
            throw "downloaded model failed validation: $dest"
        }
        $modelDir = (Resolve-Path -LiteralPath $dest).Path
    }
    else {
        Write-Ok "Reusing model (no copy): $modelDir"
        if ($modelDir -match '\\\.tmp-laya\\') {
            Write-Warn 'Model is owned by the spike cache. Delete .tmp-laya and rerun bootstrap to move it into the sidecar.'
        }
    }

    Write-Ok ("model ready: {0} ({1:n1} MB)" -f $modelDir, ((Get-Item -LiteralPath (Join-Path $modelDir 'model.safetensors')).Length / 1e6))

    if ($ExportOnnx) {
        $existingOnnx = Find-ExistingOnnx
        if ($existingOnnx) {
            Write-Ok "ONNX already exists: $existingOnnx (not copied)"
        }
        else {
            $onnxOut = Join-Path $Root 'models\laya-multilingual.onnx'
            Write-Step "Exporting ONNX -> $onnxOut (one-off, ~1.3 GB)"
            & $VenvPython (Join-Path $Root 'export_onnx.py') --model $modelDir --output $onnxOut
            if ($LASTEXITCODE -ne 0) {
                throw "ONNX export failed (exit $LASTEXITCODE)"
            }
            $exported = Get-Item -LiteralPath $onnxOut -ErrorAction SilentlyContinue
            if (-not $exported -or $exported.Length -lt 1000000000) {
                throw "ONNX export did not produce a full file: $onnxOut"
            }
            Write-Ok "ONNX exported: $onnxOut"
            Write-Warn 'laya-serve 0.4.15 does not serve ONNX; this artifact is not used by start.ps1.'
        }
    }

    Write-Host ''
    Write-Host 'Laya sidecar is ready.' -ForegroundColor Green
    Write-Host "  start : powershell -NoProfile -ExecutionPolicy Bypass -File `"$Root\start.ps1`""
    Write-Host "  status: powershell -NoProfile -ExecutionPolicy Bypass -File `"$Root\start.ps1`" -Status"
    Write-Host "  stop  : powershell -NoProfile -ExecutionPolicy Bypass -File `"$Root\start.ps1`" -Stop"
    Write-Host '  health: http://127.0.0.1:8765/health'
    Write-Host '  model : zh-tier (local, 127.0.0.1 only)'
}
catch {
    Write-Host ''
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
