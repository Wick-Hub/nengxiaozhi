param(
    [int]$TimeoutSeconds = 30
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$runDir = Join-Path $root "run"
New-Item -ItemType Directory -Force -Path $runDir | Out-Null

$flag = Join-Path $runDir "stop.flag"
Set-Content -Path $flag -Value "stop $(Get-Date -Format o)" -Encoding ascii

$pidFile = Join-Path $runDir "service.pid"
if (-not (Test-Path $pidFile)) {
    Write-Host "kb-service is not running (no service.pid)"
    exit 0
}

$servicePid = 0
[void][int]::TryParse((Get-Content $pidFile -Raw).Trim(), [ref]$servicePid)
if ($servicePid -le 0 -or -not (Get-Process -Id $servicePid -ErrorAction SilentlyContinue)) {
    Write-Host "kb-service is not running (stale pid)"
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    exit 0
}

for ($i = 0; $i -lt $TimeoutSeconds; $i++) {
    if (-not (Get-Process -Id $servicePid -ErrorAction SilentlyContinue)) {
        Write-Host "kb-service stopped"
        exit 0
    }
    Start-Sleep -Seconds 1
}

Write-Host "graceful stop timed out; killing process tree pid=$servicePid"
& taskkill.exe /PID $servicePid /T /F | Out-Null
Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $runDir "uvicorn.pid") -Force -ErrorAction SilentlyContinue
Write-Host "kb-service killed"
