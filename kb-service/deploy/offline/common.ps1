# 离线交付公共函数（任务 5.2）。由 install/upgrade/rollback 点源引入。
$ErrorActionPreference = "Stop"

function Write-TextUtf8NoBom {
    param([string]$Path, [string]$Text)
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    [IO.File]::WriteAllText($Path, $Text, (New-Object Text.UTF8Encoding($false)))
}

function Read-JsonFile {
    param([string]$Path)
    if (-not (Test-Path $Path)) { throw "文件不存在：$Path" }
    return (Get-Content -Path $Path -Raw -Encoding UTF8 | ConvertFrom-Json)
}

function Get-BundleInfo {
    param([string]$BundleDir)
    return (Read-JsonFile (Join-Path $BundleDir "BUILD-INFO.json"))
}

function Assert-BundleIntegrity {
    param([string]$BundleDir)
    $sums = Join-Path $BundleDir "SHA256SUMS"
    if (-not (Test-Path $sums)) { throw "缺少 SHA256SUMS：$BundleDir" }
    $checked = 0
    foreach ($line in Get-Content -Path $sums -Encoding UTF8) {
        if (-not $line.Trim()) { continue }
        $parts = $line -split '  ', 2
        if ($parts.Count -ne 2) { throw "SHA256SUMS 行格式错误：$line" }
        $expected = $parts[0].Trim().ToLowerInvariant()
        $rel = $parts[1].Trim()
        $file = Join-Path $BundleDir $rel
        if (-not (Test-Path $file)) { throw "离线包文件缺失：$rel" }
        $actual = (Get-FileHash -Path $file -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $expected) { throw "校验失败：$rel" }
        $checked++
    }
    Write-Host "离线包校验通过（$checked 个文件）"
}

function Get-EnvValue {
    param([string]$Path, [string]$Key, [string]$Default = "")
    if (-not (Test-Path $Path)) { return $Default }
    $match = Select-String -Path $Path -Pattern ("^\s*" + [regex]::Escape($Key) + "\s*=\s*(.*)$") |
        Select-Object -First 1
    if (-not $match) { return $Default }
    $value = $match.Matches[0].Groups[1].Value.Trim()
    if (($value.StartsWith('"') -and $value.EndsWith('"')) -or
        ($value.StartsWith("'") -and $value.EndsWith("'"))) {
        $value = $value.Substring(1, $value.Length - 2)
    }
    return $value
}

function Set-EnvValue {
    param([string]$Path, [string]$Key, [string]$Value)
    $lines = @()
    if (Test-Path $Path) { $lines = @(Get-Content -Path $Path -Encoding UTF8) }
    $pattern = "^\s*" + [regex]::Escape($Key) + "\s*="
    $replaced = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match $pattern) {
            $lines[$i] = "$Key=$Value"
            $replaced = $true
            break
        }
    }
    if (-not $replaced) { $lines += "$Key=$Value" }
    Write-TextUtf8NoBom -Path $Path -Text (($lines -join "`r`n") + "`r`n")
}

function Get-PythonVersion {
    param([string]$PythonExe)
    $code = "import sys,struct;print('%d.%d' % sys.version_info[:2]);print(struct.calcsize('P')*8)"
    $raw = & $PythonExe -c $code 2>&1
    if ($LASTEXITCODE -ne 0) { throw "无法运行 Python：$PythonExe`n$raw" }
    $parts = @($raw | Where-Object { $_ -match '\S' })
    return @{ Version = "$($parts[0])".Trim(); Bits = [int]"$($parts[1])".Trim() }
}

function Resolve-PythonExe {
    param([string]$PythonExe, [string]$TargetVersion)
    $candidates = @()
    if ($PythonExe) { $candidates += $PythonExe }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $candidates += $cmd.Source }
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { $candidates += "py:$TargetVersion" }
    foreach ($candidate in $candidates) {
        try {
            if ($candidate -like "py:*") {
                $wanted = $candidate.Split(":")[1]
                $actual = & py "-$wanted" -c "import sys;print(sys.executable)" 2>$null
                if ($LASTEXITCODE -ne 0) { continue }
                $exe = "$actual".Trim()
                $info = Get-PythonVersion $exe
                if ($info.Version -ne $TargetVersion -or $info.Bits -ne 64) { continue }
                return $exe
            }
            $info = Get-PythonVersion $candidate
            if ($info.Version -ne $TargetVersion) {
                Write-Warning "跳过 Python $($info.Version)（离线包目标为 $TargetVersion）：$candidate"
                continue
            }
            if ($info.Bits -ne 64) { Write-Warning "跳过 32 位 Python：$candidate"; continue }
            return $candidate
        } catch {
            continue
        }
    }
    throw "未找到 Python $TargetVersion 64 位。请安装后重试，或用 -PythonExe 指定路径。"
}

function New-Junction {
    param([string]$Link, [string]$Target)
    if (Test-Path $Link) { Remove-Junction $Link }
    $parent = Split-Path -Parent $Link
    if ($parent -and -not (Test-Path $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
    & cmd /c mklink /J "`"$Link`"" "`"$Target`"" | Out-Null
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $Link)) { throw "创建目录联接失败：$Link -> $Target" }
}

function Remove-Junction {
    param([string]$Path)
    if (-not (Test-Path $Path)) { return }
    $item = Get-Item $Path -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        & cmd /c rmdir "`"$Path`"" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "删除目录联接失败：$Path" }
    } else {
        throw "$Path 不是目录联接，拒绝删除（避免误删数据）"
    }
}

function Assert-EmptyOrMissing {
    param([string]$Path)
    if (Test-Path $Path) {
        $items = @(Get-ChildItem -Path $Path -Force)
        if ($items.Count -gt 0) { throw "目录非空：$Path（请换一个 -InstallDir 或先备份/清理）" }
    }
}

function Get-CurrentRelease {
    param([string]$Root)
    $marker = Join-Path $Root "current-release.txt"
    if (-not (Test-Path $marker)) { return "" }
    $name = (Get-Content -Path $marker -Raw -Encoding UTF8).Trim()
    if (-not $name) { return "" }
    $path = Join-Path (Join-Path $Root "releases") $name
    if (-not (Test-Path $path)) { throw "当前版本目录不存在：$path" }
    return $path
}

function Set-CurrentRelease {
    param([string]$Root, [string]$Release)
    $name = Split-Path -Leaf $Release
    Write-TextUtf8NoBom -Path (Join-Path $Root "current-release.txt") -Text ($name + "`r`n")
    New-Junction -Link (Join-Path $Root "current") -Target $Release
}

function Get-ReleaseList {
    param([string]$Root)
    $dir = Join-Path $Root "releases"
    if (-not (Test-Path $dir)) { return @() }
    return @(Get-ChildItem -Path $dir -Directory | Sort-Object Name)
}

function New-Release {
    param(
        [string]$BundleDir,
        [string]$Root,
        [string]$PythonExe,
        [string]$EnvFile,
        [string]$ReleaseName = ""
    )
    $info = Get-BundleInfo $BundleDir
    $payload = Join-Path $BundleDir "payload"
    if (-not (Test-Path (Join-Path $payload "kb-service"))) { throw "离线包缺少 payload/kb-service" }
    if (-not (Test-Path (Join-Path $payload "wheels"))) { throw "离线包缺少 wheels；无法离线安装依赖" }
    if (-not $ReleaseName) {
        $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
        $ReleaseName = "$($info.version)-$stamp"
    }
    $release = Join-Path (Join-Path $Root "releases") $ReleaseName
    if (Test-Path $release) { throw "版本目录已存在：$release" }
    New-Item -ItemType Directory -Force -Path $release | Out-Null

    Write-Host "展开应用文件到 $release …"
    Copy-Item -Path (Join-Path $payload "kb-service") -Destination $release -Recurse
    if (Test-Path (Join-Path $payload "database")) {
        Copy-Item -Path (Join-Path $payload "database") -Destination $release -Recurse
    }
    Copy-Item -Path (Join-Path $BundleDir "BUILD-INFO.json") -Destination $release

    $appDir = Join-Path $release "kb-service"
    $shared = Join-Path $Root "shared"
    foreach ($name in @("data", "uploads", "logs", "run")) {
        $target = Join-Path $shared $name
        if (-not (Test-Path $target)) { New-Item -ItemType Directory -Force -Path $target | Out-Null }
        New-Junction -Link (Join-Path $appDir $name) -Target $target
    }
    $models = Join-Path $shared "models"
    if (Test-Path $models) {
        New-Junction -Link (Join-Path $appDir "models") -Target $models
    }
    $kb = Join-Path $shared "knowledge-base"
    if (Test-Path $kb) {
        New-Junction -Link (Join-Path $release "knowledge-base") -Target $kb
    }

    Write-Host "创建 venv …"
    $venv = Join-Path $appDir ".venv"
    & $PythonExe -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "创建 venv 失败" }
    $venvPython = Join-Path $venv "Scripts\python.exe"
    if (-not (Test-Path $venvPython)) { throw "venv Python 不存在：$venvPython" }

    $wheels = Join-Path $payload "wheels"
    $requirements = Join-Path $BundleDir "scripts\requirements-runtime.txt"
    if (-not (Test-Path $requirements)) { $requirements = Join-Path $appDir "deploy\offline\requirements-runtime.txt" }
    Write-Host "离线安装依赖（--no-index）…"
    & $venvPython -m pip install --no-index --find-links $wheels --upgrade pip setuptools wheel | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "离线安装 pip/setuptools/wheel 失败" }
    & $venvPython -m pip install --no-index --find-links $wheels -r $requirements | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "离线安装运行时依赖失败" }
    $projectWheel = Get-ChildItem -Path $wheels -Filter "kb_service-*.whl" | Select-Object -First 1
    if (-not $projectWheel) { throw "wheels 中缺少 kb_service-*.whl" }
    & $venvPython -m pip install --no-index --no-deps $projectWheel.FullName | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "离线安装应用 wheel 失败" }

    $envSource = $EnvFile
    if (-not $envSource) { $envSource = Join-Path $shared ".env" }
    if (-not (Test-Path $envSource)) {
        $template = Join-Path $appDir "deploy\env.production.example"
        if (-not (Test-Path $template)) { throw "缺少生产 env 模板：$template" }
        $envSource = Join-Path $shared ".env"
        Copy-Item -Path $template -Destination $envSource
    }
    Copy-Item -Path $envSource -Destination (Join-Path $appDir ".env") -Force
    Set-EnvValue -Path (Join-Path $appDir ".env") -Key "MODEL_DIR" -Value $models
    Set-EnvValue -Path (Join-Path $appDir ".env") -Key "KNOWLEDGE_DIR" -Value "../knowledge-base"
    Set-EnvValue -Path (Join-Path $appDir ".env") -Key "UPLOAD_DIR" -Value "./uploads"
    Set-EnvValue -Path (Join-Path $appDir ".env") -Key "STORE_PATH" -Value "./data/store.json"

    Write-Host "校验安装结果 …"
    Push-Location $appDir
    try {
        & $venvPython -c "import app.main; print('import-ok')" | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "安装后导入 app.main 失败" }
    } finally {
        Pop-Location
    }
    return $release
}

function Test-Placeholders {
    param([string]$EnvPath)
    $text = Get-Content -Path $EnvPath -Raw -Encoding UTF8
    $missing = @()
    foreach ($m in [regex]::Matches($text, "__SET_[A-Z0-9_]+__")) {
        if ($missing -notcontains $m.Value) { $missing += $m.Value }
    }
    return $missing
}

function Invoke-DbInit {
    param([string]$Release, [string]$Schema = "", [switch]$SeedAuth)
    $appDir = Join-Path $Release "kb-service"
    $store = Get-EnvValue -Path (Join-Path $appDir ".env") -Key "KB_STORE" -Default "json"
    if ($store -ne "mysql") {
        Write-Host "JSON 模式：跳过数据库初始化"
        return
    }
    $venvPython = Join-Path $appDir ".venv\Scripts\python.exe"
    $cliArgs = @("tools\offline_db_init.py")
    if ($Schema) { $cliArgs += @("--schema", $Schema) }
    if ($SeedAuth) { $cliArgs += "--seed-auth" }
    Push-Location $appDir
    try {
        & $venvPython @cliArgs
        if ($LASTEXITCODE -ne 0) { throw "数据库初始化失败" }
    } finally {
        Pop-Location
    }
}

function Backup-Database {
    param([string]$Release, [string]$BackupDir)
    $appDir = Join-Path $Release "kb-service"
    $envPath = Join-Path $appDir ".env"
    $store = Get-EnvValue -Path $envPath -Key "KB_STORE" -Default "json"
    if ($store -ne "mysql") { return "" }
    $db = Get-EnvValue -Path $envPath -Key "DB_NAME" -Default "nengxiaozhi"
    $user = Get-EnvValue -Path $envPath -Key "DB_USER" -Default "root"
    $pass = Get-EnvValue -Path $envPath -Key "DB_PASSWORD"
    $dump = Join-Path $BackupDir "database.sql"
    $container = "nengxiaozhi-mysql"
    $docker = Get-Command docker -ErrorAction SilentlyContinue
    if (-not $docker) { Write-Warning "未找到 docker，跳过数据库备份（请自行 mysqldump）"; return "" }
    $running = & docker ps --filter "name=$container" --format "{{.Names}}" 2>$null
    if ("$running".Trim() -ne $container) {
        Write-Warning "容器 $container 未运行，跳过数据库备份"
        return ""
    }
    Write-Host "备份数据库到 $dump …"
    $oldPassword = $env:MYSQL_PWD
    $env:MYSQL_PWD = $pass
    try {
        $cmd = "docker exec -e MYSQL_PWD $container mysqldump -u$user --single-transaction --routines --databases $db > `"$dump`""
        & cmd /c $cmd
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path $dump)) { throw "数据库备份失败" }
    } finally {
        $env:MYSQL_PWD = $oldPassword
    }
    return $dump
}

function Restore-Database {
    param([string]$Release, [string]$Dump)
    if (-not (Test-Path $Dump)) { throw "备份文件不存在：$Dump" }
    $appDir = Join-Path $Release "kb-service"
    $envPath = Join-Path $appDir ".env"
    $db = Get-EnvValue -Path $envPath -Key "DB_NAME" -Default "nengxiaozhi"
    $user = Get-EnvValue -Path $envPath -Key "DB_USER" -Default "root"
    $pass = Get-EnvValue -Path $envPath -Key "DB_PASSWORD"
    $container = "nengxiaozhi-mysql"
    Write-Host "恢复数据库 $db（$Dump）…"
    $oldPassword = $env:MYSQL_PWD
    $env:MYSQL_PWD = $pass
    try {
        $cmd = "docker exec -i -e MYSQL_PWD $container mysql -u$user $db < `"$Dump`""
        & cmd /c $cmd
        if ($LASTEXITCODE -ne 0) { throw "数据库恢复失败" }
    } finally {
        $env:MYSQL_PWD = $oldPassword
    }
}

function Get-ServiceState {
    param([string]$Root)
    $release = Get-CurrentRelease $Root
    if (-not $release) { return @{ Running = $false; Port = 0 } }
    $appDir = Join-Path $release "kb-service"
    $port = [int](Get-EnvValue -Path (Join-Path $appDir ".env") -Key "APP_PORT" -Default "8000")
    $pidFile = Join-Path $appDir "run\service.pid"
    $running = $false
    if (Test-Path $pidFile) {
        $supervisorPid = 0
        [void][int]::TryParse((Get-Content $pidFile -Raw).Trim(), [ref]$supervisorPid)
        if ($supervisorPid -gt 0 -and (Get-Process -Id $supervisorPid -ErrorAction SilentlyContinue)) {
            $running = $true
        }
    }
    return @{ Running = $running; Port = $port; AppDir = $appDir }
}

function Start-KbService {
    param([string]$Root)
    $release = Get-CurrentRelease $Root
    if (-not $release) { throw "尚未安装任何版本" }
    $script = Join-Path $release "kb-service\deploy\windows\start.ps1"
    if (-not (Test-Path $script)) { throw "缺少启动脚本：$script" }
    & $script
    if ($LASTEXITCODE -ne 0) { throw "服务启动失败" }
    $state = Get-ServiceState $Root
    $health = "http://127.0.0.1:$($state.Port)/api/health"
    for ($i = 0; $i -lt 30; $i++) {
        try {
            $resp = Invoke-RestMethod -Uri $health -TimeoutSec 2
            Write-Host "健康检查通过：$health（store=$($resp.store), llm=$($resp.llm)）"
            return
        } catch {
            Start-Sleep -Seconds 1
        }
    }
    throw "健康检查超时：$health"
}

function Stop-KbService {
    param([string]$Root)
    $state = Get-ServiceState $Root
    if (-not $state.Running) {
        Write-Host "服务未运行，跳过停止"
        return
    }
    $script = Join-Path $state.AppDir "deploy\windows\stop.ps1"
    if (Test-Path $script) {
        & $script
    } else {
        $flag = Join-Path $state.AppDir "run\stop.flag"
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $flag) | Out-Null
        Write-TextUtf8NoBom -Path $flag -Text "stop`r`n"
    }
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 500
        $now = Get-ServiceState $Root
        if (-not $now.Running) { Write-Host "服务已停止"; return }
    }
    throw "服务停止超时；请检查 run\service.pid"
}

function Write-InstallInfo {
    param([string]$Root, [string]$Release, [string]$BundleDir, [string]$Action)
    $path = Join-Path $Root "INSTALL-INFO.json"
    $history = @()
    if (Test-Path $path) {
        try { $history = @((Read-JsonFile $path).history) } catch { $history = @() }
    }
    $entry = [ordered]@{
        action = $Action
        release = (Split-Path -Leaf $Release)
        bundleVersion = (Get-BundleInfo $BundleDir).version
        at = (Get-Date).ToString("o")
    }
    $history += $entry
    $payload = [ordered]@{
        product = "能小智校园 AI 助手平台"
        installDir = $Root
        current = (Split-Path -Leaf $Release)
        updatedAt = $entry.at
        history = $history
    }
    Write-TextUtf8NoBom -Path $path -Text ($payload | ConvertTo-Json -Depth 6)
}
