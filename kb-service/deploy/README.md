# 生产部署与 HTTPS（任务 5.1）

目标：`Nginx（443/TLS）→ uvicorn（127.0.0.1:8000）→ MySQL（127.0.0.1:3306）`，
进程异常自动拉起，服务器重启后各组件自动就绪。

```
浏览器 ──HTTPS:443──> Nginx ──HTTP:127.0.0.1:8000──> kb-service(FastAPI)
                                                        │
                                                        └──> MySQL 8 (Docker, 127.0.0.1:3306)
```

## 1. 生产环境变量

```bash
cp deploy/env.production.example .env
```

逐项替换 `__SET_*__`：

- `DB_PASSWORD`：与 `database/docker-compose.yml` 中的 `MYSQL_ROOT_PASSWORD` 一致；
- `LLM_BASE_URL` / `LLM_API_KEY`：校园 AI 网关地址与业务 Key（只配网关，不配厂商）；
  `LLM_PROVIDER=gateway` 保持网关为唯一模型来源；确需离线/校内私有推理时才改 `local`，
  并在 `/integrations` 启用本地 OpenAI 兼容端点（生产默认不启用）；
- `INTEGRATION_*_PROVIDER`：本地模型/MCP/第三方智能体测试默认 `mock`；平台授权与地址
  就绪后再改 `live`，Token 只写服务端 `.env` 或集成中心（落库 AES 加密）；
- `AES_KEY` / `JWT_SECRET`：各生成一份，命令见模板注释；
- `CORS_ALLOW_ORIGINS`：填门户与允许嵌入的第三方站点（含协议，逗号分隔），生产禁止 `*`；
  身份走 `Authorization` 头，`CORS_ALLOW_CREDENTIALS=0`（鉴权开启且来源为 `*` 时启动会告警）；
- 生产必须 `AUTH_ENABLED=true`、`LLM_MOCK=0`、`HF_ENDPOINT=`（断网环境不下载模型）。

`.env` 含密钥：Linux `chmod 600 .env`；Windows 用 `icacls .env /inheritance:r /grant:r "%USERNAME%:R"` 限制读取。

## 2. MySQL：自启与健康检查

`database/docker-compose.yml` 已配置 `restart: unless-stopped`、健康检查与 `127.0.0.1:3306:3306`。

```bash
cd database
docker compose up -d
docker inspect --format '{{.State.Health.Status}}' nengxiaozhi-mysql   # healthy
```

- Linux：`systemctl enable docker`，容器随 Docker 自启；
- Windows：Docker Desktop 设置中勾选开机启动，容器随 Docker 自启。

## 3. TLS 证书

测试/内网先用自签证书跑通链路：

```bash
python deploy/scripts/gen_selfsigned_cert.py \
    --out-dir deploy/certs --hostname kb.example.edu.cn
```

正式交付把学校 CA 签发的证书与私钥命名为 `fullchain.pem` / `privkey.pem` 放入
`deploy/certs/`（私钥权限 600）。证书到期前替换并 reload Nginx。

## 4. Nginx：TLS 终结与反向代理

渲染配置（域名、证书目录、上游按实际替换）：

```bash
python deploy/nginx/render_nginx_conf.py \
    --out /etc/nginx/nginx.conf \
    --server-name kb.example.edu.cn \
    --cert-dir /etc/nginx/certs \
    --upstream 127.0.0.1:8000
```

- Linux：备份原 `/etc/nginx/nginx.conf` 后覆盖，证书放 `/etc/nginx/certs/`；
  `nginx -t && systemctl enable --now nginx`。
- Windows：把渲染结果覆盖到 `C:\nginx\conf\nginx.conf`，证书放 `C:\nginx\conf\certs\`；
  `C:\nginx\nginx.exe -t` 通过后由 `install-service.ps1 -NginxExe C:\nginx\nginx.exe`
  注册开机任务。

配置要点：`proxy_buffering off` + `X-Accel-Buffering: no`（SSE 不攒批）、
读超时 300s、上传 64MB、HTTP 301 到 HTTPS、HSTS/nosniff/Referrer-Policy。

## 5. kb-service 常驻

公共守护进程 `deploy/service_supervisor.py`：uvicorn 退出后 5 秒重启；
健康检查连续失败 3 次时主动重启；`run/stop.flag` 用于可预期停止。

### Windows

```powershell
# 前台联调（隐藏窗口启动 + 健康检查）
powershell -ExecutionPolicy Bypass -File deploy\windows\start.ps1
powershell -ExecutionPolicy Bypass -File deploy\windows\status.ps1
powershell -ExecutionPolicy Bypass -File deploy\windows\stop.ps1

# 注册开机任务（管理员；同时注册 Nginx 与防火墙规则）
powershell -ExecutionPolicy Bypass -File deploy\windows\install-service.ps1 `
    -NginxExe C:\nginx\nginx.exe
```

日志：`logs\service.log`（>10MB 自动轮转为 `.1`），PID：`run\service.pid` / `run\uvicorn.pid`。
计划任务名：`NengXiaoZhi-KBService`、`NengXiaoZhi-Nginx`。

### Linux

```bash
sudo bash deploy/linux/install.sh /opt/nengxiaozhi/kb-service
sudo bash deploy/linux/service.sh status
sudo journalctl -u nengxiaozhi -f
```

systemd 单元：`Restart=always`、`After=docker.service`、`EnvironmentFile=.env`，
并做了 `NoNewPrivileges` / `PrivateTmp` / `ProtectSystem=full` 基础加固。

## 6. 验收

```bash
python tools/verify_deploy.py            # 静态配置 + 证书 + 守护重启 + HTTPS 端到端
python tools/verify_deploy.py --skip-docker   # 无 Docker 时跳过 Nginx 实机段
curl -k https://127.0.0.1/api/health
curl -k -N -X POST https://127.0.0.1/api/chat/stream \
     -H "Content-Type: application/json" -d '{"question":"校园网怎么开户"}'
```

重启验收：重启服务器后确认 MySQL `healthy`、`NengXiaoZhi-KBService` 运行、
`https://<域名>/api/health` 返回 200；随后手动结束 uvicorn 进程，5–10 秒内监督进程应拉起新进程。

## 7. 上线检查清单

- [ ] `.env` 为生产值：`AUTH_ENABLED=true`、`LLM_MOCK=0`、密钥非空、数据库密码已改；
- [ ] MySQL 仅监听 `127.0.0.1`，uvicorn 仅监听 `127.0.0.1`；
- [ ] 443 使用有效证书（自签仅限内网测试）；
- [ ] Nginx 配置 `nginx -t` 通过，SSE 首块不被缓冲；
- [ ] `verify_deploy.py` 全过；5.6 安全加固验收另行执行；
- [ ] 备份 `database` 数据卷、`uploads/`、`.env` 与证书。
