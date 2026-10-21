"""问数服务浏览器验收（任务 4.3 / P-DQ-01·02·03）。

真实 Chromium 覆盖 HTTP 层看不到的行为：

1. 本科生提问「本周课表」→ 结构化卡片：标题/学期周次/指标/身份/表格/脚注；
2. 「第 3 周周一」过滤后只剩两门课，标题变为「周一课表」；
3. 「我的成绩」→ 加权平均 89.6、6 行成绩；
4. 「一卡通余额」→ 余额与流水表格；教职工身份余额不同；
5. 访客查课表 → 拒绝卡片（HTTP 403），回答主链路不被阻断；
6. 全程零 ``pageerror`` / ``console.error``。

演示模式（``AUTH_ENABLED=false``）固定映射演示学号 ``20230001``。

用法::

    python tools/verify_data_query_ui.py
    python tools/verify_data_query_ui.py --headful

退出码：全部通过为 0，存在 FAIL 为 1。
"""

from __future__ import annotations

import argparse
import os
import sys
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


def main() -> int:
    parser = argparse.ArgumentParser(description="问数服务浏览器验收（任务 4.3）")
    parser.add_argument("--port", type=int, default=8048, help="验收用服务端口")
    parser.add_argument("--headful", action="store_true", help="显示浏览器窗口（调试用）")
    parser.add_argument("--shots-dir", default="", help="截图目录（默认 kb-service/artifacts）")
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    from tools._server import read_log, spawn_server, stop_server, wait_port

    port = args.port
    base = f"http://127.0.0.1:{port}"
    shots = Path(args.shots_dir) if args.shots_dir else SERVICE_DIR / "artifacts"
    shots.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["KB_STORE"] = "json"
    env["AUTH_ENABLED"] = "false"
    env["RATE_LIMIT_ENABLED"] = "false"
    env["LLM_MOCK"] = "1"
    env["DQ_PROVIDER"] = "mock"

    print(f"启动服务 127.0.0.1:{port}（KB_STORE=json, AUTH_ENABLED=false, DQ_PROVIDER=mock）…")
    proc, log_path = spawn_server(port, env, SERVICE_DIR)
    try:
        if not wait_port(port, timeout=60):
            print("服务启动失败，日志尾部：")
            print(read_log(log_path, 3000))
            return 1

        errors: list[str] = []

        def watch(target) -> None:  # noqa: ANN001
            target.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))

            def on_console(msg) -> None:  # noqa: ANN001
                if msg.type == "error":
                    errors.append(f"console.error: {msg.text}")

            target.on("console", on_console)

        def ask(page, question: str):  # noqa: ANN001
            """发送问题并返回新的 AI 气泡（等待 finalize 完成）。"""
            ai_before = page.locator(".msg.ai:not(.thinking)").count()
            page.fill("#input", question)
            page.click("#send")
            page.wait_for_function(
                "n => document.querySelectorAll('.msg.ai:not(.thinking)').length > n",
                arg=ai_before,
                timeout=30000,
            )
            bubble = page.locator(".msg.ai .bubble").nth(ai_before)
            # finalize 先移除 thinking 再同步追加卡片；等待卡片真正挂载，避免读取竞态
            bubble.locator(".dq-cards").wait_for(timeout=15000)
            return bubble

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not args.headful)
            ctx = browser.new_context(viewport={"width": 1440, "height": 950})
            page = ctx.new_page()
            watch(page)
            try:
                page.goto(f"{base}/", wait_until="networkidle")
                page.wait_for_selector("#input", timeout=15000)
                page.select_option("#role", "本科生")

                section("1. 本周课表")
                bubble = ask(page, "本周课表")
                card = bubble.locator(".dq-cards .dq-card").first
                check("出现问数卡片", card.count() == 1)
                title = card.locator(".dq-title").inner_text()
                check("标题为本周课表", title == "本周课表", title)
                subtitle = card.locator(".dq-sub").inner_text()
                check("展示学期与周次",
                      "学年" in subtitle and "第" in subtitle and "周" in subtitle, subtitle)
                identity = card.locator(".dq-identity").inner_text()
                check("展示演示身份", "李知远" in identity and "20230001" in identity
                      and "本科生" in identity, identity)
                metrics = card.locator(".dq-metrics .dq-metric").all_inner_texts()
                check("三个指标", len(metrics) == 3, str(metrics))
                check("指标含课程/学分/天数",
                      any("课程" in m for m in metrics)
                      and any("学分" in m for m in metrics)
                      and any("天数" in m for m in metrics))
                headers = card.locator(".dq-table th").all_inner_texts()
                check("表头契约",
                      headers == ["星期", "时间", "课程", "地点", "教师"], str(headers))
                rows = card.locator(".dq-table tbody tr").count()
                check("课表有数据行", rows >= 4, str(rows))
                check("课表含高等数学", "高等数学" in card.inner_text())
                footnote = card.locator(".dq-footnote").inner_text()
                check("脚注标注演示数据", "演示数据" in footnote and "教务系统" in footnote,
                      footnote[:80])
                page.screenshot(path=str(shots / "dq-schedule.png"), full_page=True)

                section("2. 周次 / 星期过滤")
                bubble = ask(page, "第 3 周周一有什么课")
                card = bubble.locator(".dq-cards .dq-card").first
                check("标题为周一课表", card.locator(".dq-title").inner_text() == "周一课表")
                check("副标题含第 3 周", "第 3 周" in card.locator(".dq-sub").inner_text(),
                      card.locator(".dq-sub").inner_text())
                rows = card.locator(".dq-table tbody tr").count()
                check("过滤后两门课", rows == 2, str(rows))
                text = card.inner_text()
                check("保留高等数学与大学英语",
                      "高等数学" in text and "大学英语" in text and "数据结构" not in text)

                section("3. 我的成绩")
                bubble = ask(page, "我的成绩")
                card = bubble.locator(".dq-cards .dq-card").first
                check("标题为成绩单", card.locator(".dq-title").inner_text() == "我的成绩单")
                metrics = card.locator(".dq-metrics .dq-metric").all_inner_texts()
                check("成绩四个指标", len(metrics) == 4, str(metrics))
                check("加权平均 89.6", any("89.6" in m for m in metrics), str(metrics))
                rows = card.locator(".dq-table tbody tr").count()
                check("成绩 6 行", rows == 6, str(rows))
                check("成绩含数据结构", "数据结构" in card.inner_text())
                page.screenshot(path=str(shots / "dq-grades.png"), full_page=True)

                section("4. 一卡通")
                bubble = ask(page, "一卡通余额，最近 7 天")
                card = bubble.locator(".dq-cards .dq-card").first
                check("标题为一卡通账户", card.locator(".dq-title").inner_text() == "一卡通账户")
                metrics = card.locator(".dq-metrics .dq-metric").all_inner_texts()
                check("余额 ¥86.50", any("¥86.50" in m for m in metrics), str(metrics))
                headers = card.locator(".dq-table th").all_inner_texts()
                check("流水表头", headers == ["时间", "地点", "类型", "金额"], str(headers))
                check("流水有数据", card.locator(".dq-table tbody tr").count() > 0)
                page.screenshot(path=str(shots / "dq-card.png"), full_page=True)

                page.select_option("#role", "教职工")
                bubble = ask(page, "一卡通余额")
                card = bubble.locator(".dq-cards .dq-card").first
                metrics = card.locator(".dq-metrics .dq-metric").all_inner_texts()
                check("教职工余额 ¥152.30", any("¥152.30" in m for m in metrics), str(metrics))

                section("5. 无权限拒绝卡片")
                page.select_option("#role", "访客")
                bubble = ask(page, "我的课表")
                denied = bubble.locator(".dq-card.dq-denied")
                check("出现拒绝卡片", denied.count() == 1)
                text = denied.inner_text()
                check("拒绝原因说明无权", "无权" in text, text[:80])
                check("拒绝状态 403", "403" in text, text[:80])
                check("拒绝卡片给出登录提示", "登录" in text)
                check("回答主链路未中断", len(bubble.inner_text()) > len(text))
                page.screenshot(path=str(shots / "dq-denied.png"), full_page=True)

                section("6. 控制台")
                check("零 pageerror", not any(e.startswith("pageerror") for e in errors),
                      "; ".join(errors[:2]))
                check("零 console.error", not any(e.startswith("console.error") for e in errors),
                      "; ".join(errors[:2]))
            finally:
                ctx.close()
                browser.close()
    finally:
        stop_server(proc)

    print(f"\n结果：{PASSED} PASS / {FAILED} FAIL")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
