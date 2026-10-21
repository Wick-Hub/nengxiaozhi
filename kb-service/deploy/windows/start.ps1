param(
    [int]$Port = 0
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Error "venv python not found: $python"
    exit 1
}

function Get-EnvPort {
    param([string]$Path)
    if (Test-Path $Path) {
        $m = Select-String -Path $Path -Pattern '^\s*APP_PORT\s*=\s*(\d+)' | Select-Object -First 1
        if ($m) { return [int]$m.Matches[0].Groups[1].Value }
    }
    return 8000
}

if ($Port -le 0) {
    $Port = Get-EnvPort (Join-Path $root ".env")
}

$runDir = Join-Path $root "run"
$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $runDir | Out-Null
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$stopFlag = Join-Path $runDir "stop.flag"
if (Test-Path $stopFlag) { Remove-Item $stopFlag -Force }

$pidFile = Join-Path $runDir "service.pid"
if (Test-Path $pidFile) {
    $oldPid = 0
    [void][int]::TryParse((Get-Content $pidFile -Raw).Trim(), [ref]$oldPid)
    if ($oldPid -gt 0 -and (Get-Process -Id $oldPid -ErrorAction SilentlyContinue)) {
        Write-Host "kb-service already running (supervisor pid=$oldPid)"
        exit 0
    }
}

$supervisor = Join-Path $root "deploy\service_supervisor.py"
$supervisorArgs = @("`"$supervisor`"", "--port", "$Port")
Start-Process -FilePath $python -ArgumentList $supervisorArgs -WorkingDirectory $root -WindowStyle Hidden

$health = "http://127.0.0.1:$Port/api/health"
for ($i = 0; $i -lt 90; $i++) {
    try {
        $resp = Invoke-RestMethod -Uri $health -TimeoutSec 2
        Write-Host "kb-service started: http://127.0.0.1:$Port (store=$($resp.store), llm=$($resp.llm))"
        exit 0
    } catch {
        Start-Sleep -Seconds 1
    }
}

Write-Error "start timeout; see $logDir\service.log"
exit 1
