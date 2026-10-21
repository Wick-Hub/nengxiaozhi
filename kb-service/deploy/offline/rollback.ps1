param(
    [string]$InstallDir = "C:\NengXiaoZhi",
    [string]$ToRelease = "",
    [string]$RestoreDb = "",
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

$root = [IO.Path]::GetFullPath($InstallDir)
Write-Host "==== 能小智离线回滚 ===="
Write-Host "安装目录：$root"

$current = Get-CurrentRelease $root
if (-not $current) { throw "未检测到已安装版本" }
$releases = Get-ReleaseList $root
if ($releases.Count -lt 2 -and -not $ToRelease) {
    throw "没有可回滚的历史版本（releases 目录只有 1 个版本）"
}

$target = $null
if ($ToRelease) {
    if (Test-Path $ToRelease) {
        $target = (Resolve-Path $ToRelease).Path
    } else {
        $candidate = Join-Path (Join-Path $root "releases") $ToRelease
        if (Test-Path $candidate) { $target = (Resolve-Path $candidate).Path }
    }
    if (-not $target) { throw "目标版本不存在：$ToRelease" }
} else {
    $others = @($releases | Where-Object { $_.FullName -ne $current })
    if ($others.Count -eq 0) { throw "没有可回滚的历史版本" }
    $target = $others[$others.Count - 1].FullName
}
$targetName = Split-Path -Leaf $target
if ($target -eq $current) { throw "目标版本与当前版本相同：$targetName" }

Write-Host "当前版本：$(Split-Path -Leaf $current)"
Write-Host "回滚目标：$targetName"
if ($RestoreDb) {
    Write-Host "将恢复数据库：$RestoreDb"
    Write-Warning "数据库恢复会覆盖当前数据；请确认该备份属于目标版本"
}

Stop-KbService -Root $root
if ($RestoreDb) {
    Restore-Database -Release $current -Dump $RestoreDb
}
Set-CurrentRelease -Root $root -Release $target
if (-not $NoStart) {
    Start-KbService -Root $root
}
Write-InstallInfo -Root $root -Release $target -BundleDir $target -Action "rollback"
Write-Host ""
Write-Host "回滚完成：$targetName"
Write-Warning "数据库迁移不回退：新版本新增的表/列会保留，旧版本通常可忽略；如需精确回退请用 -RestoreDb 指定升级前备份。"
