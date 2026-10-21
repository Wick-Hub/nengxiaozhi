"""渠道分身页 · 浏览器端到端验收（任务 3.1 / 3.2 前端）。

真实 Chromium 覆盖 HTTP 层看不到的行为：

1. 后台顶栏「渠道分身」入口可达，独立页不占页签；
2. 新建渠道：表单（含门户个性化字段）→ 保存 → 列表出现，接口侧落库；
3. 嵌入代码：渠道 URL 带 ``?channel=``，iframe 带 ``embed=1``，
   浮窗页签出现 ``<script>`` 与主题色；
4. 编辑 / 启停 / 认证开关 / 一键复制：名称、欢迎语、主题色生效；认证徽章随
   一键开关切换；副本默认停用并继承门户配置；
5. 对话页分流与门户个性化：``/?channel=…&embed=1`` 显示 AI 形象、FAQ、
   快捷方式、推荐应用、客服/反馈入口，提问请求体携带渠道默认助手 ``agentId``；
6. 删除后列表与公开接口同步消失（副本由清理逻辑回收）；
7. 全程零 ``pageerror`` / ``console.error``。

**幂等性**：验收渠道编码固定 ``vf-ui-ch``，前后按编码清理，可重复运行。

用法::

    python tools/verify_channels_ui.py
    python tools/verify_channels_ui.py --store mysql --headful

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
CHANNEL_CODE = "vf-ui-ch"


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
    """按编码删除验收渠道（幂等）。"""
    items = cli.get("/api/admin/channels").json()["data"]["items"]
    for item in items:
        # 覆盖源渠道与复制出的 <code>-copy 副本
        if item["channelCode"].startswith(CHANNEL_CODE):
            cli.delete(f"/api/admin/channels/{item['channelId']}")


def _public(cli) -> dict | None:
    resp = cli.get(f"/api/channels/{CHANNEL_CODE}")
    return resp.json().get("data") if resp.status_code == 200 else None


def main() -> int:
    parser = argparse.ArgumentParser(description="渠道分身页浏览器验收（任务 3.1）")
    parser.add_argument("--port", type=int, default=8025, help="验收用服务端口")
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
    agent_id: int | None = None

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
            entry = page.locator('a[href="/channels"]')
            check("后台顶栏有「渠道分身」入口", entry.count() == 1)
            check("入口文案正确", entry.inner_text().strip() == "渠道分身")
            entry.click()
            page.wait_for_url("**/channels", timeout=10000)
            page.wait_for_function(
                "document.getElementById('ch-note').textContent.includes('共')",
                timeout=15000,
            )
            check("渠道页可打开且清单加载完成",
                  "共" in page.locator("#ch-note").inner_text(),
                  page.locator("#ch-note").inner_text())
            check("页签未混入管理后台（独立页）",
                  page.locator("#tabs").count() == 0)
            check("统计卡片显示渠道总数",
                  page.locator("#ch-count").inner_text().isdigit())
            page.screenshot(path=str(shots / "channels-list.png"), full_page=True)

            section("2. 新建渠道")
            page.click("#ch-new")
            page.wait_for_function("document.getElementById('ch-modal').hidden === false")
            check("新建弹窗打开", page.locator("#ch-modal").is_visible())
            check("默认助手下拉已渲染",
                  page.locator("#ch-f-default option").count() > 0)
            check("可切换助手复选框已渲染",
                  page.locator("#ch-f-agents input[type=checkbox]").count() > 0)
            page.fill("#ch-f-code", CHANNEL_CODE)
            page.fill("#ch-f-name", "UI验收渠道")
            page.fill("#ch-f-welcome", "欢迎来到 UI 验收渠道")
            page.fill("#ch-f-launcher", "验收咨询")
            page.evaluate(
                "document.getElementById('ch-f-color').value = '#3366ff'"
            )
            page.select_option("#ch-f-position", "left")
            page.select_option("#ch-f-need-auth", "0")
            page.select_option("#ch-f-status", "1")
            # 任务 3.2：门户个性化字段
            page.fill("#ch-f-assistant", "验收小智")
            page.fill("#ch-f-avatar-text", "验")
            page.fill(
                "#ch-f-faq",
                "校园网怎么开通？\n一卡通丢了怎么办？",
            )
            page.fill(
                "#ch-f-shortcuts",
                "🌐|校园网|问|校园网怎么开通？\n"
                "🏛|办事大厅|链|https://ehall.example.edu.cn",
            )
            page.fill(
                "#ch-f-apps",
                "📚|图书馆|借阅与续借|https://lib.example.edu.cn",
            )
            page.select_option("#ch-f-service-enabled", "1")
            page.fill("#ch-f-service-label", "联系客服")
            page.fill("#ch-f-service-url", "https://support.example.edu.cn")
            page.select_option("#ch-f-feedback-enabled", "1")
            page.fill("#ch-f-feedback-label", "意见反馈")
            page.fill("#ch-f-feedback-url", "/feedback")
            page.screenshot(path=str(shots / "channels-modal.png"), full_page=True)
            page.click("#ch-save")
            page.wait_for_function("document.getElementById('ch-modal').hidden === true",
                                   timeout=15000)
            page.wait_for_timeout(600)

            row = page.locator("#ch-tbody tr", has_text="UI验收渠道").first
            check("列表出现新渠道", row.count() > 0)
            check("行内显示渠道编码", CHANNEL_CODE in row.inner_text())
            check("行内显示启用状态", "启用" in row.inner_text())
            check("行内显示访客可用", "访客可用" in row.inner_text())
            pub = _public(cli)
            check("接口侧落库且公开可读", pub is not None and pub["channelName"] == "UI验收渠道",
                  str((pub or {}).get("channelName")))
            if pub:
                agent_id = pub["defaultAgentId"]
                check("默认助手已绑定", isinstance(agent_id, int) and agent_id > 0,
                      f"agentId={agent_id}")
                theme = pub.get("themeConfig") or {}
                check("门户个性化配置落库",
                      theme.get("assistantName") == "验收小智"
                      and theme.get("avatarText") == "验"
                      and theme.get("position") == "left"
                      and len(theme.get("faq") or []) == 2
                      and len(theme.get("shortcuts") or []) == 2
                      and len(theme.get("recommendedApps") or []) == 1
                      and (theme.get("serviceEntry") or {}).get("enabled") == 1
                      and (theme.get("feedback") or {}).get("enabled") == 1,
                      str(theme)[:200])

            section("3. 嵌入代码")
            row.locator(".op-btn", has_text="嵌入代码").click()
            page.wait_for_function(
                "document.getElementById('ch-code-modal').hidden === false"
            )
            url_text = page.locator("#ch-url").inner_text()
            check("渠道 URL 带 channel 参数",
                  f"?channel={CHANNEL_CODE}" in url_text, url_text)
            iframe = page.locator("#ch-code-pre").inner_text()
            check("iframe 代码含 embed=1", "<iframe" in iframe and "embed=1" in iframe)
            page.click("#ch-code-tabs .ch-code-tab[data-kind='float']")
            page.wait_for_timeout(200)
            float_code = page.locator("#ch-code-pre").inner_text()
            check("浮窗代码为自包含脚本", "<script>" in float_code)
            check("浮窗代码带主题色", "#3366ff" in float_code)
            check("浮窗代码带按钮文案", "验收咨询" in float_code)
            page.click("#ch-code-tabs .ch-code-tab[data-kind='sdk']")
            page.wait_for_timeout(200)
            sdk_code = page.locator("#ch-code-pre").inner_text()
            check("SDK 代码引入 nxz.js", "/sdk/nxz.js" in sdk_code, sdk_code[:120])
            check("SDK 代码携带渠道编码",
                  f'data-channel="{CHANNEL_CODE}"' in sdk_code, sdk_code[:160])
            check("SDK 代码带主题色与文案",
                  "#3366ff" in sdk_code and "验收咨询" in sdk_code)
            page.screenshot(path=str(shots / "channels-code.png"), full_page=True)
            page.click("#ch-code-modal .modal-x")
            page.wait_for_function("document.getElementById('ch-code-modal').hidden === true")

            section("4. 编辑与启停")
            row.locator(".op-btn", has_text="编辑").click()
            page.wait_for_function("document.getElementById('ch-modal').hidden === false")
            check("编辑弹窗回填名称", page.input_value("#ch-f-name") == "UI验收渠道")
            check("编辑弹窗回填欢迎语",
                  page.input_value("#ch-f-welcome") == "欢迎来到 UI 验收渠道")
            check("编辑弹窗回填主题色", page.input_value("#ch-f-color") == "#3366ff")
            page.fill("#ch-f-name", "UI验收渠道·改")
            page.fill("#ch-f-welcome", "欢迎语已更新")
            page.click("#ch-save")
            page.wait_for_function("document.getElementById('ch-modal').hidden === true",
                                   timeout=15000)
            page.wait_for_timeout(600)
            edited = page.locator("#ch-tbody tr", has_text="UI验收渠道·改").first
            check("列表显示新名称", edited.count() > 0)
            pub = _public(cli)
            check("接口侧欢迎语已更新",
                  pub is not None and pub["welcomeText"] == "欢迎语已更新",
                  str((pub or {}).get("welcomeText")))

            edited.locator(".op-btn", has_text="停用").click()
            page.wait_for_timeout(800)
            check("停用后徽章变为「停用」",
                  "停用" in page.locator("#ch-tbody tr", has_text="UI验收渠道·改").first.inner_text())
            check("停用后公开接口 404", _public(cli) is None)
            page.locator("#ch-tbody tr", has_text="UI验收渠道·改").first.locator(
                ".op-btn", has_text="启用"
            ).click()
            page.wait_for_timeout(800)
            check("重新启用后公开接口恢复", _public(cli) is not None)

            section("4b. 认证开关与一键复制")
            auth_row = page.locator("#ch-tbody tr", has_text="UI验收渠道·改").first
            auth_row.locator(".op-btn", has_text="开启认证").click()
            page.wait_for_timeout(800)
            check("一键开启认证", "需认证" in page.locator(
                "#ch-tbody tr", has_text="UI验收渠道·改"
            ).first.inner_text())
            auth_row.locator(".op-btn", has_text="关闭认证").click()
            page.wait_for_timeout(800)
            check("一键关闭认证", "访客可用" in page.locator(
                "#ch-tbody tr", has_text="UI验收渠道·改"
            ).first.inner_text())

            auth_row.locator(".op-btn", has_text="复制").click()
            page.wait_for_function(
                "document.getElementById('ch-modal').hidden === false", timeout=15000
            )
            page.wait_for_timeout(400)
            check("复制后打开副本编辑",
                  page.input_value("#ch-f-code") == f"{CHANNEL_CODE}-copy",
                  page.input_value("#ch-f-code"))
            check("副本名称自动带（副本）",
                  page.input_value("#ch-f-name") == "UI验收渠道·改（副本）",
                  page.input_value("#ch-f-name"))
            check("副本默认停用", page.input_value("#ch-f-status") == "0")
            check("副本继承门户配置",
                  page.input_value("#ch-f-assistant") == "验收小智"
                  and "校园网怎么开通？" in page.input_value("#ch-f-faq"))
            check("副本停用时公开接口 404",
                  cli.get(f"/api/channels/{CHANNEL_CODE}-copy").status_code == 404)
            page.click("#ch-modal .modal-x")
            page.wait_for_function(
                "document.getElementById('ch-modal').hidden === true"
            )
            check("副本出现在列表", page.locator(
                f'.doc-meta:text-is("{CHANNEL_CODE}-copy")'
            ).count() == 1)

            section("5. 对话页渠道分流与门户个性化")
            page.goto(f"{base}/?channel={CHANNEL_CODE}&embed=1", wait_until="networkidle")
            check("渠道名称替换品牌标题",
                  page.locator("#brand-title").inner_text() == "UI验收渠道·改",
                  page.locator("#brand-title").inner_text())
            check("首屏展示渠道欢迎语",
                  "欢迎语已更新" in page.locator(".welcome").inner_text())
            check("嵌入模式隐藏后台控件",
                  page.locator(".topbar .controls").count() == 0
                  or not page.locator(".topbar .controls").is_visible())
            check("body 标记 embed-mode",
                  "embed-mode" in (page.get_attribute("body", "class") or ""))
            color = page.evaluate(
                "getComputedStyle(document.documentElement).getPropertyValue('--brand-1').trim()"
            )
            check("主题色应用到页面", color.lower() == "#3366ff", color)
            check("AI 形象名称作为首屏标题",
                  page.locator(".welcome h2").inner_text() == "验收小智",
                  page.locator(".welcome h2").inner_text())
            check("头像文字生效",
                  page.locator(".welcome .big-logo").inner_text() == "验",
                  page.locator(".welcome .big-logo").inner_text())
            check("FAQ 使用渠道配置条数",
                  page.locator(".samples .sample").count() == 2,
                  str(page.locator(".samples .sample").count()))
            check("FAQ 首条文案正确",
                  "校园网怎么开通？" in page.locator(".samples").inner_text())
            check("快捷方式渲染",
                  page.locator(".shortcut-row .shortcut").count() == 2)
            check("推荐应用卡片渲染",
                  page.locator(".portal-app").count() == 1
                  and "图书馆" in page.locator(".portal-app").inner_text())
            check("客服/反馈入口渲染", page.locator(".portal-link").count() == 2)
            check("客服入口链接正确",
                  page.locator(".portal-link", has_text="联系客服").get_attribute("href")
                  == "https://support.example.edu.cn")
            check("反馈入口为站内路径",
                  page.locator(".portal-link", has_text="意见反馈").get_attribute("href")
                  == "/feedback")
            page.screenshot(path=str(shots / "channels-portal.png"), full_page=True)

            with page.expect_request(
                lambda r: "/api/chat/stream" in r.url and r.method == "POST"
            ) as req_info:
                page.fill("#input", "校园网怎么开通？")
                page.click("#send")
            body = req_info.value.post_data_json or {}
            check("提问请求携带渠道默认助手 agentId",
                  str(body.get("agentId")) == str(agent_id),
                  f"{body.get('agentId')} == {agent_id}")
            page.wait_for_selector(".msg.ai:not(.thinking) .bubble", timeout=30000)
            page.wait_for_timeout(800)
            answer = page.locator(".msg.ai:not(.thinking) .bubble").last.inner_text()
            check("作答正常渲染", len(answer) > 0, answer[:40].replace("\n", " "))
            check("作答带 AI 生成标识",
                  page.locator(".msg.ai .ai-label").count() > 0)

            section("6. 删除")
            page.goto(f"{base}/channels", wait_until="networkidle")
            page.wait_for_function(
                "document.getElementById('ch-note').textContent.includes('共')",
                timeout=15000,
            )
            target = page.locator("#ch-tbody tr").filter(
                has=page.locator(f'.doc-meta:text-is("{CHANNEL_CODE}")')
            ).first
            check("精确命中源渠道行（不被副本干扰）", target.count() == 1)
            target.locator(".op-btn", has_text="删除").click()
            page.wait_for_timeout(900)
            check("行已从列表消失",
                  page.locator(f'.doc-meta:text-is("{CHANNEL_CODE}")').count() == 0)
            check("公开接口同步 404", _public(cli) is None)

            section("7. 零 JS 错误")
            check("无 pageerror / console.error", not js_errors,
                  "\n         ".join(js_errors[:5]))
        finally:
            browser.close()

    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
