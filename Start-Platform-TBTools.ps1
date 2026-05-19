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
    $rootResolved = (Resolve-Path $root).Path
    $wslScript = Join-Path $rootResolved 'scripts\launch_tbtools_wsl.sh'

    if ((Test-Path $wslScript -PathType Leaf) -and ($rootResolved -match '^[A-Za-z]:\\')) {
        $drive = $rootResolved.Substring(0, 1).ToLower()
        $tail = $rootResolved.Substring(2).Replace('\', '/')
        $rootWsl = "/mnt/$drive$tail"
        $wslScriptPath = "$rootWsl/scripts/launch_tbtools_wsl.sh"
        $wslWindowCommand = "title TBTools WSL && `"$wslExe`" bash -lc `"bash '$wslScriptPath'`""
        Start-Process -FilePath 'cmd.exe' -ArgumentList @('/k', $wslWindowCommand) -WorkingDirectory $root | Out-Null
    } else {
        Write-Host '[WARN] WSL launcher script path could not be resolved. Opening plain WSL shell.'
        Start-Process -FilePath 'cmd.exe' -ArgumentList @('/k', 'title TBTools WSL && wsl.exe') -WorkingDirectory $root | Out-Null
    }
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

if (-not $SkipBrowser) {
    for ($i = 0; $i -lt 10; $i++) {
        try {
            if (Test-NetConnection -ComputerName 'localhost' -Port 8081 -InformationLevel Quiet -WarningAction SilentlyContinue) {
                break
            }
        } catch {
        }
        Start-Sleep -Milliseconds 500
    }

    Start-Process 'http://localhost:8081' | Out-Null
}

Write-Host '[INFO] Running lineage/DR validation preflight in background to refresh tool status...'
try {
    Start-Process -FilePath $venvPython -ArgumentList @(Join-Path $root 'scripts\run_lineage_dr_validation.py') -WorkingDirectory $root | Out-Null
} catch {
    Write-Host "[WARN] Could not start lineage/DR validation preflight: $($_.Exception.Message)"
}
