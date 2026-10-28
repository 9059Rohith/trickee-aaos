param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8000
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $projectRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    $python = (Get-Command python -ErrorAction Stop).Source
}
if (-not $env:TRICKEE_DEMO_MODE) { $env:TRICKEE_DEMO_MODE = '1' }
if (-not $env:TRICKEE_DB) { $env:TRICKEE_DB = Join-Path $projectRoot 'data/cloud.db' }
Push-Location $projectRoot
try {
    & $python -m uvicorn trickee.api:app --host 127.0.0.1 --port $Port
    if ($LASTEXITCODE -ne 0) { throw "Local server exited with code $LASTEXITCODE" }
} finally {
    Pop-Location
}
