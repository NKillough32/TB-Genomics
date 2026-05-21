param(
    [string]$ServerName = "",
    [string]$Title = "TB Genomic Surveillance",
    [string]$PythonVersion = "3.11.9"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

python scripts/check_posit_readiness.py

$argsList = @("deploy", "fastapi", "--entrypoint", "backend.app:app", "--title", $Title, "--override-python-version", $PythonVersion)
if ($ServerName.Trim()) {
    $argsList += @("--name", $ServerName)
}
$argsList += "."

rsconnect @argsList
