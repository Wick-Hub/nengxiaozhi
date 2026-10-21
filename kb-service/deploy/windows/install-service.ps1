param(
    [int]$Port = 0,
    [string]$NginxExe = "",
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "run this script from an elevated PowerShell (Administrator)"
    exit 1
}

$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Error "venv python not found: $python"
    exit 1
}

$envFile = Join-Path $root ".env"
if (-not (Test-Path $envFile)) {
    Write-Error "production .env not found; copy deploy\env.production.example first"
    exit 1
}
$authLine = Select-String -Path $envFile -Pattern '^\s*AUTH_ENABLED\s*=\s*(.+)$' | Select-Object -First 1
if (-not $authLine -or $authLine.Matches[0].Groups[1].Value.Trim() -notmatch '^(1|true|yes|on)$') {
    Write-Warning "AUTH_ENABLED is not enabled in .env; production must use AUTH_ENABLED=true"
}

if ($Port -le 0) {
    $m = Select-String -Path $envFile -Pattern '^\s*APP_PORT\s*=\s*(\d+)' | Select-Object -First 1
    if ($m) { $Port = [int]$m.Matches[0].Groups[1].Value } else { $Port = 8000 }
}

$supervisor = Join-Path $root "deploy\service_supervisor.py"
$taskName = "NengXiaoZhi-KBService"

$action = New-ScheduledTaskAction -Execute $python `
    -Argument "`"$supervisor`" --port $Port" `
    -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
$runAs = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $runAs -Force `
    -Description "NengXiaoZhi kb-service (uvicorn + auto restart)" | Out-Null
Write-Host "scheduled task registered: $taskName (AtStartup, SYSTEM)"

foreach ($ruleName in @("NengXiaoZhi HTTP", "NengXiaoZhi HTTPS")) {
    if (-not (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue)) {
        $fwPort = if ($ruleName -like "*HTTPS*") { 443 } else { 80 }
        New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Protocol TCP `
            -LocalPort $fwPort -Action Allow | Out-Null
        Write-Host "firewall rule added: $ruleName (TCP $fwPort)"
    }
}

if ($NginxExe) {
    if (-not (Test-Path $NginxExe)) {
        Write-Error "Nginx executable not found: $NginxExe"
        exit 1
    }
    $nginxDir = Split-Path $NginxExe -Parent
    $nginxTask = "NengXiaoZhi-Nginx"
    $nginxAction = New-ScheduledTaskAction -Execute $NginxExe -WorkingDirectory $nginxDir
    $nginxTrigger = New-ScheduledTaskTrigger -AtStartup
    $nginxSettings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName $nginxTask -Action $nginxAction -Trigger $nginxTrigger `
        -Settings $nginxSettings -Principal $runAs -Force `
        -Description "NengXiaoZhi Nginx TLS reverse proxy" | Out-Null
    Write-Host "scheduled task registered: $nginxTask (AtStartup, SYSTEM)"
}

if (-not $NoStart) {
    Start-ScheduledTask -TaskName $taskName
    if ($NginxExe) { Start-ScheduledTask -TaskName "NengXiaoZhi-Nginx" }
    Start-Sleep -Seconds 3
    for ($i = 0; $i -lt 60; $i++) {
        try {
            $resp = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 2
            Write-Host "service healthy: store=$($resp.store) llm=$($resp.llm)"
            exit 0
        } catch {
            Start-Sleep -Seconds 1
        }
    }
    Write-Warning "task started but health check did not pass within 60s; see logs\service.log"
    exit 2
}
