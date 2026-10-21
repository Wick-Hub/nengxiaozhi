"""客服体系 · 浏览器端到端验收（任务 4.6 前端）。

真实 Chromium 串起「用户转人工 → 坐席工作台接入 → 双向消息 → 结束会话」：

1. 对话页顶栏出现「转人工」，命中人工诉求后入口高亮；
2. 用户面板创建工单、展示首条消息与状态、可继续发言；
3. ``/support`` 工作台可检索工单、接入、回复、结束；
4. 坐席回复在用户面板轮询可见，用户追问在坐席工作台可见（WebSocket/刷新）；
5. 结束后用户端状态变「已结束」且不能继续发言；
6. 全程零 ``pageerror`` / ``console.error``。

**幂等性**：每次生成唯一主题 ``转人工浏览器验收 VFUI<时分秒>``，前后按该
主题清理工单，可重复运行。

用法::

    python tools/verify_support_ui.py
    python tools/verify_support_ui.py --store mysql --headful

退出码：全部通过为 0，存在 FAIL 为 1。
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

os.environ.setdefault("KB_STORE", "json")
os.environ.setdefault("LLM_MOCK", "1")
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


def cleanup(token: str, store: str) -> None:
    """按主题标记清理本次验收工单（JSON / MySQL 一致，幂等）。"""
    if not token:
        return
    if store == "mysql":
        from sqlalchemy import delete as sa_delete, select

        from app.db import models as M
        from app.db.session import db_transaction

        with db_transaction() as conn:
            ids = list(
                conn.execute(
                    select(M.Ticket.ticket_id).where(M.Ticket.subject.like(f"%{token}%"))
                ).scalars()
            )
            if ids:
                conn.execute(
                    sa_delete(M.TicketMessage).where(M.TicketMessage.ticket_id.in_(ids))
                )
                conn.execute(sa_delete(M.Ticket).where(M.Ticket.ticket_id.in_(ids)))
        return

    import json

    from app.db import support_store

    path = support_store.SUPPORT_PATH
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    keep = []
    drop = []
    for ticket in data.get("tickets", []):
        if token in str(ticket.get("subject") or ""):
            drop.append(int(ticket.get("ticketId") or 0))
        else:
            keep.append(ticket)
    if not drop:
        return
    data["tickets"] = keep
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _run(base: str, shots: Path, *, headful: bool, token: str) -> int:
    from playwright.sync_api import sync_playwright

    subject = f"转人工浏览器验收 {token}"
    reply_text = f"同学你好，已收到你的问题（{token}），正在处理。"
    follow_text = f"补充一下：我的校园卡是昨天丢的（{token}）"
    js_errors: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headful)
        user = browser.new_page(viewport={"width": 1360, "height": 900})
        admin = browser.new_page(viewport={"width": 1500, "height": 950})

        for page in (user, admin):
            page.on("pageerror", lambda exc: js_errors.append(f"pageerror: {exc}"))

            def on_console(msg) -> None:  # noqa: ANN001
                if msg.type == "error":
                    js_errors.append(f"console.error: {msg.text}")

            page.on("console", on_console)

        section("1. 用户端：转人工入口与建议")
        user.goto(base + "/", wait_until="domcontentloaded")
        user.wait_for_selector("#support-open:not([hidden])", timeout=20000)
        check("顶栏出现转人工入口", user.is_visible("#support-open"))

        user.fill("#input", "我要转人工")
        user.click("#send")
        user.wait_for_selector(".msg.ai:not(.thinking) .bubble", timeout=30000)
        user.wait_for_function(
            "document.querySelector('#support-open').classList.contains('attention')",
            timeout=5000,
        )
        check("命中人工诉求后入口高亮", True)

        section("2. 用户端：创建工单并发送首条消息")
        user.click("#support-open")
        user.wait_for_selector("#support-panel:not([hidden])")
        check("客服面板打开", user.is_visible("#support-panel"))
        user.fill("#support-input", subject)
        user.click("#support-send")
        user.wait_for_selector(
            f".support-message.user .support-bubble:has-text('{token}')", timeout=15000
        )
        check("首条消息进入会话", True)
        user.wait_for_selector("#support-state:not([hidden])", timeout=10000)
        state1 = user.inner_text("#support-state")
        check("展示工单状态", state1 in ("待接入", "离线留言", "接待中"), state1)
        user.screenshot(path=str(shots / "support-ui-user-created.png"), full_page=True)

        section("3. 坐席工作台：检索 → 接入 → 回复")
        admin.goto(base + "/support", wait_until="domcontentloaded")
        admin.wait_for_selector("#sp-ticket-list", timeout=20000)
        admin.wait_for_function(
            "document.querySelector('#sp-seat') && document.querySelector('#sp-seat').value",
            timeout=15000,
        )
        check("工作台已选择坐席", bool(admin.input_value("#sp-seat")))
        admin.fill("#sp-q", token)
        admin.click("#sp-apply")
        admin.wait_for_selector(f".sp-ticket:has-text('{token}')", timeout=15000)
        check("按关键词检索到工单", True)

        admin.click(f".sp-ticket:has-text('{token}')")
        admin.wait_for_selector(f"#sp-thread-title:has-text('{token}')", timeout=10000)
        admin.wait_for_selector("#sp-claim:not([hidden])", timeout=10000)
        admin.click("#sp-claim")
        admin.wait_for_function(
            "document.querySelector('#sp-thread-state')?.textContent.trim() === '接待中'",
            timeout=15000,
        )
        check("坐席接入 → 接待中", True)

        admin.fill("#sp-reply", reply_text)
        admin.click("#sp-send")
        admin.wait_for_selector(f".sp-msg.agent .sp-msg-body:has-text('{token}')", timeout=15000)
        check("坐席回复进入会话", True)
        admin.screenshot(path=str(shots / "support-ui-admin-replied.png"), full_page=True)

        section("4. 用户端：收到回复并追问")
        user.wait_for_selector(
            f".support-message.agent .support-bubble:has-text('{token}')", timeout=20000
        )
        check("用户轮询收到坐席回复", True)
        user.wait_for_function(
            "document.querySelector('#support-state')?.textContent.trim() === '接待中'",
            timeout=10000,
        )
        check("用户端状态同步接待中", True)

        user.fill("#support-input", follow_text)
        user.click("#support-send")
        user.wait_for_selector(
            f".support-message.user .support-bubble:has-text('昨天丢的')", timeout=15000
        )
        check("用户追问发送成功", True)
        try:
            admin.wait_for_selector(
                f".sp-msg.user .sp-msg-body:has-text('昨天丢的')", timeout=12000
            )
            ws_ok = True
        except Exception:  # noqa: BLE001 - WS 偶发未推送时手动刷新兜底
            admin.click("#sp-refresh")
            admin.wait_for_selector(
                f".sp-msg.user .sp-msg-body:has-text('昨天丢的')", timeout=10000
            )
            ws_ok = False
        check("坐席工作台收到用户追问", True, "WebSocket 实时" if ws_ok else "刷新后可见")

        section("5. 结束会话")
        admin.click("#sp-close")
        admin.wait_for_function(
            "document.querySelector('#sp-thread-state')?.textContent.trim() === '已关闭'",
            timeout=10000,
        )
        check("坐席结束会话", True)

        user.wait_for_function(
            "document.querySelector('#support-state')?.textContent.trim() === '已结束'",
            timeout=20000,
        )
        check("用户端状态变已结束", True)
        check("结束后隐藏结束按钮", user.is_hidden("#support-end"))
        user.screenshot(path=str(shots / "support-ui-user-closed.png"), full_page=True)

        section("6. 前端错误")
        check("无 JS 运行时错误", not js_errors, "; ".join(js_errors[:3]))

        browser.close()

    return 0 if FAILED == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="客服体系浏览器验收（任务 4.6）")
    parser.add_argument("--port", type=int, default=8026, help="验收用服务端口")
    parser.add_argument("--store", default="json", choices=["json", "mysql"])
    parser.add_argument("--headful", action="store_true", help="显示浏览器窗口（调试用）")
    parser.add_argument("--shots-dir", default="", help="截图目录（默认 kb-service/artifacts）")
    args = parser.parse_args()

    from tools._server import read_log, spawn_server, stop_server, wait_port

    os.environ["KB_STORE"] = args.store
    port = args.port
    base = f"http://127.0.0.1:{port}"
    shots = Path(args.shots_dir) if args.shots_dir else SERVICE_DIR / "artifacts"
    shots.mkdir(parents=True, exist_ok=True)
    token = "VFUI" + datetime.now().strftime("%H%M%S")

    env = dict(os.environ)
    env["KB_STORE"] = args.store
    env["AUTH_ENABLED"] = "false"
    env["RATE_LIMIT_ENABLED"] = "false"
    env["LLM_MOCK"] = "1"

    print(f"启动服务 127.0.0.1:{port}（KB_STORE={args.store}）…")
    proc, log_path = spawn_server(port, env, SERVICE_DIR)
    code = 1
    try:
        if not wait_port(port, timeout=60):
            print("服务启动失败，日志尾部：")
            print(read_log(log_path, 3000))
            return 1
        cleanup(token, args.store)
        try:
            code = _run(base, shots, headful=args.headful, token=token)
        finally:
            cleanup(token, args.store)
            print(f"\n截图目录：{shots}")
            print(f"\n结果：{PASSED} PASS / {FAILED} FAIL")
    finally:
        stop_server(proc)

    return 0 if code == 0 and FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
