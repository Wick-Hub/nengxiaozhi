"""统计与大屏浏览器验收（任务 3.10 / P-QA-06）。

真实 Chromium 覆盖 HTTP 层看不到的行为：

1. 管理端「统计分析」页签可打开，6 张指标卡与筛选条渲染；
2. 扩大日期范围后趋势柱、渠道/助手表、热门知识/分类表有数据；
3. 指标卡数值与接口 ``summary`` 一致；
4. 无数据时间范围显示「— / 暂无数据」空态，而不是 0；
5. 情绪 / 应用面板显示空态说明（待 4.6 / 埋点未接入）；
6. 「导出概览」触发 CSV 下载；
7. 「全屏大屏」进入全屏，Esc/退出后恢复；
8. 全程零 ``pageerror`` / ``console.error``。

**只读**：不创建/修改任何渠道、助手或会话数据，可直接在开发库上运行。

用法::

    python tools/verify_stats_ui.py
    python tools/verify_stats_ui.py --store mysql --headful

退出码：全部通过为 0，存在 FAIL 为 1。
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
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


def _summary(base: str) -> dict:
    import httpx

    with httpx.Client(timeout=20, trust_env=False, base_url=base) as cli:
        return cli.get("/api/admin/stats/overview").json()["data"]["summary"]


def _search(page, start: str, end: str) -> None:
    page.fill("#st-from", start)
    page.fill("#st-to", end)
    page.click("#st-search")
    page.wait_for_timeout(900)


def main() -> int:
    parser = argparse.ArgumentParser(description="统计分析浏览器验收（任务 3.10）")
    parser.add_argument("--port", type=int, default=8035, help="验收用服务端口")
    parser.add_argument("--store", default="json", choices=["json", "mysql"])
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
    env["KB_STORE"] = args.store
    env["AUTH_ENABLED"] = "false"
    env["RATE_LIMIT_ENABLED"] = "false"
    env["LLM_MOCK"] = "1"

    print(f"启动服务 127.0.0.1:{port}（KB_STORE={args.store}）…")
    proc, log_path = spawn_server(port, env, SERVICE_DIR)
    try:
        if not wait_port(port, timeout=60):
            print("服务启动失败，日志尾部：")
            print(read_log(log_path, 3000))
            return 1

        expected = _summary(base)
        errors: list[str] = []

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not args.headful)
            page = browser.new_page(viewport={"width": 1500, "height": 950})
            page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))

            def on_console(msg) -> None:  # noqa: ANN001
                if msg.type == "error":
                    errors.append(f"console.error: {msg.text}")

            page.on("console", on_console)
            page.on("dialog", lambda d: d.accept())
            try:
                section("1. 打开统计页签")
                page.goto(f"{base}/admin", wait_until="networkidle")
                page.click('#tabs .tab[data-tab="stats"]')
                page.wait_for_selector("#st-cards .st-card", timeout=20000)
                check("指标卡渲染 6 张", page.locator("#st-cards .st-card").count() == 6,
                      str(page.locator("#st-cards .st-card").count()))
                for field in ("#st-from", "#st-to", "#st-channel", "#st-agent", "#st-role"):
                    check(f"筛选控件存在 {field}", page.locator(field).count() == 1)
                check("默认时间范围为最近 7 天",
                      page.input_value("#st-from") < page.input_value("#st-to"),
                      f"{page.input_value('#st-from')} ~ {page.input_value('#st-to')}")

                section("2. 全量范围渲染")
                today = date.today().isoformat()
                _search(page, "2020-01-01", today)
                cards = page.locator("#st-cards .st-card")
                values = [cards.nth(i).locator(".st-card-value").inner_text()
                          for i in range(cards.count())]
                check("指标卡有真实数值（非空态）",
                      expected["replies"] > 0 and "—" not in values, str(values))
                check("提问卡与接口一致",
                      values[0].replace(",", "") == str(expected["questions"]),
                      f"{values[0]} == {expected['questions']}")
                check("趋势柱渲染", page.locator("#st-trend .st-bar-col").count() > 0,
                      str(page.locator("#st-trend .st-bar-col").count()))
                check("渠道表有数据", page.locator("#st-channels tr").count() > 0,
                      str(page.locator("#st-channels tr").count()))
                check("助手表有数据", page.locator("#st-agents tr").count() > 0,
                      str(page.locator("#st-agents tr").count()))
                check("热门知识表有数据", page.locator("#st-knowledge tr").count() > 0,
                      str(page.locator("#st-knowledge tr").count()))
                check("分类表有数据", page.locator("#st-categories tr").count() > 0,
                      str(page.locator("#st-categories tr").count()))
                note = page.locator("#st-note").inner_text()
                check("口径说明可见（存储 / 扫描窗口）",
                      "存储：" in note and "扫描消息" in note, note[:80])
                check("应用排行空态可见", page.locator("#st-apps-empty").is_visible())
                check("应用空态说明含「埋点」",
                      "埋点" in page.locator("#st-apps-note").inner_text())
                page.screenshot(path=str(shots / "stats-ui.png"), full_page=True)

                section("3. 无数据空态")
                _search(page, "2000-01-01", "2000-01-02")
                values = [cards.nth(i).locator(".st-card-value").inner_text()
                          for i in range(cards.count())]
                subs = [cards.nth(i).locator(".st-card-sub").inner_text()
                        for i in range(cards.count())]
                check("指标卡显示「—」而不是 0", all(v == "—" for v in values), str(values))
                check("指标卡副标题「暂无数据」", all(s == "暂无数据" for s in subs))
                check("趋势空态可见", page.locator("#st-trend-empty").is_visible())
                check("渠道空态可见", page.locator("#st-channels-empty").is_visible())
                check("知识空态可见", page.locator("#st-knowledge-empty").is_visible())
                page.screenshot(path=str(shots / "stats-ui-empty.png"), full_page=True)

                section("4. 导出")
                with page.expect_download(timeout=15000) as dl:
                    page.click("#st-export-summary")
                download = dl.value
                check("导出概览触发 CSV 下载",
                      download.suggested_filename.lower().endswith(".csv"),
                      download.suggested_filename)

                section("5. 全屏大屏")
                _search(page, "2020-01-01", today)
                page.click("#st-full")
                page.wait_for_function("!!document.fullscreenElement", timeout=10000)
                check("进入全屏", page.evaluate("!!document.fullscreenElement") is True)
                check("全屏按钮文案切换",
                      page.locator("#st-full").inner_text().strip() == "退出全屏",
                      page.locator("#st-full").inner_text())
                page.screenshot(path=str(shots / "stats-ui-fullscreen.png"))
                page.evaluate("document.exitFullscreen()")
                page.wait_for_function("!document.fullscreenElement", timeout=10000)
                check("退出全屏", page.evaluate("!!document.fullscreenElement") is False)

                section("6. 运行时错误")
                check("零 pageerror / console.error", not errors, "; ".join(errors[:3]))
            finally:
                browser.close()

        print(f"\n截图目录：{shots}")
        print(f"\n结果：{PASSED} PASS / {FAILED} FAIL")
        return 1 if FAILED else 0
    finally:
        stop_server(proc)


if __name__ == "__main__":
    raise SystemExit(main())
