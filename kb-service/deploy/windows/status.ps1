$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

$port = 8000
$envFile = Join-Path $root ".env"
if (Test-Path $envFile) {
    $m = Select-String -Path $envFile -Pattern '^\s*APP_PORT\s*=\s*(\d+)' | Select-Object -First 1
    if ($m) { $port = [int]$m.Matches[0].Groups[1].Value }
}

$runDir = Join-Path $root "run"
$servicePid = 0
$pidFile = Join-Path $runDir "service.pid"
if (Test-Path $pidFile) {
    [void][int]::TryParse((Get-Content $pidFile -Raw).Trim(), [ref]$servicePid)
}
$uvicornPid = 0
$childPidFile = Join-Path $runDir "uvicorn.pid"
if (Test-Path $childPidFile) {
    [void][int]::TryParse((Get-Content $childPidFile -Raw).Trim(), [ref]$uvicornPid)
}

$running = $servicePid -gt 0 -and (Get-Process -Id $servicePid -ErrorAction SilentlyContinue)
if (-not $running) {
    Write-Host "kb-service: stopped"
    exit 1
}

Write-Host "kb-service: supervisor pid=$servicePid uvicorn pid=$uvicornPid"
$health = "http://127.0.0.1:$port/api/health"
try {
    $resp = Invoke-RestMethod -Uri $health -TimeoutSec 3
    Write-Host "health: ok store=$($resp.store) llm=$($resp.llm) docs=$($resp.docs) chunks=$($resp.chunks) embed=$($resp.embed_backend)"
    exit 0
} catch {
    Write-Host "health: FAILED ($($_.Exception.Message))"
    exit 2
}
