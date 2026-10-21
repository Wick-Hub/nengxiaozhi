"""语音输入浏览器验收（任务 4.7 / C-04·P-QA-01）。

真实 Chromium（fake 麦克风设备）覆盖 HTTP 层看不到的行为：

1. 能力可用时展示麦克风入口，点击进入录音态并显示计时；
2. 结束录音 → ``/api/asr/transcribe`` → 文本填入输入框（可编辑、不自动发送）；
3. 点击发送后走原有 ``/api/chat/stream``，回答正常渲染；
4. 能力关闭时隐藏入口；麦克风权限被拒时给出提示。

**数据隔离**：验收前备份 ``data/sessions.json`` / ``data/unrecognized.json``，
结束后恢复。

用法::

    python tools/verify_asr_ui.py
    python tools/verify_asr_ui.py --headful

退出码：全部通过为 0，存在 FAIL 为 1。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

os.environ.setdefault("KB_STORE", "json")
os.environ.setdefault("LLM_MOCK", "1")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH", r"C:\Users\wick\.workbuddy-ai\binaries\ms-playwright"
)

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


def main() -> int:
    parser = argparse.ArgumentParser(description="语音输入浏览器验收（任务 4.7）")
    parser.add_argument("--port", type=int, default=8047, help="验收用服务端口")
    parser.add_argument("--headful", action="store_true", help="显示浏览器窗口（调试用）")
    parser.add_argument("--shots-dir", default="", help="截图目录（默认 kb-service/artifacts）")
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    from tools._server import read_log, spawn_server, stop_server, wait_port

    data_dir = SERVICE_DIR / "data"
    sessions_path = data_dir / "sessions.json"
    unrecognized_path = data_dir / "unrecognized.json"
    tmp_dir = Path(tempfile.mkdtemp(prefix="nxz-asr-ui-"))
    backups = {
        "sessions": _backup(sessions_path, tmp_dir),
        "unrecognized": _backup(unrecognized_path, tmp_dir),
    }

    port = args.port
    base = f"http://127.0.0.1:{port}"
    shots = Path(args.shots_dir) if args.shots_dir else SERVICE_DIR / "artifacts"
    shots.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["KB_STORE"] = "json"
    env["AUTH_ENABLED"] = "false"
    env["RATE_LIMIT_ENABLED"] = "false"
    env["LLM_MOCK"] = "1"
    env["ASR_PROVIDER"] = "mock"
    env["ASR_MOCK_TEXT"] = "校园网怎么开通？"

    print(f"启动服务 127.0.0.1:{port}（KB_STORE=json, ASR_PROVIDER=mock）…")
    proc, log_path = spawn_server(port, env, SERVICE_DIR)
    try:
        if not wait_port(port, timeout=60):
            print("服务启动失败，日志尾部：")
            print(read_log(log_path, 3000))
            return 1

        errors: list[str] = []
        statuses: dict[str, int] = {}
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=not args.headful,
                args=[
                    "--use-fake-device-for-media-stream",
                    "--use-fake-ui-for-media-stream",
                ],
            )
            ctx = browser.new_context(
                viewport={"width": 1440, "height": 950},
                permissions=["microphone"],
            )
            page = ctx.new_page()
            page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))

            def on_console(msg) -> None:  # noqa: ANN001
                if msg.type == "error":
                    errors.append(f"console.error: {msg.text}")

            def on_response(resp) -> None:  # noqa: ANN001
                if "/api/asr/transcribe" in resp.url:
                    statuses["asr"] = resp.status
                elif "/api/chat/stream" in resp.url:
                    statuses["chat"] = resp.status

            page.on("console", on_console)
            page.on("response", on_response)

            disabled_page = None
            denied_page = None
            try:
                section("1. 录音入口")
                page.goto(f"{base}/", wait_until="networkidle")
                page.wait_for_selector("#mic", state="visible", timeout=20000)
                check("能力可用时展示麦克风", page.locator("#mic").is_visible())
                check("初始非录音态", page.get_attribute("#mic", "aria-pressed") == "false")
                check("能力状态请求成功",
                      page.evaluate("async () => (await (await fetch('/api/asr/status')).json()).data.enabled"))

                page.click("#mic")
                page.wait_for_selector("#mic.recording", timeout=5000)
                check("点击进入录音态", "recording" in (page.get_attribute("#mic", "class") or ""))
                check("录音按钮 aria-pressed=true",
                      page.get_attribute("#mic", "aria-pressed") == "true")
                time.sleep(1.4)
                check("录音计时提示",
                      "正在录音" in (page.get_attribute("#input", "placeholder") or ""))
                page.screenshot(path=str(shots / "asr-recording.png"))

                section("2. 转写与问答")
                page.click("#mic")
                page.wait_for_function(
                    "() => document.getElementById('input').value.includes('校园网')",
                    timeout=20000,
                )
                transcript = page.input_value("#input")
                check("转写填入输入框", "校园网怎么开通" in transcript, transcript)
                check("录音态已结束",
                      "recording" not in (page.get_attribute("#mic", "class") or ""))
                page.wait_for_function(
                    "() => [...document.querySelectorAll('.nxz-toast')]"
                    ".some(el => el.innerText.includes('已转写'))",
                    timeout=5000,
                )
                check("出现转写提示", True)
                check("转写后发送按钮可用", page.is_enabled("#send"))
                check("转写不自动发送", page.locator(".msg.user").count() == 0)
                page.screenshot(path=str(shots / "asr-transcript.png"))

                page.click("#send")
                page.wait_for_selector(".msg.user .bubble", timeout=5000)
                user_text = page.locator(".msg.user .bubble").last.inner_text()
                check("发送后用户消息=转写文本", "校园网怎么开通" in user_text, user_text)
                page.wait_for_selector(".msg.ai .msg-actions", timeout=30000)
                answer = page.locator(".msg.ai .bubble").last.inner_text()
                check("同一问答链路返回回答", len(answer.strip()) > 0, answer[:40])
                check("转写接口 200", statuses.get("asr") == 200, str(statuses.get("asr")))
                check("问答流接口 200", statuses.get("chat") == 200, str(statuses.get("chat")))
                check("回答带 AI 标识", page.locator(".msg.ai .ai-label").count() >= 1)
                page.screenshot(path=str(shots / "asr-answer.png"))

                section("3. 能力关闭")
                disabled_page = ctx.new_page()
                disabled_page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
                disabled_page.route(
                    "**/api/asr/status",
                    lambda route: route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"ok": True, "data": {"enabled": False, "ready": False}}),
                    ),
                )
                disabled_page.goto(f"{base}/", wait_until="networkidle")
                disabled_page.wait_for_function(
                    "() => document.getElementById('mic').hidden === true", timeout=10000
                )
                check("能力关闭时隐藏麦克风", disabled_page.locator("#mic").is_hidden())

                section("4. 麦克风权限被拒")
                denied_page = ctx.new_page()
                denied_page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
                denied_page.goto(f"{base}/", wait_until="networkidle")
                denied_page.wait_for_selector("#mic", state="visible", timeout=20000)
                denied_page.evaluate(
                    """() => {
                        navigator.mediaDevices.getUserMedia = () =>
                            Promise.reject(new DOMException('denied', 'NotAllowedError'));
                    }"""
                )
                denied_page.click("#mic")
                denied_page.wait_for_function(
                    "() => [...document.querySelectorAll('.nxz-toast')]"
                    ".some(el => el.innerText.includes('无法访问麦克风'))",
                    timeout=5000,
                )
                check("权限被拒给出提示", True)
                check("权限被拒后仍在非录音态",
                      "recording" not in (denied_page.get_attribute("#mic", "class") or ""))

                section("5. 运行时错误")
                check("零 pageerror / console.error", not errors, "; ".join(errors[:3]))
            finally:
                for extra in (disabled_page, denied_page):
                    if extra is not None:
                        try:
                            extra.close()
                        except Exception:  # noqa: BLE001
                            pass
                page.close()
                browser.close()

        print(f"\n截图目录：{shots}")
        print(f"\n结果：{PASSED} PASS / {FAILED} FAIL")
        return 1 if FAILED else 0
    finally:
        stop_server(proc)
        _restore(sessions_path, backups["sessions"])
        _restore(unrecognized_path, backups["unrecognized"])
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
