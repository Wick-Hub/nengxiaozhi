"""渠道×角色会话路由 · 浏览器端到端验收（任务 3.4 前端）。

真实 Chromium 覆盖 HTTP 层看不到的行为：

1. 渠道清单「路由」入口打开角色路由弹窗，五类身份卡片齐全，未启用的卡片禁用；
2. 启用「访客 / 教职工」：分别指定助手、专属欢迎语、引导问题与模块开关，
   保存后接口侧 ``routeConfig`` 正确落库；
3. 用户端默认访客：欢迎语/引导问题按角色覆写，关闭的模块（快捷方式、应用广场、
   客服/反馈）不渲染，推荐应用保留；
4. 切换身份为教职工：欢迎语与引导问题即时切换，应用广场/快捷方式/客服反馈恢复，
   推荐应用按路由关闭；
5. 提问请求携带该身份路由后的助手 ``agentId``；
6. 全程零 ``pageerror`` / ``console.error``。

**幂等性**：渠道编码固定 ``vf-ui-rt``、助手编码 ``vf-ui-rta-*``、应用名固定，
前后按前缀清理，可重复运行。

用法::

    python tools/verify_routes_ui.py
    python tools/verify_routes_ui.py --store mysql --headful

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
CHANNEL_CODE = "vf-ui-rt"
AGENT_PREFIX = "vf-ui-rta-"
APP_NAME = "路由验收应用"

THEME = {
    "primaryColor": "#3366ff",
    "assistantName": "路由小智",
    "avatarText": "路",
    "faq": ["渠道常见问题一？", "渠道常见问题二？"],
    "shortcuts": [
        {"icon": "🌐", "label": "校园网", "action": "ask", "content": "校园网怎么开通？"}
    ],
    "recommendedApps": [
        {
            "icon": "📚",
            "name": "图书馆",
            "desc": "借阅与续借",
            "url": "https://lib.example.edu.cn",
        }
    ],
    "serviceEntry": {
        "enabled": 1,
        "label": "联系客服",
        "url": "https://support.example.edu.cn",
    },
    "feedback": {"enabled": 1, "label": "意见反馈", "url": "/feedback"},
}


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


def _ensure_agent(cli, code: str, name: str) -> int:
    for item in _agent_items(cli):
        if item.get("agentCode") == code:
            return int(item["agentId"])
    resp = cli.post(
        "/api/admin/agents",
        json={"agentCode": code, "agentName": name, "agentType": "chat"},
    )
    assert resp.status_code == 200, resp.text
    return int(resp.json()["data"]["agentId"])


def _cleanup(cli) -> None:
    items = cli.get("/api/admin/channels").json()["data"]["items"]
    for item in items:
        if item["channelCode"].startswith(CHANNEL_CODE):
            cli.delete(f"/api/admin/channels/{item['channelId']}")
    for app in _app_items(cli):
        if app.get("appName") == APP_NAME:
            cli.delete(f"/api/admin/apps/{app['appId']}")
    for agent in _agent_items(cli):
        if str(agent.get("agentCode") or "").startswith(AGENT_PREFIX):
            cli.delete(f"/api/admin/agents/{agent['agentId']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="渠道角色路由浏览器验收（任务 3.4）")
    parser.add_argument("--port", type=int, default=8028, help="验收用服务端口")
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


def _run(base: str, shots: Path, *, cli, headful: bool) -> int:
    from playwright.sync_api import sync_playwright

    js_errors: list[str] = []
    channel_calls: list[str] = []

    a_id = _ensure_agent(cli, f"{AGENT_PREFIX}a", "路由验收助手甲")
    b_id = _ensure_agent(cli, f"{AGENT_PREFIX}b", "路由验收助手乙")
    channel = cli.post(
        "/api/admin/channels",
        json={
            "channelCode": CHANNEL_CODE,
            "channelName": "UI路由渠道",
            "defaultAgentId": a_id,
            "welcomeText": "渠道默认欢迎语",
            "themeConfig": THEME,
            "agentIds": [a_id, b_id],
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
            "agentId": a_id,
            "category": "事务办理",
            "description": "路由验收用",
            "iconUrl": "🧪",
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

        def on_response(resp) -> None:  # noqa: ANN001
            if "/api/channels/" in resp.url:
                channel_calls.append(resp.url)

        page.on("console", on_console)
        page.on("response", on_response)
        page.on("dialog", lambda d: d.accept())

        try:
            section("1. 后台入口与路由弹窗")
            page.goto(f"{base}/channels", wait_until="networkidle")
            page.wait_for_function(
                "document.getElementById('ch-note').textContent.includes('共')",
                timeout=15000,
            )
            row = page.locator("#ch-tbody tr").filter(
                has=page.locator(f'.doc-meta:text-is("{CHANNEL_CODE}")')
            ).first
            check("验收渠道出现在清单", row.count() == 1)
            row.locator(".op-btn", has_text="路由").click()
            page.wait_for_function(
                "document.getElementById('ch-route-modal').hidden === false"
            )
            check("路由弹窗打开", page.locator("#ch-route-modal").is_visible())
            check(
                "五类身份卡片齐全",
                page.locator("#ch-route-list .ch-route-card").count() == 5,
                str(page.locator("#ch-route-list .ch-route-card").count()),
            )
            guest_card = page.locator(
                '#ch-route-list .ch-route-card[data-role="访客"]'
            )
            staff_card = page.locator(
                '#ch-route-list .ch-route-card[data-role="教职工"]'
            )
            check(
                "未启用身份默认禁用表单",
                guest_card.locator("select[data-route-agent]").is_disabled(),
            )

            section("2. 配置访客 / 教职工路由")
            guest_card.locator("input[data-route-enabled]").check()
            check(
                "启用后表单解锁",
                guest_card.locator("select[data-route-agent]").is_enabled(),
            )
            guest_card.locator("select[data-route-agent]").select_option(str(a_id))
            guest_card.locator("input[data-route-welcome]").fill("访客欢迎语")
            guest_card.locator("textarea[data-route-guides]").fill(
                "校园网怎么开通？\n一卡通丢了怎么办？"
            )
            for key in ("appSquare", "shortcuts", "serviceEntry", "feedback"):
                guest_card.locator(f'input[data-route-module="{key}"]').uncheck()

            staff_card.locator("input[data-route-enabled]").check()
            staff_card.locator("select[data-route-agent]").select_option(str(b_id))
            staff_card.locator("input[data-route-welcome]").fill("教职工欢迎语")
            staff_card.locator("textarea[data-route-guides]").fill(
                "差旅怎么报销？\n体检怎么预约？"
            )
            staff_card.locator('input[data-route-module="recommendedApps"]').uncheck()
            page.screenshot(path=str(shots / "routes-modal.png"), full_page=True)

            page.click("#ch-route-save")
            page.wait_for_function(
                "document.getElementById('ch-route-modal').hidden === true",
                timeout=15000,
            )
            page.wait_for_timeout(500)
            detail = cli.get(f"/api/admin/channels").json()["data"]["items"]
            item = next(x for x in detail if x["channelCode"] == CHANNEL_CODE)
            routes = {r["role"]: r for r in item["routeConfig"]["routes"]}
            check(
                "路由落库：两个身份",
                set(routes) == {"访客", "教职工"},
                str(sorted(routes)),
            )
            check(
                "访客助手/欢迎语/引导问题正确",
                routes["访客"]["agentId"] == a_id
                and routes["访客"]["welcomeText"] == "访客欢迎语"
                and routes["访客"]["guideQuestions"]
                == ["校园网怎么开通？", "一卡通丢了怎么办？"],
                str(routes.get("访客"))[:160],
            )
            check(
                "访客模块开关正确",
                routes["访客"]["modules"]["appSquare"] == 0
                and routes["访客"]["modules"]["shortcuts"] == 0
                and routes["访客"]["modules"]["recommendedApps"] == 1
                and routes["访客"]["modules"]["serviceEntry"] == 0
                and routes["访客"]["modules"]["feedback"] == 0,
                str(routes["访客"]["modules"]),
            )
            check(
                "教职工助手/模块正确",
                routes["教职工"]["agentId"] == b_id
                and routes["教职工"]["modules"]["recommendedApps"] == 0
                and routes["教职工"]["modules"]["appSquare"] == 1,
                str(routes.get("教职工"))[:160],
            )

            section("3. 用户端访客首屏")
            page.goto(base, wait_until="networkidle")
            page.evaluate("localStorage.setItem('nxz.role', '访客')")
            channel_calls.clear()
            page.goto(f"{base}/?channel={CHANNEL_CODE}", wait_until="networkidle")
            page.wait_for_selector(".welcome", timeout=15000)
            check(
                "欢迎语按访客路由覆写",
                page.locator(".welcome p").first.inner_text() == "访客欢迎语",
                page.locator(".welcome p").first.inner_text(),
            )
            check(
                "引导问题使用访客配置",
                page.locator(".samples .sample").count() == 2
                and "校园网怎么开通？" in page.locator(".samples").inner_text(),
                str(page.locator(".samples .sample").count()),
            )
            check(
                "访客关闭快捷方式模块",
                page.locator(".shortcut-row .shortcut").count() == 0,
            )
            check(
                "访客保留推荐应用",
                page.locator(".portal-app").count() == 1
                and "图书馆" in page.locator(".portal-app").inner_text(),
            )
            check("访客关闭应用广场", page.locator(".app-square").count() == 0)
            check("访客关闭客服/反馈入口", page.locator(".portal-link").count() == 0)
            check(
                "首屏请求携带 role=访客",
                any("role=%E8%AE%BF%E5%AE%A2" in u for u in channel_calls),
                str(channel_calls[-2:]),
            )
            page.screenshot(path=str(shots / "routes-guest.png"), full_page=True)

            section("4. 切换身份为教职工")
            channel_calls.clear()
            page.select_option("#role", "教职工")
            page.wait_for_function(
                "document.querySelector('.welcome p').textContent === '教职工欢迎语'",
                timeout=15000,
            )
            check(
                "欢迎语切换为教职工",
                page.locator(".welcome p").first.inner_text() == "教职工欢迎语",
            )
            check(
                "引导问题切换为教职工",
                page.locator(".samples .sample").count() == 2
                and "差旅怎么报销？" in page.locator(".samples").inner_text(),
                page.locator(".samples").inner_text()[:80],
            )
            check(
                "切换身份重新请求渠道配置",
                any("role=%E6%95%99%E8%81%8C%E5%B7%A5" in u for u in channel_calls),
                str(channel_calls[-2:]),
            )
            check(
                "教职工关闭推荐应用",
                page.locator(".portal-app").count() == 0,
            )
            check(
                "教职工恢复应用广场",
                page.locator(".app-square").count() == 1
                and APP_NAME in page.locator(".app-square").inner_text(),
                str(page.locator(".app-square").count()),
            )
            check(
                "教职工恢复快捷方式",
                page.locator(".shortcut-row .shortcut").count() == 1,
            )
            check(
                "教职工恢复客服/反馈入口",
                page.locator(".portal-link").count() == 2,
                str(page.locator(".portal-link").count()),
            )
            page.screenshot(path=str(shots / "routes-staff.png"), full_page=True)

            with page.expect_request(
                lambda r: "/api/chat/stream" in r.url and r.method == "POST"
            ) as req_info:
                page.fill("#input", "差旅怎么报销？")
                page.click("#send")
            body = req_info.value.post_data_json or {}
            check(
                "提问携带教职工路由助手 agentId",
                str(body.get("agentId")) == str(b_id),
                f"{body.get('agentId')} == {b_id}",
            )
            check("提问 role=教职工", body.get("role") == "教职工",
                  str(body.get("role")))
            page.wait_for_selector(".msg.ai:not(.thinking) .bubble", timeout=30000)
            check(
                "作答正常渲染",
                len(page.locator(".msg.ai:not(.thinking) .bubble").last.inner_text()) > 0,
            )

            section("5. 零 JS 错误")
            check("无 pageerror / console.error", not js_errors,
                  "\n         ".join(js_errors[:5]))
        finally:
            browser.close()

    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
