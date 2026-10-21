param(
    [string]$BundleDir = "",
    [string]$InstallDir = "C:\NengXiaoZhi",
    [string]$PythonExe = "",
    [string]$EnvFile = "",
    [int]$Port = 0,
    [switch]$LinkModels,
    [switch]$SkipDbInit,
    [switch]$NoStart,
    [switch]$AllowPlaceholders
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

if (-not $BundleDir) { $BundleDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }
$BundleDir = (Resolve-Path $BundleDir).Path
$InstallDir = [IO.Path]::GetFullPath($InstallDir)

Write-Host "==== 能小智离线安装 ===="
Write-Host "离线包：$BundleDir"
Write-Host "安装目录：$InstallDir"

Assert-BundleIntegrity $BundleDir
$info = Get-BundleInfo $BundleDir
if (-not $info.includesWheels) { throw "该离线包未包含 wheels，不能离线安装" }

$root = $InstallDir
$shared = Join-Path $root "shared"
$releases = Join-Path $root "releases"
if (Get-CurrentRelease $root) {
    throw "检测到已安装版本；升级请用 upgrade.ps1，回滚请用 rollback.ps1"
}
foreach ($dir in @($root, $shared, $releases, (Join-Path $shared "data"), (Join-Path $shared "uploads"),
                   (Join-Path $shared "logs"), (Join-Path $shared "run"), (Join-Path $shared "knowledge-base"))) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
}

$pyExe = Resolve-PythonExe -PythonExe $PythonExe -TargetVersion $info.targetPython
Write-Host "使用 Python：$pyExe"

# ---- 共享知识库（已有内容不覆盖，保护学校真实文档）----
$sharedKb = Join-Path $shared "knowledge-base"
if (@(Get-ChildItem $sharedKb -Force).Count -eq 0) {
    $payloadKb = Join-Path $BundleDir "payload\knowledge-base"
    if (Test-Path $payloadKb) {
        Write-Host "复制示例知识库到共享目录 …"
        Copy-Item -Path (Join-Path $payloadKb "*") -Destination $sharedKb -Recurse -Force
    }
}

# ---- 共享模型 ----
$payloadModels = Join-Path $BundleDir "payload\models"
$sharedModels = Join-Path $shared "models"
if (Test-Path $payloadModels) {
    if (@(Get-ChildItem $sharedModels -Force -ErrorAction SilentlyContinue).Count -eq 0) {
        if ($LinkModels) {
            Write-Host "链接模型目录（仅同机验收/演示）：$sharedModels"
            New-Item -ItemType Directory -Force -Path $sharedModels | Out-Null
            foreach ($item in Get-ChildItem $payloadModels -Directory) {
                New-Junction -Link (Join-Path $sharedModels $item.Name) -Target $item.FullName
            }
        } else {
            Write-Host "复制模型到共享目录（约 650 MB）…"
            Copy-Item -Path $payloadModels -Destination $shared -Recurse
        }
    }
} else {
    Write-Warning "离线包未包含模型；请在 .env 中把 MODEL_DIR 指向已有模型目录，或重新构建"
    New-Item -ItemType Directory -Force -Path $sharedModels | Out-Null
}

# ---- 生产配置 ----
$envPath = Join-Path $shared ".env"
if (-not (Test-Path $envPath)) {
    if ($EnvFile) {
        if (-not (Test-Path $EnvFile)) { throw "指定的 env 文件不存在：$EnvFile" }
        Copy-Item -Path $EnvFile -Destination $envPath
        Write-Host "使用指定配置：$EnvFile"
    } else {
        $template = Join-Path $BundleDir "payload\kb-service\deploy\env.production.example"
        Copy-Item -Path $template -Destination $envPath
        Write-Host "已生成 $envPath（来自生产模板）"
    }
}
if ($Port -gt 0) { Set-EnvValue -Path $envPath -Key "APP_PORT" -Value "$Port" }
$placeholders = Test-Placeholders $envPath
if ($placeholders.Count -gt 0 -and -not $AllowPlaceholders) {
    throw "生产配置仍有占位符：$($placeholders -join ', ')；请先填写 $envPath（或传入 -EnvFile）"
}
if ($placeholders.Count -gt 0) {
    Write-Warning "配置中仍有占位符（-AllowPlaceholders）：$($placeholders -join ', ')"
}
$store = Get-EnvValue -Path $envPath -Key "KB_STORE" -Default "json"
Write-Host "存储模式：$store"

# ---- 展开 release ----
$release = New-Release -BundleDir $BundleDir -Root $root -PythonExe $pyExe -EnvFile $envPath
Set-CurrentRelease -Root $root -Release $release
Write-InstallInfo -Root $root -Release $release -BundleDir $BundleDir -Action "install"
$appDir = Join-Path $release "kb-service"

# ---- 数据库 ----
if ($store -eq "mysql" -and -not $SkipDbInit) {
    Write-Host "初始化数据库（幂等）…"
    Invoke-DbInit -Release $release -Schema (Join-Path $release "database\schema.sql") -SeedAuth
} elseif ($store -eq "mysql") {
    Write-Warning "已跳过数据库初始化（-SkipDbInit）；部署前请执行 tools/offline_db_init.py"
} else {
    Write-Host "JSON 模式：跳过数据库初始化"
}

# ---- 服务 ----
if (-not $NoStart) {
    Start-KbService -Root $root
}

$port = [int](Get-EnvValue -Path (Join-Path $appDir ".env") -Key "APP_PORT" -Default "8000")
Write-Host ""
Write-Host "安装完成：$release"
Write-Host "当前版本联接：$(Join-Path $root 'current')"
Write-Host "应用地址：http://127.0.0.1:$port/（健康检查 /api/health）"
Write-Host "生产配置：$envPath"
if (-not $NoStart) { Write-Host "服务已启动；停止：current\kb-service\deploy\windows\stop.ps1" }
Write-Host "注册开机自启（管理员 PowerShell）：current\kb-service\deploy\windows\install-service.ps1"
exit 0
