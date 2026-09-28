# Download the official llama.cpp llama-server binary for Windows.
#
# Output:
#   bin\win-x64\llama-server.exe
#   bin\win-x64\*.dll
#   bin\win-x64\backends\vulkan\llama-server.exe  (with -Backend vulkan)
#   bin\win-x64\backends\vulkan\*.dll
#
# Honors:
#   $env:LLAMA_CPP_RELEASE = b9371 by default

param(
  [ValidateSet("cpu", "vulkan", "cuda")]
  [string] $Backend = "cuda",
  [string] $Target = "win-x64",
  [string] $Tag = $(if ($env:LLAMA_CPP_RELEASE) { $env:LLAMA_CPP_RELEASE } else { "b11237" }),
  [string] $OutDir = ""
)

$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Resolve-Path (Join-Path $here "..")

switch ("$Target/$Backend") {
  "win-x64/cpu"    { $asset = "llama-$Tag-bin-win-cpu-x64.zip" }
  "win-x64/vulkan" { $asset = "llama-$Tag-bin-win-vulkan-x64.zip" }
  "win-x64/cuda"   { $asset = "llama-$Tag-bin-win-cuda-12.4-x64.zip" }
  "win-arm64/cpu"  { $asset = "llama-$Tag-bin-win-cpu-arm64.zip" }
  default { throw "Unsupported Target/Backend: $Target/$Backend" }
}

if (-not $OutDir) {
  $OutDir = Join-Path $root "bin\$Target"
  if ($Backend -eq "vulkan") {
    $OutDir = Join-Path $OutDir "backends\vulkan"
  }
}

# GitHub is effectively unreachable from mainland China: measured 0.01 MB/s on a
# 252 MB release asset (~7 hours). These mirrors measured 1.1-1.5 MB/s (~3 min).
# Order: fastest first, official last as a courtesy fallback.
# Override the whole chain with $env:LLAMA_CPP_MIRRORS = "https://a/https://github.com/...,https://b/..."
$relUrl = "https://github.com/ggml-org/llama.cpp/releases/download/$Tag/$asset"
$mirrors = @()
if ($env:LLAMA_CPP_MIRRORS) {
  $mirrors += ($env:LLAMA_CPP_MIRRORS -split ",") | ForEach-Object { $_.Trim() } | Where-Object { $_ }
}
$mirrors += @(
  "https://gh-proxy.com/$relUrl",
  "https://ghproxy.net/$relUrl",
  "https://ghfast.top/$relUrl",
  $relUrl
)

$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("pet-llama-" + [System.Guid]::NewGuid().ToString("N"))
$archive = Join-Path $tmp $asset
$extract = Join-Path $tmp "extract"

Write-Host "==> Fetch llama.cpp ${Tag}: $asset" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $tmp, $extract, $OutDir | Out-Null

function Get-LlamaArchive {
  param([string[]] $Candidates, [string] $Destination)
  foreach ($candidate in $Candidates) {
    $isOfficial = ($candidate -eq $relUrl)
    Write-Host "  try: $candidate" -ForegroundColor DarkGray
    Remove-Item -Force $Destination -ErrorAction SilentlyContinue
    $args = @("-sL", "--max-time", "1800", "--retry", "2", "--retry-delay", "2", "-o", $Destination)
    if (-not $isOfficial) { $args = @("--noproxy", "*") + $args }
    & curl.exe $args $candidate
    if ($LASTEXITCODE -eq 0 -and (Test-Path $Destination) -and (Get-Item $Destination).Length -gt 1MB) {
      Write-Host "  got it from: $candidate" -ForegroundColor Green
      return $true
    }
    Write-Host "    failed (exit=$LASTEXITCODE), next source" -ForegroundColor Yellow
  }
  return $false
}

try {
  if (-not (Get-LlamaArchive -Candidates $mirrors -Destination $archive)) {
    throw "all download sources failed for $asset (set `$env:LLAMA_CPP_MIRRORS to override)"
  }
  Expand-Archive -Path $archive -DestinationPath $extract -Force

  $server = Get-ChildItem -Path $extract -Recurse -Filter "llama-server.exe" |
    Select-Object -First 1
  if (-not $server) {
    throw "llama-server.exe not found in $asset"
  }

  $releaseRoot = $server.Directory.FullName
  while ((Split-Path -Parent $releaseRoot) -ne $extract -and $releaseRoot -ne $extract) {
    $releaseRoot = Split-Path -Parent $releaseRoot
  }

  Copy-Item -Path (Join-Path $releaseRoot "*") -Destination $OutDir -Recurse -Force
  $outServer = Join-Path $OutDir "llama-server.exe"
  if (-not (Test-Path $outServer)) {
    Copy-Item -Path (Join-Path $server.Directory.FullName "*") -Destination $OutDir -Recurse -Force
  }
  if (-not (Test-Path $outServer)) {
    throw "copy failed: $outServer missing"
  }

  Write-Host "==> OK -> $outServer" -ForegroundColor Green
  # Only run --version when the binary matches the host architecture;
  # cross-arch binaries (e.g. arm64 on an x64 runner) can't execute.
  $hostArch = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString().ToLower()
  $targetIsArm = $Target -match "arm64"
  $hostIsArm = $hostArch -eq "arm64"
  if ($targetIsArm -eq $hostIsArm) {
    & $outServer --version
  } else {
    Write-Host "  (skipping --version: $Target binary on $hostArch host)" -ForegroundColor Yellow
  }
} finally {
  Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}
