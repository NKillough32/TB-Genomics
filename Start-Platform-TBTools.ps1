param(
    [switch]$SkipBrowser
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$venvPython = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    Write-Host '[ERROR] Python virtual environment not found at .venv\Scripts\python.exe'
    Write-Host ''
    Write-Host 'Run these once from the project root:'
    Write-Host '  python -m venv .venv'
    Write-Host '  .venv\Scripts\python.exe -m pip install -r backend\requirements.txt'
    exit 1
}

if (-not $env:DATABASE_URL) {
    $env:DATABASE_URL = 'postgresql://tb:tb@localhost/tb_surveillance'
}

if (-not $env:TBPROFILER_WSL_ENV) {
    $env:TBPROFILER_WSL_ENV = 'tbtools'
}

$env:TBPROFILER_WSL_FALLBACK = '1'
$env:TBPROFILER_DOCKER_FALLBACK = '1'

$wslExe = (Get-Command wsl.exe -ErrorAction SilentlyContinue).Source
if ($wslExe) {
    $bashCommand = @'
MAMBA_CMD="$HOME/micromamba"
if [ ! -x "$MAMBA_CMD" ]; then
  MAMBA_CMD=micromamba
fi

echo "[INFO] Checking TBProfiler and Mykrobe in WSL env: tbtools"
if command -v micromamba >/dev/null 2>&1 || [ -x "$HOME/micromamba" ]; then
  "$MAMBA_CMD" run -n tbtools tb-profiler --version || true
  "$MAMBA_CMD" run -n tbtools mykrobe --help || true
else
  echo "[WARN] micromamba not found in WSL. Install micromamba and the tbtools environment."
fi

echo
echo "[INFO] Leaving a WSL login shell open. Close this window when finished."
exec bash -l
'@
    Start-Process -FilePath $wslExe -ArgumentList @('bash', '-lc', $bashCommand) -WorkingDirectory $root | Out-Null
} else {
    Write-Host '[WARN] wsl.exe is not available. TBProfiler/Mykrobe fallback checks will stay limited to the local executable path.'
}

Write-Host 'Launching backend and GUI with TBProfiler fallback enabled...'
Write-Host "DATABASE_URL=$env:DATABASE_URL"
Write-Host "TBPROFILER_WSL_ENV=$env:TBPROFILER_WSL_ENV"
Write-Host "TBPROFILER_WSL_FALLBACK=$env:TBPROFILER_WSL_FALLBACK"
Write-Host "TBPROFILER_DOCKER_FALLBACK=$env:TBPROFILER_DOCKER_FALLBACK"

Start-Process -FilePath $venvPython -ArgumentList @('-m', 'uvicorn', 'backend.app:app', '--reload') -WorkingDirectory $root | Out-Null
Start-Process -FilePath $venvPython -ArgumentList @('-m', 'http.server', '8081') -WorkingDirectory (Join-Path $root 'gui') | Out-Null

for ($i = 0; $i -lt 30; $i++) {
    try {
        if (Test-NetConnection -ComputerName 'localhost' -Port 8000 -InformationLevel Quiet -WarningAction SilentlyContinue) {
            break
        }
    } catch {
    }
    Start-Sleep -Seconds 1
}

Write-Host '[INFO] Running lineage/DR validation preflight to refresh tool status...'
try {
    & $venvPython (Join-Path $root 'scripts\run_lineage_dr_validation.py')
} catch {
    Write-Host "[WARN] Lineage/DR validation preflight failed: $($_.Exception.Message)"
}

Start-Sleep -Seconds 1
if (-not $SkipBrowser) {
    Start-Process 'http://localhost:8081' | Out-Null
}
