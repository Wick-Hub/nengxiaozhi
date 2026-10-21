"""语音输入验收（任务 4.7 / C-04·P-QA-01，JSON 存储 + mock ASR）。

覆盖 HTTP 层：

1. 能力状态与前端资源：``/api/asr/status``、对话页麦克风按钮、转写脚本；
2. mock 转写：webm / wav / 带 codecs 的 MIME，返回固定文本；
3. 上传校验：空音频、非音频格式、超过大小上限；
4. 边界：超长转写截断到问答接口允许的 500 字、自定义录音上限；
5. 同一问答链路：转写文本直接调用 ``/api/chat`` 与 ``/api/chat/stream``；
6. 关闭开关：状态不可用、转写 503。

**数据隔离**：验收前备份 ``data/sessions.json`` / ``data/unrecognized.json``，
结束后恢复。请勿与写入同一数据目录的服务同时运行。

用法::

    python tools/verify_asr.py
    python tools/verify_asr.py --port 8044 --keep

退出码：全部通过为 0，存在 FAIL 为 1。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

os.environ.setdefault("KB_STORE", "json")
os.environ.setdefault("LLM_MOCK", "1")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")

try:  # pragma: no cover - 仅影响交互输出
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

PASSED = 0
FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
    else:
        FAILED += 1
    tail = f"  [{detail}]" if detail else ""
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{tail}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def _backup(path: Path, backup_dir: Path) -> Path | None:
    if not path.is_file():
        return None
    target = backup_dir / path.name
    shutil.copy2(path, target)
    return target


def _restore(path: Path, backup: Path | None) -> None:
    if backup is None:
        if path.exists():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup, path)


def _server_env(**overrides: str) -> dict:
    env = dict(os.environ)
    env["KB_STORE"] = "json"
    env["AUTH_ENABLED"] = "false"
    env["RATE_LIMIT_ENABLED"] = "false"
    env["LLM_MOCK"] = "1"
    env["ASR_PROVIDER"] = "mock"
    env["ASR_MOCK_TEXT"] = "校园网怎么开通？"
    env.update(overrides)
    return env


def main() -> int:
    parser = argparse.ArgumentParser(description="语音输入验收（任务 4.7）")
    parser.add_argument("--port", type=int, default=8044, help="验收用服务端口")
    parser.add_argument("--keep", action="store_true", help="保留验收数据（默认恢复备份）")
    args = parser.parse_args()

    import httpx

    from tools._server import read_log, spawn_server, stop_server, wait_port

    data_dir = SERVICE_DIR / "data"
    sessions_path = data_dir / "sessions.json"
    unrecognized_path = data_dir / "unrecognized.json"
    tmp_dir = Path(tempfile.mkdtemp(prefix="nxz-asr-verify-"))
    backups = {
        "sessions": _backup(sessions_path, tmp_dir),
        "unrecognized": _backup(unrecognized_path, tmp_dir),
    }

    port = args.port
    base = f"http://127.0.0.1:{port}"
    env = _server_env()
    proc = None
    log_path = None
    edge_proc = None
    disabled_proc = None
    try:
        print(f"启动服务 127.0.0.1:{port}（KB_STORE=json, ASR_PROVIDER=mock）…")
        proc, log_path = spawn_server(port, env, SERVICE_DIR)
        if not wait_port(port, timeout=60):
            print("服务启动失败，日志尾部：")
            print(read_log(log_path, 3000))
            return 1

        with httpx.Client(timeout=30, trust_env=False, base_url=base) as cli:
            section("1. 能力状态与前端资源")
            status = cli.get("/api/asr/status")
            check("状态接口 200", status.status_code == 200, str(status.status_code))
            data = status.json().get("data") or {}
            check("mock 模式可用", data.get("enabled") is True and data.get("ready") is True)
            check("provider=mock", data.get("provider") == "mock" and data.get("mock") is True)
            check("录音上限下发", data.get("maxSeconds") == 60, str(data.get("maxSeconds")))
            check("转写上限下发", data.get("maxTranscriptChars") == 500)
            check("不泄露 API Key", "apikey" not in str(data).lower() and "api_key" not in str(data).lower())
            page = cli.get("/")
            check("对话页含麦克风按钮", page.status_code == 200 and 'id="mic"' in page.text)
            app_js = cli.get("/app.js")
            check("前端接入转写接口", app_js.status_code == 200 and "/api/asr/transcribe" in app_js.text)
            check("前端读取能力状态", "/api/asr/status" in app_js.text)

            section("2. mock 转写")
            audio = b"fake-webm-audio" * 8
            webm = cli.post(
                "/api/asr/transcribe",
                files={"file": ("voice.webm", audio, "audio/webm;codecs=opus")},
            )
            check("webm 转写 200", webm.status_code == 200, str(webm.status_code))
            body = webm.json().get("data") or {}
            check("返回固定文本", body.get("text") == "校园网怎么开通？", str(body.get("text")))
            check("provider=mock", body.get("provider") == "mock")
            check("未截断", body.get("truncated") is False)
            check("耗时字段为整数", isinstance(body.get("elapsedMs"), int))
            wav = cli.post(
                "/api/asr/transcribe",
                files={"file": ("voice.wav", b"RIFF" + b"0" * 64, "audio/wav")},
            )
            check("wav 转写 200", wav.status_code == 200, str(wav.status_code))
            octet = cli.post(
                "/api/asr/transcribe",
                files={"file": ("voice", b"x" * 64, "application/octet-stream")},
            )
            check("octet-stream 兜底可用", octet.status_code == 200, str(octet.status_code))

            section("3. 上传校验")
            empty = cli.post(
                "/api/asr/transcribe",
                files={"file": ("voice.webm", b"", "audio/webm")},
            )
            check("空音频 400", empty.status_code == 400, str(empty.status_code))
            bad = cli.post(
                "/api/asr/transcribe",
                files={"file": ("note.txt", b"hello", "text/plain")},
            )
            check("非音频 415", bad.status_code == 415, str(bad.status_code))
            missing = cli.post("/api/asr/transcribe")
            check("缺少文件 422", missing.status_code == 422, str(missing.status_code))

            section("4. 同一问答链路")
            question = body.get("text") or ""
            chat = cli.post("/api/chat", json={"question": question, "role": "本科生"})
            check("转写文本可问答 200", chat.status_code == 200, str(chat.status_code))
            chat_data = (chat.json() or {}).get("data") or {}
            check("返回回答", bool(chat_data.get("answer")))
            check("返回会话 ID", bool(chat_data.get("sessionId")))
            check("回答问题=转写文本", chat_data.get("question") in (None, question))
            with cli.stream(
                "POST",
                "/api/chat/stream",
                json={"question": question, "role": "本科生"},
            ) as resp:
                stream_body = "".join(resp.iter_text())
            check("SSE 200", "text/event-stream" in resp.headers.get("content-type", ""))
            check("SSE 完成事件", '"type": "done"' in stream_body or '"type":"done"' in stream_body)
            check("SSE 含回答", '"answer"' in stream_body)

        # ---------------------------------------------------------- 边界服务
        edge_port = port + 1
        print(f"\n启动边界服务 127.0.0.1:{edge_port}（超长文本/1KB 上限）…")
        edge_env = _server_env(
            ASR_MOCK_TEXT="测" * 600,
            ASR_MAX_BYTES="1024",
            ASR_MAX_SECONDS="30",
        )
        edge_proc, edge_log = spawn_server(edge_port, edge_env, SERVICE_DIR)
        if not wait_port(edge_port, timeout=60):
            print("边界服务启动失败，日志尾部：")
            print(read_log(edge_log, 3000))
            return 1
        with httpx.Client(
            timeout=20, trust_env=False, base_url=f"http://127.0.0.1:{edge_port}"
        ) as cli:
            section("5. 边界与限制")
            status = cli.get("/api/asr/status").json()["data"]
            check("自定义录音上限", status.get("maxSeconds") == 30, str(status.get("maxSeconds")))
            long_resp = cli.post(
                "/api/asr/transcribe",
                files={"file": ("voice.webm", b"x" * 64, "audio/webm")},
            )
            long_body = long_resp.json().get("data") or {}
            check("超长文本截断标记", long_body.get("truncated") is True)
            check("截断到 500 字", len(long_body.get("text") or "") == 500,
                  str(len(long_body.get("text") or "")))
            big = cli.post(
                "/api/asr/transcribe",
                files={"file": ("voice.webm", b"x" * 1025, "audio/webm")},
            )
            check("超过大小上限 413", big.status_code == 413, str(big.status_code))
            ok_size = cli.post(
                "/api/asr/transcribe",
                files={"file": ("voice.webm", b"x" * 1024, "audio/webm")},
            )
            check("边界大小可用", ok_size.status_code == 200, str(ok_size.status_code))
        stop_server(edge_proc)
        edge_proc = None

        # ---------------------------------------------------------- 关闭开关
        off_port = port + 2
        print(f"\n启动关闭态服务 127.0.0.1:{off_port}（ASR_ENABLED=0）…")
        off_env = _server_env(ASR_ENABLED="0")
        disabled_proc, off_log = spawn_server(off_port, off_env, SERVICE_DIR)
        if not wait_port(off_port, timeout=60):
            print("关闭态服务启动失败，日志尾部：")
            print(read_log(off_log, 3000))
            return 1
        with httpx.Client(
            timeout=20, trust_env=False, base_url=f"http://127.0.0.1:{off_port}"
        ) as cli:
            section("6. 关闭开关")
            status = cli.get("/api/asr/status").json()["data"]
            check("状态不可用", status.get("enabled") is False and status.get("ready") is False)
            off = cli.post(
                "/api/asr/transcribe",
                files={"file": ("voice.webm", b"x" * 64, "audio/webm")},
            )
            check("转写 503", off.status_code == 503, str(off.status_code))
    finally:
        for handle in (edge_proc, disabled_proc, proc):
            if handle is not None:
                stop_server(handle)
        if not args.keep:
            _restore(sessions_path, backups["sessions"])
            _restore(unrecognized_path, backups["unrecognized"])

    print(f"\n结果：{PASSED} PASS / {FAILED} FAIL")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
