"""应用广场 · 浏览器端到端验收（任务 3.3 / M-C-05·06 前端）。

真实 Chromium 覆盖 HTTP 层看不到的行为：

1. 后台顶栏「应用广场」入口可达，独立页不占页签；
2. 新建智能体应用：选择助手、分类、图标、排序，按具体角色授权；
3. 新建外链应用：来源切换后链接字段出现，保存后列表可点开外链；
4. 用户端广场：切换身份后只显示有权限的应用；智能体卡片可选中，
   外链卡片带安全 href；
5. 编辑 / 下架 / 删除后，用户端广场同步变化；
6. 全程零 ``pageerror`` / ``console.error``。

**幂等性**：验收应用名固定 ``vf-ui-app-*``，前后按名称前缀清理，可重复运行。

用法::

    python tools/verify_apps_ui.py
    python tools/verify_apps_ui.py --store mysql --headful

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
os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH", r"C:\Users\wick\.workbuddy-ai\binaries\ms-playwright"
)

try:  # pragma: no cover - 仅影响交互输出
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

PASSED = 0
FAILED = 0
APP_PREFIX = "vf-ui-app-"


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


def _client(base: str):
    import httpx

    return httpx.Client(timeout=20, trust_env=False, base_url=base)


def _cleanup(cli) -> None:
    items = cli.get("/api/admin/apps").json()["data"]["items"]
    for item in items:
        if str(item.get("appName") or "").startswith(APP_PREFIX):
            cli.delete(f"/api/admin/apps/{item['appId']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="应用广场浏览器验收（任务 3.3）")
    parser.add_argument("--port", type=int, default=8027, help="验收用服务端口")
    parser.add_argument("--store", default="json", choices=["json", "mysql"])
    parser.add_argument("--headful", action="store_true", help="显示浏览器窗口（调试用）")
    parser.add_argument("--shots-dir", default="", help="截图目录（默认 kb-service/artifacts）")
    args = parser.parse_args()

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
        with _client(base) as cli:
            _cleanup(cli)
            try:
                code = _run(base, shots, cli=cli, headful=args.headful)
            finally:
                _cleanup(cli)
            print(f"\n截图目录：{shots}")
            print(f"\n结果：{PASSED} PASS / {FAILED} FAIL")
            return code
    finally:
        stop_server(proc)


def _row(page, name: str):  # noqa: ANN001
    return page.locator("#ap-tbody tr", has_text=name)


def _create_agent_app(page, name: str, *, staff_only: bool, open_modal: bool = True) -> str:
    if open_modal:
        page.click("#ap-new")
        page.wait_for_function("document.getElementById('ap-modal').hidden === false")
    page.fill("#ap-f-name", name)
    page.fill("#ap-f-category", "事务办理")
    page.fill("#ap-f-icon", "🤖")
    page.fill("#ap-f-desc", "UI 验收智能体应用")
    page.fill("#ap-f-sort", "5")
    if staff_only:
        page.uncheck('#ap-f-roles input[value="全部"]')
        page.check('#ap-f-roles input[value="教职工"]')
    agent_name = page.locator("#ap-f-agent option").first.inner_text()
    page.click("#ap-save")
    page.wait_for_function("document.getElementById('ap-modal').hidden === true", timeout=10000)
    page.wait_for_function(
        f"document.getElementById('ap-tbody').textContent.includes({name!r})",
        timeout=10000,
    )
    return agent_name


def _run(base: str, shots: Path, *, cli, headful: bool) -> int:
    from playwright.sync_api import sync_playwright

    js_errors: list[str] = []
    agent_app = f"{APP_PREFIX}agent"
    link_app = f"{APP_PREFIX}link"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headful)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.on("pageerror", lambda exc: js_errors.append(f"pageerror: {exc}"))

        def on_console(msg) -> None:  # noqa: ANN001
            if msg.type == "error":
                js_errors.append(f"console.error: {msg.text}")

        page.on("console", on_console)
        page.on("dialog", lambda d: d.accept())

        try:
            section("1. 入口与初始状态")
            page.goto(f"{base}/admin", wait_until="networkidle")
            entry = page.locator('a[href="/apps"]')
            check("后台顶栏有「应用广场」入口", entry.count() == 1)
            check("入口文案正确", entry.inner_text().strip() == "应用广场")
            entry.click()
            page.wait_for_url("**/apps", timeout=10000)
            page.wait_for_function(
                "document.getElementById('ap-note').textContent.includes('共')",
                timeout=15000,
            )
            check("应用页可打开且清单加载完成",
                  "共" in page.locator("#ap-note").inner_text(),
                  page.locator("#ap-note").inner_text())
            check("页签未混入管理后台（独立页）", page.locator("#tabs").count() == 0)
            check("统计卡片显示应用总数",
                  page.locator("#ap-count").inner_text().isdigit())
            page.screenshot(path=str(shots / "apps-list.png"), full_page=True)

            section("2. 新建智能体应用（按角色授权）")
            page.click("#ap-new")
            page.wait_for_function("document.getElementById('ap-modal').hidden === false")
            check("新建弹窗打开", page.locator("#ap-modal").is_visible())
            check("智能体下拉已渲染",
                  page.locator("#ap-f-agent option").count() > 0)
            check("角色复选框已渲染",
                  page.locator("#ap-f-roles input").count() >= 6)
            agent_name = _create_agent_app(page, agent_app, staff_only=True, open_modal=False)
            row = _row(page, agent_app)
            check("智能体应用出现在列表", row.count() == 1)
            check("来源列显示绑定助手",
                  agent_name in row.inner_text(), row.inner_text()[:80])
            check("可见角色列显示「教职工」",
                  "教职工" in row.inner_text() and "全部" not in row.inner_text())
            page.screenshot(path=str(shots / "apps-agent-created.png"), full_page=True)

            section("3. 新建外链应用")
            page.click("#ap-new")
            page.wait_for_function("document.getElementById('ap-modal').hidden === false")
            page.fill("#ap-f-name", link_app)
            page.select_option("#ap-f-source", "link")
            check("切到外链来源后链接字段出现",
                  page.locator("#ap-f-link").is_visible())
            check("绑定助手字段隐藏",
                  not page.locator("#ap-f-agent").is_visible())
            page.fill("#ap-f-link", "https://ehall.example.edu.cn/apps")
            page.fill("#ap-f-category", "信息服务")
            page.fill("#ap-f-icon", "🏛")
            page.fill("#ap-f-sort", "1")
            page.click("#ap-save")
            page.wait_for_function("document.getElementById('ap-modal').hidden === true", timeout=10000)
            page.wait_for_function(
                f"document.getElementById('ap-tbody').textContent.includes({link_app!r})",
                timeout=10000,
            )
            link_row = _row(page, link_app)
            check("外链应用出现在列表", link_row.count() == 1)
            link_href = page.locator("#ap-tbody a", has_text="ehall.example.edu.cn").first
            check("外链地址可点击且协议为 https",
                  link_href.get_attribute("href").startswith("https://"),
                  str(link_href.get_attribute("href")))
            check("清单按排序号排列（外链在前）",
                  page.locator("#ap-tbody tr").first.inner_text().find(link_app) >= 0)
            page.screenshot(path=str(shots / "apps-two.png"), full_page=True)

            section("4. 用户端广场按身份过滤")
            chat = browser.new_page(viewport={"width": 1280, "height": 900})
            chat.on("pageerror", lambda exc: js_errors.append(f"pageerror(chat): {exc}"))
            chat.on("console", on_console)
            chat.goto(f"{base}/", wait_until="networkidle")
            chat.wait_for_selector(".app-square", timeout=15000)
            check("访客首屏展示应用广场", chat.locator(".app-square").count() == 1)
            check("访客只看到「全部」外链应用",
                  chat.locator(".app-card").count() == 1
                  and link_app in chat.locator(".app-square").inner_text())
            check("访客看不到教职工智能体应用",
                  agent_app not in chat.locator(".app-square").inner_text())

            chat.select_option("#role", "本科生")
            chat.wait_for_function(
                "document.querySelector('.app-square-head span')"
                ".textContent.includes('本科生')", timeout=10000
            )
            check("本科生仍只看到外链应用",
                  chat.locator(".app-card").count() == 1
                  and agent_app not in chat.locator(".app-square").inner_text())

            chat.select_option("#role", "教职工")
            chat.wait_for_function(
                "document.querySelectorAll('.app-card').length === 2", timeout=10000
            )
            square = chat.locator(".app-square").inner_text()
            check("教职工看到两个应用",
                  agent_app in square and link_app in square, square[:120])
            agent_card = chat.locator('.app-card[data-source="agent"]', has_text=agent_app)
            check("智能体应用渲染为助手卡片", agent_card.count() == 1)
            check("外链应用使用新窗口打开",
                  chat.locator('.app-card[data-source="link"]').get_attribute("target") == "_blank")
            agent_card.click()
            check("点击助手卡片出现选中态",
                  "active" in (agent_card.get_attribute("class") or ""))
            chat.screenshot(path=str(shots / "apps-chat-square.png"), full_page=True)

            section("5. 编辑 / 下架 / 删除同步到用户端")
            page.bring_to_front()
            _row(page, link_app).locator('[data-op="edit"]').click()
            page.wait_for_function("document.getElementById('ap-modal').hidden === false")
            page.fill("#ap-f-name", f"{link_app}-2")
            page.select_option("#ap-f-status", "0")
            page.click("#ap-save")
            page.wait_for_function("document.getElementById('ap-modal').hidden === true", timeout=10000)
            page.wait_for_function(
                f"document.getElementById('ap-tbody').textContent.includes({link_app + '-2'!r})",
                timeout=10000,
            )
            check("编辑名称与状态生效",
                  "下架" in _row(page, f"{link_app}-2").inner_text())
            chat.reload(wait_until="networkidle")
            chat.wait_for_selector(".app-square", timeout=15000)
            chat.select_option("#role", "教职工")
            chat.wait_for_function(
                "document.querySelectorAll('.app-card').length === 1", timeout=10000
            )
            check("下架外链后教职工只剩智能体应用",
                  agent_app in chat.locator(".app-square").inner_text()
                  and f"{link_app}-2" not in chat.locator(".app-square").inner_text())

            page.bring_to_front()
            _row(page, agent_app).locator('[data-op="toggle"]').click()
            page.wait_for_function(
                "(name) => Array.from(document.querySelectorAll('#ap-tbody tr'))"
                ".some(tr => tr.textContent.includes(name) && tr.textContent.includes('下架'))",
                arg=agent_app,
                timeout=10000,
            )
            check("智能体应用一键下架", "下架" in _row(page, agent_app).inner_text())
            chat.reload(wait_until="networkidle")
            chat.wait_for_function(
                "document.querySelectorAll('.app-card').length === 0", timeout=15000
            )
            check("全部应用下架后广场区块消失",
                  chat.locator(".app-square").count() == 0)

            page.bring_to_front()
            _row(page, agent_app).locator('[data-op="delete"]').click()
            page.wait_for_function(
                f"!document.getElementById('ap-tbody').textContent.includes({agent_app!r})",
                timeout=10000,
            )
            check("删除智能体应用后行消失", _row(page, agent_app).count() == 0)
            _row(page, f"{link_app}-2").locator('[data-op="delete"]').click()
            page.wait_for_function(
                "document.querySelectorAll('#ap-tbody tr').length === 0", timeout=10000
            )
            check("删除外链应用后清单为空", page.locator("#ap-tbody tr").count() == 0)
            page.screenshot(path=str(shots / "apps-empty.png"), full_page=True)

            check("全程无 JS 错误", not js_errors, " | ".join(js_errors[:3]))
            browser.close()
        finally:
            pass

    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
