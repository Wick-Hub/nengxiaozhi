"""子进程启动 uvicorn 的公共辅助（验收 / 冒烟工具共用）。

**为什么不用 ``subprocess.PIPE``**：父进程不读取管道时，子进程一旦写满内核管道
缓冲区（Windows 约 64KB）就会**阻塞在 write 上**，于是 HTTP 响应体发不完、
客户端读超时——现象是「接口莫名超时，但 curl 又是好的」。

数据库不可用时 uvicorn 会打印大段异常栈，恰好会触发这个问题（任务 1.9 排查实录）。
改为重定向到日志文件：既不阻塞，也能在启动失败时读出来诊断。
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path


def spawn_server(port: int, env: dict, service_dir: Path) -> tuple[subprocess.Popen, Path]:
    """启动 ``uvicorn app.main:app``，stdout/stderr 落到临时日志文件。

    返回 ``(进程, 日志路径)``；调用方负责在 ``finally`` 中 :func:`stop_server`。
    """
    log_path = Path(tempfile.gettempdir()) / f"kb-service-{port}.log"
    # 以文件句柄直接交给子进程：父进程无需读管道，不存在缓冲区写满的风险
    log_fh = open(log_path, "w", encoding="utf-8", errors="replace")  # noqa: SIM115
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=str(service_dir),
            env=env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
        )
    finally:
        log_fh.close()  # 子进程已持有自己的句柄
    return proc, log_path


def read_log(log_path: Path, limit: int = 2000) -> str:
    """读取子进程日志（诊断用）。"""
    try:
        return log_path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return ""


def stop_server(proc: subprocess.Popen, grace: float = 8.0) -> None:
    """优雅终止子进程，超时则强杀。"""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:  # pragma: no cover - 兜底
            pass


def wait_port(port: int, timeout: float = 40.0) -> bool:
    """等待端口可连接。"""
    import socket

    t0 = time.time()
    while time.time() - t0 < timeout:
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.3)
    return False
