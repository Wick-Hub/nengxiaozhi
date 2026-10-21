param(
    [switch]$KeepFirewallRules
)

$ErrorActionPreference = "Stop"
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "run this script from an elevated PowerShell (Administrator)"
    exit 1
}

foreach ($taskName in @("NengXiaoZhi-KBService", "NengXiaoZhi-Nginx")) {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($task) {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Host "scheduled task removed: $taskName"
    }
}

& (Join-Path $PSScriptRoot "stop.ps1") | Out-Null

if (-not $KeepFirewallRules) {
    foreach ($ruleName in @("NengXiaoZhi HTTP", "NengXiaoZhi HTTPS")) {
        $rule = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
        if ($rule) {
            Remove-NetFirewallRule -DisplayName $ruleName
            Write-Host "firewall rule removed: $ruleName"
        }
    }
}

Write-Host "uninstall complete; data, logs and certificates were kept"
