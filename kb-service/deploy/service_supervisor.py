"""kb-service 常驻守护进程（任务 5.1）。

负责启动 uvicorn、进程退出后自动拉起、按健康检查判定假死并重启，
同时维护 ``run/stop.flag`` 实现可预期的停止（避免「停了又被拉起」）。

只依赖标准库，Windows 计划任务与 Linux systemd 都可直接运行::

    python deploy/service_supervisor.py --port 8000
    python deploy/service_supervisor.py --port 8000 --health-url http://127.0.0.1:8000/api/health
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
LOG_MAX_BYTES = 10 * 1024 * 1024


def _rotate(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            backup = path.with_suffix(path.suffix + ".1")
            if backup.exists():
                backup.unlink()
            path.replace(backup)
    except OSError:
        pass


class Logger:
    def __init__(self, log_dir: Path) -> None:
        log_dir.mkdir(parents=True, exist_ok=True)
        self.path = log_dir / "service.log"

    def __call__(self, message: str) -> None:
        _rotate(self.path)
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {message}"
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        print(line, flush=True)


def _http_ok(url: str, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - 固定本地地址
            return 200 <= resp.status < 300
    except Exception:  # noqa: BLE001 - 健康检查失败一律视为不可用
        return False


def _terminate(proc: subprocess.Popen, grace: float = 10.0) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            pass


def _write_pid(path: Path, pid: int | None) -> None:
    try:
        if pid is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(str(pid), encoding="ascii")
    except OSError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="kb-service 常驻守护")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--app-dir", default=str(SERVICE_DIR))
    parser.add_argument("--log-dir", default=str(SERVICE_DIR / "logs"))
    parser.add_argument("--run-dir", default=str(SERVICE_DIR / "run"))
    parser.add_argument("--restart-delay", type=float, default=5.0)
    parser.add_argument("--health-url", default="")
    parser.add_argument("--health-interval", type=float, default=30.0)
    parser.add_argument("--health-failures", type=int, default=3)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    app_dir = Path(args.app_dir).resolve()
    run_dir = Path(args.run_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    stop_flag = run_dir / "stop.flag"
    supervisor_pid = run_dir / "service.pid"
    child_pid = run_dir / "uvicorn.pid"
    log = Logger(Path(args.log_dir).resolve())

    stopping = False

    def request_stop(signum, _frame) -> None:  # noqa: ANN001
        nonlocal stopping
        stopping = True
        log(f"收到信号 {signum}，准备停止")

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    supervisor_pid.write_text(str(os.getpid()), encoding="ascii")
    log(f"守护启动 pid={os.getpid()} app_dir={app_dir} port={args.port}")

    child: subprocess.Popen | None = None
    try:
        while not stopping and not stop_flag.exists():
            cmd = [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                args.host,
                "--port",
                str(args.port),
                "--proxy-headers",
                "--forwarded-allow-ips",
                "127.0.0.1",
                "--log-level",
                args.log_level,
            ]
            log("启动 uvicorn: " + " ".join(cmd))
            child = subprocess.Popen(
                cmd,
                cwd=str(app_dir),
                env=dict(os.environ),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
            )
            _write_pid(child_pid, child.pid)
            log(f"uvicorn 已启动 pid={child.pid}")

            failures = 0
            next_check = time.time() + args.health_interval
            while not stopping and not stop_flag.exists():
                code = child.poll()
                if code is not None:
                    log(f"uvicorn 退出 code={code}")
                    break
                if args.health_url and time.time() >= next_check:
                    if _http_ok(args.health_url):
                        failures = 0
                    else:
                        failures += 1
                        log(f"健康检查失败 {failures}/{args.health_failures}")
                        if failures >= args.health_failures:
                            log("健康检查连续失败，重启 uvicorn")
                            _terminate(child)
                            break
                    next_check = time.time() + args.health_interval
                time.sleep(1)

            _write_pid(child_pid, None)
            _terminate(child)
            child = None
            if stopping or stop_flag.exists():
                break
            log(f"{args.restart_delay:.0f}s 后重启")
            end = time.time() + args.restart_delay
            while time.time() < end and not stopping and not stop_flag.exists():
                time.sleep(0.5)
    finally:
        if child is not None:
            _terminate(child)
        _write_pid(child_pid, None)
        _write_pid(supervisor_pid, None)
        log("守护退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
