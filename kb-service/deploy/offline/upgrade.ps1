param(
    [string]$BundleDir = "",
    [string]$InstallDir = "C:\NengXiaoZhi",
    [string]$PythonExe = "",
    [switch]$SkipDbBackup,
    [switch]$SkipDbMigrate,
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

if (-not $BundleDir) { $BundleDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }
$BundleDir = (Resolve-Path $BundleDir).Path
$root = [IO.Path]::GetFullPath($InstallDir)

Write-Host "==== 能小智离线升级 ===="
Write-Host "离线包：$BundleDir"
Write-Host "安装目录：$root"

$oldRelease = Get-CurrentRelease $root
if (-not $oldRelease) { throw "未检测到已安装版本；首次安装请用 install.ps1" }
Assert-BundleIntegrity $BundleDir
$info = Get-BundleInfo $BundleDir
if (-not $info.includesWheels) { throw "该离线包未包含 wheels，不能离线升级" }
$pyExe = Resolve-PythonExe -PythonExe $PythonExe -TargetVersion $info.targetPython
Write-Host "当前版本：$(Split-Path -Leaf $oldRelease)"
Write-Host "目标版本：$($info.version)（Python $($info.targetPython)）"

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backup = Join-Path (Join-Path $root "backups") $stamp
New-Item -ItemType Directory -Force -Path $backup | Out-Null
$sharedEnv = Join-Path (Join-Path $root "shared") ".env"
Copy-Item -Path $sharedEnv -Destination (Join-Path $backup ".env") -Force
Write-TextUtf8NoBom -Path (Join-Path $backup "previous-release.txt") -Text ((Split-Path -Leaf $oldRelease) + "`r`n")

Stop-KbService -Root $root

$dump = ""
if (-not $SkipDbBackup) {
    $dump = Backup-Database -Release $oldRelease -BackupDir $backup
}
Write-TextUtf8NoBom -Path (Join-Path $backup "BACKUP-INFO.json") -Text (@{
    at = (Get-Date).ToString("o")
    previousRelease = (Split-Path -Leaf $oldRelease)
    databaseDump = $dump
} | ConvertTo-Json)

$newRelease = ""
try {
    $newRelease = New-Release -BundleDir $BundleDir -Root $root -PythonExe $pyExe -EnvFile $sharedEnv
    Set-CurrentRelease -Root $root -Release $newRelease
    if (-not $SkipDbMigrate) {
        Write-Host "执行增量迁移（不写入演示数据）…"
        Invoke-DbInit -Release $newRelease
    } else {
        Write-Warning "已跳过数据库迁移（-SkipDbMigrate）"
    }
    if (-not $NoStart) {
        Start-KbService -Root $root
    }
} catch {
    $reason = $_.Exception.Message
    Write-Warning "升级失败：$reason"
    if ($newRelease) {
        try { Stop-KbService -Root $root } catch { }
    }
    Write-Warning "自动回滚到 $(Split-Path -Leaf $oldRelease) …"
    Set-CurrentRelease -Root $root -Release $oldRelease
    if (-not $NoStart) {
        try { Start-KbService -Root $root } catch {
            Write-Error "回滚后服务启动失败：$($_.Exception.Message)"
        }
    }
    throw "升级失败，已回滚到旧版本。备份目录：$backup"
}

Write-InstallInfo -Root $root -Release $newRelease -BundleDir $BundleDir -Action "upgrade"
Write-Host ""
Write-Host "升级完成：$(Split-Path -Leaf $newRelease)"
Write-Host "旧版本保留在 releases\$(Split-Path -Leaf $oldRelease)"
Write-Host "备份目录：$backup"
if ($dump) { Write-Host "数据库备份：$dump" }
Write-Host "如需回滚：scripts\rollback.ps1 -InstallDir `"$root`""
exit 0
