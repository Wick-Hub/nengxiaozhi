# 能小智离线安装 / 升级 / 回滚手册（任务 5.2）

本文档随离线包分发，目标机**全程无需联网**。安装、升级、回滚均采用
「版本目录 + `current` 联接 + `shared` 共享数据」结构，业务数据不随版本覆盖。

## 1. 离线包内容

```
nengxiaozhi-offline-<版本>-<时间戳>/
  payload/kb-service/      应用源码（不含 .venv / 数据 / 日志）
  payload/database/        schema.sql、docker-compose.yml
  payload/knowledge-base/  示例知识库（验收后替换为学校真实文档）
  payload/models/          BGE 嵌入 / 精排模型（约 650 MB）
  payload/wheels/          目标平台全部 Python wheel（含 kb_service wheel）
  payload/docker/          可选：MySQL / Nginx 镜像 tar
  scripts/                 安装 / 升级 / 回滚脚本与运行时依赖清单
  BUILD-INFO.json          版本、目标 Python、模型 / wheel / 镜像清单
  MANIFEST.json            逐文件大小与 SHA-256
  SHA256SUMS               校验清单
  OFFLINE-INSTALL.md       本手册
```

`BUILD-INFO.json` 中的 `targetPython`、`targetPlatform` 必须与目标机一致：
wheel 与平台绑定的（如 `onnxruntime`、`cryptography`）无法跨平台复用。

## 2. 构建离线包（在有网构建机执行）

```bash
cd kb-service
uv run --no-sync python tools/build_offline_bundle.py --out dist/offline --label rc1
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--skip-models` | 不打包模型（仅用于脚本联调，正式交付必须带模型） |
| `--copy-models` | 复制模型而非硬链接（跨盘或需拷贝到 U 盘时使用） |
| `--reuse-wheels DIR` | 复用上次下载的 wheels，避免重复联网 |
| `--skip-wheels` | 不下载 wheel（仅联调，产物不可离线安装） |
| `--platform manylinux2014_x86_64` | 为 Linux 目标机构建（默认 `win_amd64`） |
| `--with-docker-images mysql:8.0 nginx:1.27-alpine` | 同时导出镜像 tar（构建机需有 Docker） |

构建完成后校验：

```bash
cd dist/offline/nengxiaozhi-offline-*
sha256sum -c SHA256SUMS      # Windows: Get-Content SHA256SUMS 后逐行 Get-FileHash
```

## 3. 目标机前置条件

- 64 位操作系统，Python **与包内 `targetPython` 完全一致**（如 3.12.x）；
  `python -m venv` 可用，无需 pip 联网。
- 内存 ≥ 16 GB，磁盘 ≥ 20 GB（应用 + 模型 + 数据余量）。
- MySQL 8.0：可使用包内 `payload/database/docker-compose.yml`（需 Docker），
  或学校既有实例；`KB_STORE=json` 时只需文件系统。
- 端口 `APP_PORT`（默认 8000）未被占用。
- 建议把离线包解压到本地磁盘，不要直接在 U 盘上创建 venv。

## 4. 全新安装（Windows）

管理员 PowerShell（首次安装只需普通权限；注册服务时需要管理员）：

```powershell
cd <离线包>\scripts
.\install.ps1 -InstallDir C:\NengXiaoZhi
```

脚本依次执行：

1. 按 `SHA256SUMS` 校验全部文件；
2. 校验 Python 版本与位数，不匹配立即退出；
3. 把示例知识库、模型放入 `shared`（已有内容不覆盖）；
4. 生成 `shared\.env`（来自 `deploy/env.production.example`），
   检测 `__SET_*__` 占位符，未填写则**拒绝继续**；
5. 展开 release、创建 venv、`pip install --no-index` 安装 wheel；
6. `KB_STORE=mysql` 时执行 `tools/offline_db_init.py`（空库建表 + 幂等迁移）；
7. 启动服务并轮询 `/api/health`。

常用参数：

| 参数 | 说明 |
| --- | --- |
| `-EnvFile D:\secure\prod.env` | 直接使用已填好的生产配置 |
| `-Port 8080` | 覆盖 `APP_PORT` |
| `-LinkModels` | 模型目录改为联接（仅同机验收，不可用于正式交付） |
| `-SkipDbInit` | 跳过数据库初始化（DBA 单独执行 SQL 时使用） |
| `-NoStart` | 只安装不启动（用于批量部署） |
| `-AllowPlaceholders` | 允许占位符继续（仅联调，生产禁用） |

安装后：

```powershell
# 状态 / 停止 / 启动
.\current\kb-service\deploy\windows\status.ps1
.\current\kb-service\deploy\windows\stop.ps1
.\current\kb-service\deploy\windows\start.ps1
# 注册开机自启（管理员）
.\current\kb-service\deploy\windows\install-service.ps1
```

访问 `http://127.0.0.1:<APP_PORT>/`，健康检查 `GET /api/health`。

## 5. 全新安装（Linux）

```bash
cd <离线包>/scripts
chmod +x *.sh
sudo ./install.sh --install-dir /opt/nengxiaozhi
```

脚本行为与 Windows 一致；服务由 `deploy/service_supervisor.py` 以当前用户
后台进程方式启动，日志在 `shared/logs/supervisor.out`。若需 systemd 托管，
把 `current/kb-service` 作为 `WorkingDirectory`、`.venv/bin/python -m uvicorn app.main:app --port <端口>`
作为 `ExecStart` 编写 unit（本包不自动修改 systemd）。

常用参数：`--env-file PATH`、`--port N`、`--skip-db-init`、`--no-start`、
`--allow-placeholders`。

## 6. 生产配置要点

安装只负责生成文件，**不生成密钥**。编辑 `shared/.env` 至少确认：

- `AUTH_ENABLED=true`；`AES_KEY` / `JWT_SECRET` 各自生成且不与测试环境复用；
- `DB_*` 与 MySQL 实例一致；`KB_STORE=mysql`（正式）或 `json`（演示）；
- `LLM_PROVIDER=gateway`，`LLM_BASE_URL` / `LLM_API_KEY` 指向校园 AI 网关；
- `MODEL_DIR` / `KNOWLEDGE_DIR` 由安装脚本改写为安装目录下的绝对路径 / 共享知识库，
  不要手工改回相对路径；
- `HF_ENDPOINT=` 留空，断网环境不触发模型下载。

密钥生成参考 `deploy/env.production.example` 注释。修改 `.env` 后需重启服务；
升级生成的新 release 会重新复制 `shared/.env`。

## 7. 升级

```powershell
cd <新离线包>\scripts
.\upgrade.ps1 -InstallDir C:\NengXiaoZhi
```

流程：停止服务 → 备份 `shared\.env` 与数据库（Docker 容器可用时 `mysqldump`）→
展开新 release 并安装依赖 → 切换 `current` → 执行幂等迁移 → 启动并健康检查。
任一环节失败会**自动切回旧 release 并重启**，备份保留在
`<安装目录>\backups\<时间戳>`。

注意事项：

- 旧 release 保留，回滚不依赖旧离线包；
- 升级前确认新包 `targetPython` 与现有环境一致；
- 数据库迁移只向前执行，升级脚本不会自动降库；
- Linux 使用 `./upgrade.sh --install-dir /opt/nengxiaozhi`。

## 8. 回滚

```powershell
.\rollback.ps1 -InstallDir C:\NengXiaoZhi                  # 回退到上一个版本
.\rollback.ps1 -InstallDir C:\NengXiaoZhi -Release 0.9.0-20261020-101500
.\rollback.ps1 -InstallDir C:\NengXiaoZhi -RestoreDb D:\backup\database.sql
```

回滚只切换代码与依赖，**不会自动回退数据库结构**。若升级已写入新表 / 新列，
需同时用升级时的备份执行 `-RestoreDb`；恢复前请确认该备份时间点可接受。
不指定 `-Release` 时选择除当前版本外最新的 release。

## 9. 验收与自检

- `install.ps1` / `install.sh` 在断网机一次成功，健康检查返回 `ok`；
- 重复执行 `upgrade` → `rollback`，`current` 指向与健康检查均可复现；
- `shared/data`、`shared/uploads`、`shared/logs` 在升级 / 回滚后内容不丢；
- `sha256sum -c SHA256SUMS` 全部通过；
- 示例知识库带「示例数据（待替换为学校真实文件）」标记，正式交付前按 5.5 替换。

本机脚本级回归：`uv run --no-sync python tools/verify_offline_bundle.py`。

## 10. 常见问题

| 现象 | 处理 |
| --- | --- |
| `未找到 Python 3.12 64 位` | 安装对应版本，或 `-PythonExe` / `--python` 指定绝对路径 |
| `pip install` 报 `No matching distribution` | wheel 平台与目标机不符，用对应 `--platform` 重新构建 |
| 安装提示占位符 | 填写 `shared\.env` 中所有 `__SET_*__` 后重跑 |
| 健康检查超时 | 查看 `shared\logs\supervisor.out`；确认端口、`.env`、模型目录 |
| 数据库初始化失败 | 确认 MySQL 可达、账号有建表权限；可 `--dry-run` 先看将要执行的迁移 |
| 升级后异常 | 执行 `rollback.ps1`；数据库如需回退，使用 `backups\<时间戳>\database.sql` |
| `current` 联接失效 | `current-release.txt` 指向的目录被手工删除；用 `rollback.ps1 -Release <存在的版本>` 重建 |
