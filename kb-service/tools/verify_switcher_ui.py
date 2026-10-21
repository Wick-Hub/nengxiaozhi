"""多智能体切换 · 浏览器端到端验收（任务 3.8 / P-QA-06）。

真实 Chromium 覆盖 HTTP 层看不到的行为：

1. 管理端助手弹窗回填并保存场景引导问题（``#ag-f-guides``）；
2. 渠道首屏渲染助手矩阵，默认助手按 3.4 角色路由展示引导问题；
3. 点击矩阵切换助手：欢迎语/引导问题跟随、当前会话被丢弃（上下文不混用）；
4. 切换后提问携带新助手 ``agentId``；
5. 顶栏 ``#agent-select`` 在会话中途可切换，切回默认助手恢复路由引导问题；
6. 应用广场智能体卡片同样触发助手切换；
7. 切换身份后仅本科生可见的助手从矩阵/切换器消失，并回退默认助手；
8. 无渠道参数的根门户按公开助手清单渲染矩阵并可切换；
9. 全程零 ``pageerror`` / ``console.error``。

**幂等性**：渠道/助手编码统一 ``vf-ui-sw*``，应用名固定，前后按前缀清理。

用法::

    python tools/verify_switcher_ui.py
    python tools/verify_switcher_ui.py --store mysql --headful

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
PREFIX = "vf-ui-sw"
CHANNEL_CODE = PREFIX
APP_NAME = "切换验收应用"

GUIDES_A = ["学费什么时候缴纳？", "差旅报销需要准备哪些材料？"]
GUIDES_A_EDIT = GUIDES_A + ["科研经费怎么入账？"]
GUIDES_B = ["研究生奖学金怎么申请？", "导师双选什么时候开始？"]
ROUTE_GUEST = ["本科生路由问题一？"]
ROUTE_STAFF = ["教职工路由问题一？"]


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


def _agent_items(cli) -> list[dict]:
    data = cli.get("/api/admin/agents").json().get("data") or {}
    return data.get("items") if isinstance(data, dict) else data


def _app_items(cli) -> list[dict]:
    data = cli.get("/api/admin/apps").json().get("data") or {}
    return data.get("items") if isinstance(data, dict) else data


def _cleanup(cli) -> None:
    for item in cli.get("/api/admin/channels").json()["data"]["items"]:
        if str(item["channelCode"]).startswith(PREFIX):
            cli.delete(f"/api/admin/channels/{item['channelId']}")
    for app in _app_items(cli):
        if app.get("appName") == APP_NAME:
            cli.delete(f"/api/admin/apps/{app['appId']}")
    for agent in _agent_items(cli):
        if str(agent.get("agentCode") or "").startswith(PREFIX):
            cli.delete(f"/api/admin/agents/{agent['agentId']}")


def _create_agent(cli, code: str, name: str, **fields) -> int:
    resp = cli.post(
        "/api/admin/agents",
        json={"agentCode": code, "agentName": name, "agentType": "chat", **fields},
    )
    assert resp.status_code == 200, resp.text
    return int(resp.json()["data"]["agentId"])


def main() -> int:
    parser = argparse.ArgumentParser(description="多智能体切换浏览器验收（任务 3.8）")
    parser.add_argument("--port", type=int, default=8030, help="验收用服务端口")
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
                rc = _run(base, shots, cli=cli, headful=args.headful)
            finally:
                _cleanup(cli)
            print(f"\n截图目录：{shots}")
            print(f"\n结果：{PASSED} PASS / {FAILED} FAIL")
            return rc
    finally:
        stop_server(proc)


def _run(base: str, shots: Path, *, cli, headful: bool) -> int:
    from playwright.sync_api import sync_playwright

    js_errors: list[str] = []
    agent_calls: list[str] = []

    a_id = _create_agent(
        cli, f"{PREFIX}a", "切换验收助手甲",
        description="全校通用助手", iconUrl="🧪", guideQuestions=GUIDES_A,
    )
    b_id = _create_agent(
        cli, f"{PREFIX}b", "切换验收助手乙",
        description="本科生专属助手", iconUrl="🎓",
        visibleRoles=["本科生"], guideQuestions=GUIDES_B,
    )
    channel = cli.post(
        "/api/admin/channels",
        json={
            "channelCode": CHANNEL_CODE,
            "channelName": "UI切换渠道",
            "defaultAgentId": a_id,
            "welcomeText": "渠道通用欢迎语",
            "agentIds": [a_id, b_id],
            "themeConfig": {"assistantName": "切换小智", "avatarText": "切"},
            "routeConfig": {
                "routes": [
                    {
                        "role": "本科生",
                        "agentId": a_id,
                        "welcomeText": "本科生欢迎语",
                        "guideQuestions": ROUTE_GUEST,
                    },
                    {
                        "role": "教职工",
                        "agentId": a_id,
                        "welcomeText": "教职工欢迎语",
                        "guideQuestions": ROUTE_STAFF,
                    },
                ]
            },
            "needAuth": 0,
            "status": 1,
        },
    )
    assert channel.status_code == 200, channel.text
    app = cli.post(
        "/api/admin/apps",
        json={
            "appName": APP_NAME,
            "sourceType": "agent",
            "agentId": b_id,
            "category": "学业",
            "description": "切换验收用",
            "iconUrl": "🎓",
            "visibleRoles": ["全部"],
            "status": 1,
        },
    )
    assert app.status_code == 200, app.text

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headful)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.on("pageerror", lambda exc: js_errors.append(f"pageerror: {exc}"))

        def on_console(msg) -> None:  # noqa: ANN001
            if msg.type == "error":
                js_errors.append(f"console.error: {msg.text}")

        def on_request(req) -> None:  # noqa: ANN001
            if "/api/agents?" in req.url:
                agent_calls.append(req.url)

        page.on("console", on_console)
        page.on("request", on_request)
        page.on("dialog", lambda d: d.accept())

        try:
            section("1. 管理端编辑场景引导问题")
            page.goto(f"{base}/admin", wait_until="networkidle")
            page.click('#tabs .tab[data-tab="agents"]')
            page.wait_for_selector("#ag-scenes .scene-card", timeout=15000)
            row = page.locator("#ag-tbody tr", has_text="切换验收助手甲").first
            check("验收助手出现在管理列表", row.count() == 1)
            row.locator(".op-btn", has_text="编辑").click()
            page.wait_for_function(
                "document.getElementById('ag-modal').hidden === false", timeout=10000
            )
            guides_value = page.input_value("#ag-f-guides")
            check(
                "编辑弹窗回填场景引导问题",
                guides_value.splitlines() == GUIDES_A,
                repr(guides_value),
            )
            page.fill("#ag-f-guides", "\n".join(GUIDES_A_EDIT))
            page.click("#ag-save")
            page.wait_for_function(
                "document.getElementById('ag-modal').hidden === true", timeout=15000
            )
            page.wait_for_timeout(600)
            saved = cli.get(f"/api/admin/agents/{a_id}").json()["data"]
            check("引导问题保存到接口", saved["guideQuestions"] == GUIDES_A_EDIT,
                  str(saved.get("guideQuestions")))

            section("2. 渠道首屏助手矩阵与路由引导")
            page.goto(base, wait_until="networkidle")
            page.evaluate("localStorage.setItem('nxz.role', '本科生')")
            agent_calls.clear()
            page.goto(f"{base}/?channel={CHANNEL_CODE}", wait_until="networkidle")
            page.wait_for_selector(".welcome", timeout=15000)
            check("助手矩阵渲染两张卡片",
                  page.locator(".agent-matrix .agent-chip").count() == 2,
                  str(page.locator(".agent-matrix .agent-chip").count()))
            check(
                "默认助手卡片为选中态",
                page.locator(".agent-chip.active").inner_text().find("切换验收助手甲") >= 0,
                page.locator(".agent-chip.active").inner_text()[:40],
            )
            check(
                "默认助手沿用角色路由引导问题",
                page.locator(".samples .sample").count() == len(ROUTE_GUEST)
                and ROUTE_GUEST[0] in page.locator(".samples").inner_text(),
                page.locator(".samples").inner_text()[:80],
            )
            check(
                "顶栏切换器可见且列出两个助手",
                page.locator("#agent-select").is_visible()
                and page.locator("#agent-select option").count() == 2,
                str(page.locator("#agent-select option").count()),
            )
            check(
                "首屏请求公开助手矩阵",
                any("role=%E6%9C%AC%E7%A7%91%E7%94%9F" in u for u in agent_calls),
                str(agent_calls[-2:]),
            )
            page.screenshot(path=str(shots / "switcher-matrix.png"), full_page=True)

            section("3. 点击矩阵切换助手")
            page.locator(".agent-chip", has_text="切换验收助手乙").click()
            page.wait_for_function(
                "document.querySelector('.welcome h2').textContent.includes('切换验收助手乙')",
                timeout=10000,
            )
            check("欢迎标题切换为助手乙",
                  "切换验收助手乙" in page.locator(".welcome h2").inner_text())
            check("欢迎描述使用助手乙简介",
                  page.locator(".welcome p").first.inner_text() == "本科生专属助手")
            check(
                "引导问题切换为助手乙场景问题",
                page.locator(".samples .sample").count() == len(GUIDES_B)
                and GUIDES_B[0] in page.locator(".samples").inner_text(),
                page.locator(".samples").inner_text()[:80],
            )
            check(
                "矩阵选中态跟随切换",
                "切换验收助手乙" in page.locator(".agent-chip.active").inner_text(),
            )
            check("顶栏切换器回显助手乙",
                  page.locator("#agent-select").input_value() == str(b_id),
                  page.locator("#agent-select").input_value())
            check("切换后旧会话被清空", page.locator(".msg").count() == 0,
                  str(page.locator(".msg").count()))
            page.screenshot(path=str(shots / "switcher-b.png"), full_page=True)

            with page.expect_request(
                lambda r: "/api/chat/stream" in r.url and r.method == "POST"
            ) as req_info:
                page.fill("#input", GUIDES_B[0])
                page.click("#send")
            body = req_info.value.post_data_json or {}
            check("提问携带助手乙 agentId", str(body.get("agentId")) == str(b_id),
                  f"{body.get('agentId')} == {b_id}")
            check("提问 role=本科生", body.get("role") == "本科生", str(body.get("role")))
            page.wait_for_selector(".msg.ai:not(.thinking) .bubble", timeout=30000)
            page.wait_for_function("localStorage.getItem('nxz.sessionId')", timeout=10000)

            section("4. 顶栏切换器在会话中途切换")
            page.select_option("#agent-select", str(a_id))
            page.wait_for_function(
                "document.querySelector('.welcome h2').textContent.includes('切换小智')",
                timeout=10000,
            )
            check("切回默认助手提示主题助手名",
                  page.locator(".welcome h2").inner_text() == "切换小智",
                  page.locator(".welcome h2").inner_text())
            check("切回默认助手恢复角色路由引导",
                  page.locator(".samples .sample").count() == len(ROUTE_GUEST)
                  and ROUTE_GUEST[0] in page.locator(".samples").inner_text(),
                  page.locator(".samples").inner_text()[:80])
            check("会话中途切换同样清空上下文", page.locator(".msg").count() == 0,
                  str(page.locator(".msg").count()))
            check("切换后本机会话 id 被移除",
                  page.evaluate("localStorage.getItem('nxz.sessionId')") is None,
                  str(page.evaluate("localStorage.getItem('nxz.sessionId')")))
            with page.expect_request(
                lambda r: "/api/chat/stream" in r.url and r.method == "POST"
            ) as req_info:
                page.fill("#input", "校园网怎么开通？")
                page.click("#send")
            body = req_info.value.post_data_json or {}
            check("切换后提问携带默认助手 agentId",
                  str(body.get("agentId")) == str(a_id),
                  f"{body.get('agentId')} == {a_id}")
            page.wait_for_selector(".msg.ai:not(.thinking) .bubble", timeout=30000)

            section("5. 应用广场切换助手")
            page.click("#new-chat")
            page.wait_for_selector(".welcome", timeout=10000)
            app_card = page.locator(".app-square .app-card", has_text=APP_NAME).first
            check("应用广场出现智能体应用", app_card.count() == 1)
            app_card.click()
            page.wait_for_function(
                "document.querySelector('.welcome h2').textContent.includes('切换验收助手乙')",
                timeout=10000,
            )
            check("应用广场点击后切到助手乙",
                  "切换验收助手乙" in page.locator(".welcome h2").inner_text())
            check("应用广场切换同步场景引导",
                  page.locator(".samples .sample").count() == len(GUIDES_B)
                  and GUIDES_B[1] in page.locator(".samples").inner_text(),
                  page.locator(".samples").inner_text()[:80])

            section("6. 切换身份后不可见助手收敛")
            page.select_option("#role", "教职工")
            page.wait_for_function(
                "document.querySelector('.welcome p')"
                ".textContent.includes('教职工欢迎语')", timeout=15000,
            )
            check("欢迎语切换为教职工路由",
                  page.locator(".welcome p").first.inner_text() == "教职工欢迎语")
            check("角色路由引导问题生效",
                  page.locator(".samples .sample").count() == len(ROUTE_STAFF)
                  and ROUTE_STAFF[0] in page.locator(".samples").inner_text(),
                  page.locator(".samples").inner_text()[:80])
            check("仅剩一张助手卡片时矩阵隐藏",
                  page.locator(".agent-matrix-box").count() == 0,
                  str(page.locator(".agent-chip").count()))
            check("顶栏切换器随之隐藏", page.locator("#agent-select").is_hidden())
            check("切换身份后回到默认助手",
                  "切换小智" in page.locator(".welcome h2").inner_text(),
                  page.locator(".welcome h2").inner_text())
            page.screenshot(path=str(shots / "switcher-staff.png"), full_page=True)

            section("7. 根门户公开助手矩阵")
            page.goto(base, wait_until="networkidle")
            page.evaluate("localStorage.setItem('nxz.role', '本科生')")
            agent_calls.clear()
            page.goto(base, wait_until="networkidle")
            page.wait_for_selector(".welcome", timeout=15000)
            page.wait_for_selector(".agent-matrix .agent-chip", timeout=15000)
            b_chip = page.locator(".agent-chip", has_text="切换验收助手乙").first
            check("根门户出现公开助手矩阵", b_chip.count() == 1)
            check(
                "根门户按身份请求 /api/agents",
                any("role=%E6%9C%AC%E7%A7%91%E7%94%9F" in u for u in agent_calls),
                str(agent_calls[-2:]),
            )
            b_chip.click()
            page.wait_for_function(
                "document.querySelector('.welcome h2').textContent.includes('切换验收助手乙')",
                timeout=10000,
            )
            check("根门户切换助手生效",
                  "切换验收助手乙" in page.locator(".welcome h2").inner_text())
            check("根门户引导问题跟随助手",
                  page.locator(".samples .sample").count() == len(GUIDES_B)
                  and GUIDES_B[0] in page.locator(".samples").inner_text(),
                  page.locator(".samples").inner_text()[:80])
            page.screenshot(path=str(shots / "switcher-root.png"), full_page=True)

            section("8. 移动端 / 智能硬件布局")
            page.evaluate("localStorage.setItem('nxz.role', '本科生')")
            page.set_viewport_size({"width": 390, "height": 844})
            page.goto(f"{base}/?channel={CHANNEL_CODE}", wait_until="networkidle")
            page.wait_for_selector(".welcome", timeout=15000)
            check("移动端矩阵两张卡片",
                  page.locator(".agent-chip").count() == 2,
                  str(page.locator(".agent-chip").count()))
            check("移动端顶栏切换器可用", page.locator("#agent-select").is_visible())
            check("移动端无横向溢出",
                  page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"))
            page.screenshot(path=str(shots / "switcher-mobile.png"), full_page=True)

            page.set_viewport_size({"width": 1280, "height": 800})
            page.goto(f"{base}/?channel={CHANNEL_CODE}&kiosk=1", wait_until="networkidle")
            page.wait_for_selector(".welcome", timeout=15000)
            check("kiosk 模式生效",
                  page.evaluate("document.body.classList.contains('kiosk-mode')"))
            check("kiosk 矩阵两张卡片", page.locator(".agent-chip").count() == 2,
                  str(page.locator(".agent-chip").count()))
            check("kiosk 顶栏切换器可用", page.locator("#agent-select").is_visible())
            check("kiosk 无横向溢出",
                  page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"))
            page.screenshot(path=str(shots / "switcher-kiosk.png"), full_page=True)

            section("9. 零 JS 错误")
            check("无 pageerror / console.error", not js_errors,
                  "\n         ".join(js_errors[:5]))
        finally:
            browser.close()

    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
