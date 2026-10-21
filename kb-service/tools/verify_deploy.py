"""生产部署验收（任务 5.1）。

覆盖四层：

1. **配置静态检查**：Nginx TLS/SSE、生产 env、MySQL 健康检查与回环绑定、
   systemd 单元、Windows/Linux 启停脚本；
2. **证书链路**：自签证书可生成，SAN 正确，私钥与证书匹配；
3. **守护重启**：启动 ``deploy/service_supervisor.py``，强杀 uvicorn 后应自动拉起；
4. **HTTPS 端到端**：用 Docker Nginx + 自签证书反代真实 uvicorn，
   验证健康检查、HSTS、HTTP→HTTPS 跳转与 SSE 流式响应不被攒批。

用法::

    python tools/verify_deploy.py
    python tools/verify_deploy.py --skip-docker     # 无 Docker 时跳过第 4 段

退出码：全部通过（或仅跳过）为 0，存在 FAIL 为 1。
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

try:  # pragma: no cover
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

PASSED = 0
FAILED = 0
SKIPPED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
    else:
        FAILED += 1
    tail = f"  [{detail}]" if detail else ""
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{tail}")


def skip(name: str, reason: str) -> None:
    global SKIPPED
    SKIPPED += 1
    print(f"  [SKIP] {name}  [{reason}]")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def _read(rel: str) -> str:
    path = SERVICE_DIR / rel
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _http_ok(url: str, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            return 200 <= resp.status < 300
    except Exception:  # noqa: BLE001
        return False


def _wait_http(port: int, path: str = "/api/health", timeout: float = 60.0) -> bool:
    url = f"http://127.0.0.1:{port}{path}"
    end = time.time() + timeout
    while time.time() < end:
        if _http_ok(url):
            return True
        time.sleep(0.5)
    return False


def _run(cmd: list[str], cwd: Path | None = None, timeout: float = 60.0,
         env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout, env=env,
    )


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        proc = _run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], timeout=10)
        return str(pid) in proc.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _parse_env(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def static_checks() -> None:
    section("1. 部署资产与关键配置")
    required = [
        "deploy/README.md",
        "deploy/service_supervisor.py",
        "deploy/env.production.example",
        "deploy/nginx/nengxiaozhi.conf.template",
        "deploy/nginx/render_nginx_conf.py",
        "deploy/scripts/gen_selfsigned_cert.py",
        "deploy/linux/nengxiaozhi.service",
        "deploy/linux/install.sh",
        "deploy/linux/service.sh",
        "deploy/windows/start.ps1",
        "deploy/windows/stop.ps1",
        "deploy/windows/status.ps1",
        "deploy/windows/install-service.ps1",
        "deploy/windows/uninstall-service.ps1",
    ]
    missing = [p for p in required if not (SERVICE_DIR / p).is_file()]
    check("部署资产齐全（14 个文件）", not missing, ",".join(missing) or "ok")

    nginx = _read("deploy/nginx/nengxiaozhi.conf.template")
    nginx_rules = {
        "HTTPS 监听": "listen __HTTPS_PORT__ ssl http2;",
        "TLS 1.2/1.3": "ssl_protocols TLSv1.2 TLSv1.3;",
        "SSE 不缓冲": "proxy_buffering off;",
        "SSE 响应头": "X-Accel-Buffering no",
        "长超时": "proxy_read_timeout 300s;",
        "上传上限": "client_max_body_size 64m;",
        "HTTP 跳转": "return 301 https://",
        "保留端口": "proxy_set_header Host $http_host;",
        "HSTS": "Strict-Transport-Security",
        "隐藏版本": "server_tokens off;",
    }
    for name, token in nginx_rules.items():
        check(f"Nginx：{name}", token in nginx)

    env = _parse_env(_read("deploy/env.production.example"))
    check("生产 env：MySQL 模式", env.get("KB_STORE") == "mysql")
    check("生产 env：鉴权开启", env.get("AUTH_ENABLED", "").lower() in {"true", "1"})
    check("生产 env：不使用 mock", env.get("LLM_MOCK") == "0")
    check("生产 env：AES/JWT 为占位而非空值",
          env.get("AES_KEY", "").startswith("__SET_") and env.get("JWT_SECRET", "").startswith("__SET_"))
    check("生产 env：数据库监听回环", env.get("DB_HOST") == "127.0.0.1")
    check("生产 env：模型下载关闭", env.get("HF_ENDPOINT") == "")

    compose = (SERVICE_DIR.parent / "database" / "docker-compose.yml").read_text(encoding="utf-8")
    check("MySQL：自动重启", "restart: unless-stopped" in compose)
    check("MySQL：健康检查", "healthcheck:" in compose and "mysqladmin ping" in compose)
    check("MySQL：只绑定 127.0.0.1", "127.0.0.1:3306:3306" in compose)

    unit = _read("deploy/linux/nengxiaozhi.service")
    check("systemd：异常自动重启", "Restart=always" in unit)
    check("systemd：读取生产 env", "EnvironmentFile=" in unit)
    check("systemd：等待 Docker", "After=network-online.target docker.service" in unit)
    check("systemd：uvicorn 只监听回环", "--host 127.0.0.1" in unit)

    supervisor = _read("deploy/service_supervisor.py")
    check("守护：退出后重启", "restart-delay" in supervisor and "uvicorn 退出" in supervisor)
    check("守护：健康检查假死重启", "health-failures" in supervisor and "_http_ok" in supervisor)
    check("守护：可预期停止", "stop.flag" in supervisor)

    win_start = _read("deploy/windows/start.ps1")
    win_install = _read("deploy/windows/install-service.ps1")
    check("Windows：启动脚本等待健康检查", "api/health" in win_start and "service_supervisor.py" in win_start)
    check("Windows：开机计划任务", "New-ScheduledTaskTrigger -AtStartup" in win_install)
    check("Windows：注册 Nginx 任务", "NengXiaoZhi-Nginx" in win_install)

    bash_ok = True
    bash_detail = ""
    bash = shutil.which("bash")
    for script in ("deploy/linux/install.sh", "deploy/linux/service.sh"):
        if not bash:
            skip(f"Shell 语法：{script}", "未找到 bash")
            continue
        proc = _run([bash, "-n", script], cwd=SERVICE_DIR)
        if proc.returncode != 0:
            bash_ok = False
            bash_detail = proc.stderr[:160]
    check("Linux 脚本语法通过", bash_ok, bash_detail or "ok")

    powershell = shutil.which("powershell")
    if not powershell:
        skip("PowerShell 脚本语法", "未找到 powershell")
    else:
        bad = []
        for script in sorted((SERVICE_DIR / "deploy" / "windows").glob("*.ps1")):
            cmd = (
                "$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile("
                f"'{script}',[ref]$null,[ref]$e); if($e){{$e | ForEach-Object {{$_.Message}}; exit 1}}"
            )
            proc = _run([powershell, "-NoProfile", "-NonInteractive", "-Command", cmd], timeout=30)
            if proc.returncode != 0:
                bad.append(script.name)
        check("Windows 脚本语法通过（5 个）", not bad, ",".join(bad) or "ok")

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "nginx.conf"
        render = _run(
            [sys.executable, "deploy/nginx/render_nginx_conf.py", "--out", str(out),
             "--server-name", "kb.example.edu.cn", "--cert-dir", str(Path(tmp) / "certs"),
             "--upstream", "127.0.0.1:8000"],
            cwd=SERVICE_DIR,
        )
        check("Nginx 配置可渲染", render.returncode == 0, render.stderr[-160:] or "ok")
        rendered = out.read_text(encoding="utf-8") if out.is_file() else ""
        check("渲染后无未替换占位符", "__" not in rendered.replace("__SET_", ""))
        check("渲染结果包含域名与上游",
              "kb.example.edu.cn" in rendered and "127.0.0.1:8000" in rendered)


def cert_checks(tmp: Path) -> tuple[Path, Path]:
    section("2. 自签证书")
    certs = tmp / "certs"
    proc = _run(
        [sys.executable, "deploy/scripts/gen_selfsigned_cert.py",
         "--out-dir", str(certs), "--hostname", "localhost,kb.example.edu.cn"],
        cwd=SERVICE_DIR,
    )
    check("证书生成命令成功", proc.returncode == 0, proc.stderr[-160:] or "ok")
    cert_path = certs / "fullchain.pem"
    key_path = certs / "privkey.pem"
    check("证书与私钥文件存在", cert_path.is_file() and key_path.is_file())

    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization

        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        dns = set(san.get_values_for_type(x509.DNSName))
        ips = {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
        check("SAN 含域名与回环地址",
              {"localhost", "kb.example.edu.cn"} <= dns and "127.0.0.1" in ips,
              f"dns={sorted(dns)} ips={sorted(ips)}")
        same_key = (
            cert.public_key().public_numbers() == key.public_key().public_numbers()  # type: ignore[attr-defined]
        )
        check("证书与私钥匹配", same_key)
        days = (cert.not_valid_after_utc - cert.not_valid_before_utc).days
        check("有效期合理（30–500 天）", 30 <= days <= 500, f"{days} 天")
        check("私钥未加密（供 Nginx 直接加载）", b"ENCRYPTED" not in key_path.read_bytes())
    except Exception as exc:  # noqa: BLE001
        check("证书解析", False, str(exc)[:160])
    return cert_path, key_path


def supervisor_checks(tmp: Path) -> None:
    section("3. 守护进程自动拉起")
    port = _free_port()
    run_dir = tmp / "sup-run"
    log_dir = tmp / "sup-logs"
    env = dict(os.environ)
    env.update(
        KB_STORE="json",
        LLM_MOCK="1",
        AUTH_ENABLED="false",
        RATE_LIMIT_ENABLED="false",
    )
    log_fh = open(tmp / "supervisor.out", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "deploy/service_supervisor.py",
         "--port", str(port), "--run-dir", str(run_dir), "--log-dir", str(log_dir),
         "--restart-delay", "1"],
        cwd=str(SERVICE_DIR), env=env, stdout=log_fh, stderr=subprocess.STDOUT,
    )
    try:
        up = _wait_http(port, timeout=75)
        check("守护启动 uvicorn 并通过健康检查", up, f"port={port}")
        time.sleep(1)
        pid_file = run_dir / "uvicorn.pid"
        pid1 = int(pid_file.read_text().strip()) if pid_file.is_file() else 0
        check("记录 uvicorn PID", pid1 > 0, str(pid1))
        sup_file = run_dir / "service.pid"
        sup_pid = int(sup_file.read_text().strip()) if sup_file.is_file() else 0
        # Windows 下 uv 的 python.exe 可能是 trampoline，文件里是真实守护进程 PID，
        # 与 Popen 拿到的启动器 PID 不一定相同；以「文件 PID 存活 + 日志自报」为准。
        check("记录守护 PID", _pid_alive(sup_pid), f"pid={sup_pid} launcher={proc.pid}")

        if pid1 > 0:
            if os.name == "nt":
                _run(["taskkill", "/PID", str(pid1), "/F"], timeout=15)
            else:
                os.kill(pid1, 15)

            waited = time.time()
            pid2 = 0
            while time.time() - waited < 75:
                try:
                    pid2 = int(pid_file.read_text().strip())
                except (OSError, ValueError):
                    pid2 = 0
                if pid2 and pid2 != pid1 and _http_ok(f"http://127.0.0.1:{port}/api/health"):
                    break
                time.sleep(0.5)
            check("强杀 uvicorn 后自动拉起新进程", pid2 not in (0, pid1), f"{pid1} → {pid2 or '-'}")
            check("重启后健康检查恢复", _http_ok(f"http://127.0.0.1:{port}/api/health"))
        else:
            check("强杀 uvicorn 后自动拉起新进程", False, "无初始 PID")
            check("重启后健康检查恢复", False, "无初始 PID")

        (run_dir / "stop.flag").write_text("verify stop", encoding="ascii")
        try:
            code = proc.wait(timeout=25)
        except subprocess.TimeoutExpired:
            code = -1
            proc.terminate()
        check("stop.flag 可正常停止守护", code == 0, f"exit={code}")
        log_text = (log_dir / "service.log").read_text(encoding="utf-8", errors="replace") \
            if (log_dir / "service.log").is_file() else ""
        check("守护日志记录退出与重启",
              "uvicorn 退出" in log_text and log_text.count("启动 uvicorn") >= 2)
        check("停止后 PID 文件已清理", not (run_dir / "uvicorn.pid").exists())
    finally:
        log_fh.close()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


def _docker_available() -> tuple[bool, str]:
    if not shutil.which("docker"):
        return False, "未找到 docker"
    try:
        proc = _run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=15)
    except subprocess.TimeoutExpired:
        return False, "docker info 超时"
    return proc.returncode == 0, (proc.stdout or proc.stderr).strip()[:80]


def _ensure_image() -> tuple[bool, str, str]:
    candidates = [
        "nginx:alpine",
        "nginx:1.27-alpine",
        "docker.m.daocloud.io/library/nginx:1.27-alpine",
    ]
    for tag in candidates:
        if _run(["docker", "image", "inspect", tag], timeout=20).returncode == 0:
            return True, tag, "local"
    try:
        pull = _run(["docker", "pull", candidates[0]], timeout=240)
    except subprocess.TimeoutExpired:
        return False, "", "pull timeout"
    output = (pull.stdout or pull.stderr or "").strip()
    return pull.returncode == 0, candidates[0], output.splitlines()[-1][:100] if output else "pull failed"


def docker_https_checks(tmp: Path, cert_path: Path, key_path: Path) -> None:
    section("4. HTTPS 端到端（Docker Nginx + 自签证书）")
    ok, detail = _docker_available()
    if not ok:
        skip("Docker 环境", detail)
        return

    compose = _run(
        ["docker", "compose", "-f", str(SERVICE_DIR.parent / "database" / "docker-compose.yml"), "config"],
        timeout=30,
    )
    check("docker compose 配置可解析", compose.returncode == 0,
          (compose.stderr or compose.stdout).strip()[:160] or "ok")

    ok, image, detail = _ensure_image()
    if not ok:
        skip("Nginx 镜像", detail)
        return
    print(f"    使用镜像：{image}（{detail}）")

    port_svc = _free_port()
    port_https = _free_port()
    port_http = _free_port()
    name = f"nxz-verify-nginx-{os.getpid()}"
    env = dict(os.environ)
    env.update(
        KB_STORE="json",
        LLM_MOCK="1",
        AUTH_ENABLED="false",
        RATE_LIMIT_ENABLED="false",
    )
    uv_log = open(tmp / "uvicorn.log", "w", encoding="utf-8")
    uv = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0",
         "--port", str(port_svc)],
        cwd=str(SERVICE_DIR), env=env, stdout=uv_log, stderr=subprocess.STDOUT,
    )
    container = ""
    try:
        if not _wait_http(port_svc, timeout=75):
            check("上游 uvicorn 可达", False, "健康检查超时")
            return
        check("上游 uvicorn 可达", True, f"port={port_svc}")

        conf = tmp / "nginx.conf"
        render = _run(
            [sys.executable, "deploy/nginx/render_nginx_conf.py", "--out", str(conf),
             "--server-name", "localhost", "--cert-dir", "/etc/nginx/certs",
             "--upstream", f"host.docker.internal:{port_svc}",
             "--http-port", "8080", "--https-port", "8443"],
            cwd=SERVICE_DIR,
        )
        check("渲染容器版 Nginx 配置", render.returncode == 0, render.stderr[-160:] or "ok")
        certs_dir = tmp / "docker-certs"
        certs_dir.mkdir(exist_ok=True)
        shutil.copy2(cert_path, certs_dir / "fullchain.pem")
        shutil.copy2(key_path, certs_dir / "privkey.pem")
        nginx_logs = tmp / "nginx-logs"
        nginx_logs.mkdir(exist_ok=True)

        run = _run(
            ["docker", "run", "--rm", "-d", "--name", name,
             "--mount", f"type=bind,source={conf.as_posix()},target=/etc/nginx/nginx.conf,readonly",
             "--mount", f"type=bind,source={certs_dir.as_posix()},target=/etc/nginx/certs,readonly",
             "--mount", f"type=bind,source={nginx_logs.as_posix()},target=/etc/nginx/logs",
             "-p", f"127.0.0.1:{port_https}:8443",
             "-p", f"127.0.0.1:{port_http}:8080",
             image],
            timeout=60,
        )
        check("Nginx 容器启动（配置通过 nginx -t）", run.returncode == 0,
              (run.stderr or run.stdout).strip()[:160] or "ok")
        if run.returncode != 0:
            return
        container = run.stdout.strip()

        deadline = time.time() + 30
        reachable = False
        while time.time() < deadline:
            with socket.socket() as s:
                s.settimeout(0.5)
                if s.connect_ex(("127.0.0.1", port_https)) == 0:
                    reachable = True
                    break
            time.sleep(0.5)
        check("Nginx 443 可连接", reachable, f"port={port_https}")
        if not reachable:
            logs = _run(["docker", "logs", name], timeout=15)
            print("    nginx logs:", (logs.stdout or logs.stderr).strip()[-400:])
            return

        import warnings

        import httpx

        warnings.filterwarnings("ignore", message="Unverified HTTPS request")
        with httpx.Client(base_url=f"https://127.0.0.1:{port_https}", verify=False,
                          trust_env=False, timeout=30) as cli:
            resp = cli.get("/api/health")
            check("HTTPS 健康检查 200", resp.status_code == 200, str(resp.status_code))
            data = resp.json() if resp.status_code == 200 else {}
            check("HTTPS 响应为 kb-service", data.get("ok") is True and "store" in data,
                  str(data)[:100])
            check("HSTS 响应头存在",
                  "max-age" in resp.headers.get("strict-transport-security", ""),
                  resp.headers.get("strict-transport-security", "-"))
            check("nosniff 响应头存在",
                  resp.headers.get("x-content-type-options") == "nosniff",
                  resp.headers.get("x-content-type-options", "-"))

            redirect = cli.get(f"http://127.0.0.1:{port_http}/api/health",
                               follow_redirects=False)
            check("HTTP 301 跳转 HTTPS", redirect.status_code == 301,
                  f"{redirect.status_code} → {redirect.headers.get('location', '-')}")

            t0 = time.time()
            first_at = 0.0
            chunks = 0
            with cli.stream("POST", "/api/chat/stream",
                            json={"question": "校园网怎么开户", "role": "本科生"}) as stream:
                status = stream.status_code
                for chunk in stream.iter_bytes():
                    if chunk:
                        chunks += 1
                        if not first_at:
                            first_at = time.time() - t0
            total = time.time() - t0
            check("HTTPS SSE 流式可用", status == 200 and chunks > 0,
                  f"status={status} chunks={chunks}")
            check("SSE 首块未被 Nginx 攒批", chunks >= 2 and first_at < total,
                  f"first={first_at * 1000:.0f}ms total={total * 1000:.0f}ms")
    finally:
        uv_log.close()
        if container:
            _run(["docker", "rm", "-f", container], timeout=30)
        if uv.poll() is None:
            uv.terminate()
            try:
                uv.wait(timeout=10)
            except subprocess.TimeoutExpired:
                uv.kill()


def main() -> int:
    parser = argparse.ArgumentParser(description="生产部署验收（任务 5.1）")
    parser.add_argument("--skip-docker", action="store_true", help="跳过 Docker Nginx 实机段")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        static_checks()
        cert_path, key_path = cert_checks(tmp)
        supervisor_checks(tmp)
        if args.skip_docker:
            skip("HTTPS 端到端", "--skip-docker")
        else:
            docker_https_checks(tmp, cert_path, key_path)

    print(f"\n结果：{PASSED} PASS / {FAILED} FAIL / {SKIPPED} SKIP")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
