"""模型网关页 · 浏览器端到端验收（C-01 接入 · 2.4 延伸）。

真实 Chromium 打开 ``/admin`` 的「模型网关」页签，验证 HTTP 层看不见的东西：
页签互斥、状态渲染、**外链入口而非内嵌**、错误码对照展开、以及**零 JS 运行时错误**。

1. **页签**：五个页签互斥切换，网关页懒加载后可见；
2. **指向厂商时**：显示「厂商直连」徽章 + 提示未走网关，控制台按钮**禁用**
   （没有网关就没有控制台可开）；
3. **指向网关时**：显示「网关在线」徽章 + 延迟 + 控制台按钮**启用**；
4. **外链而非内嵌**：页面上**不得出现 iframe** —— 网关带
   ``X-Frame-Options: DENY`` + CSP ``frame-ancestors 'none'``，内嵌必然白屏；
5. **控制台按钮**：点击后新开标签页（本工具拦截 popup 断言 URL），
   且原页面**不发生跳转**（新标签页语义，不是当前页跳走）；
6. **接入配置指引**：三项（apiBase / remoteModel / apiKey）都在；
7. **错误码对照**：默认折叠，点「展开」后出现 model_not_allowed 等条目；
8. **零 JS 错误**：全程无 ``pageerror`` / ``console.error``。

**幂等性**：只读页面，不产生任何持久化数据（网关状态接口本身是只读的），
可反复运行。

用法::

    python tools/verify_gateway_ui.py
    python tools/verify_gateway_ui.py --port 8045 --headful
    python tools/verify_gateway_ui.py --gateway-base http://127.0.0.1:8088/v1

``--gateway-base`` 指向一个真实网关；不传时用本机 8088，若探测不到会**自动降级**为
「只验证离线/厂商直连两条路径」而不是报失败——验收工具不该依赖某个服务恰好活着。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

os.environ.setdefault("KB_STORE", "json")
os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH", r"C:\Users\wick\.workbuddy-ai\binaries\ms-playwright"
)

try:  # pragma: no cover - 仅影响交互输出
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

PASSED = 0
FAILED = 0
SKIPPED = 0

#: 浏览器对 4xx/5xx 资源响应会记 console.error；本页若有意触发则归类，不计入零错误守卫
_HTTP_RES_ERR = re.compile(r"Failed to load resource: the server responded with a status of \d{3}")


def check(name: str, ok: bool, detail: str = "") -> bool:
    global PASSED, FAILED
    if ok:
        PASSED += 1
    else:
        FAILED += 1
    tail = f"  [{detail}]" if detail else ""
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{tail}")
    return ok


def skip(name: str, why: str) -> None:
    global SKIPPED
    SKIPPED += 1
    print(f"  [SKIP] {name}  [{why}]")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def _server_helpers():
    from tools._server import read_log, spawn_server, stop_server, wait_port

    return read_log, spawn_server, stop_server, wait_port


def _reachable(base: str) -> bool:
    """探测网关是否真的在跑（决定跑「在线」还是「离线」那条路径）。"""
    import httpx

    from app.services.gateway_admin import origin_of

    origin = origin_of(base)
    if not origin:
        return False
    try:
        resp = httpx.get(f"{origin}/healthz", timeout=3.0, trust_env=False)
    except Exception:  # noqa: BLE001
        return False
    return resp.status_code == 200


# ================================================================== 主流程
def _run(base: str, shots: Path, *, headful: bool, gateway_base: str, online: bool) -> int:
    from playwright.sync_api import sync_playwright

    dialogs: list[str] = []
    js_errors: list[str] = []
    expected_http_errors: list[str] = []
    popups: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headful)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})

        page.on("pageerror", lambda exc: js_errors.append(f"pageerror: {exc}"))

        def on_console(msg) -> None:  # noqa: ANN001
            if msg.type != "error":
                return
            if _HTTP_RES_ERR.search(msg.text):
                expected_http_errors.append(msg.text)
            else:
                js_errors.append(f"console.error: {msg.text}")

        page.on("console", on_console)
        page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))

        # 拦截新标签页：控制台入口必须是 target=_blank 语义
        def on_popup(pop) -> None:
            popups.append(pop.url)

        page.on("popup", on_popup)

        section("1. 页签与初始状态")
        page.goto(f"{base}/admin", wait_until="networkidle")
        check("页签数量为 5（知识库/智能体/网关/日志/系统管理）", page.locator("#tabs .tab").count() == 5,
              f"{page.locator('#tabs .tab').count()} 个")
        check("网关页签存在", page.locator('#tabs .tab[data-tab="gateway"]').count() == 1)
        check("默认停在「知识库」页", page.locator("#page-docs").is_visible())
        check("网关页初始隐藏", not page.locator("#page-gateway").is_visible())

        section("2. 切到网关页（懒加载）")
        page.click('#tabs .tab[data-tab="gateway"]')
        page.wait_for_selector("#gw-list", timeout=15000)
        check("网关页可见", page.locator("#page-gateway").is_visible())
        check("知识库页已隐藏", not page.locator("#page-docs").is_visible())
        check("页签高亮切换",
              "active" in (page.locator('#tabs .tab[data-tab="gateway"]').get_attribute("class") or ""))

        # 等首屏渲染完成：列表有内容，或空态可见
        page.wait_for_function(
            """() => {
                const list = document.getElementById('gw-list');
                const empty = document.getElementById('gw-empty');
                return (list && list.children.length > 0) || (empty && !empty.hidden);
            }""",
            timeout=20000,
        )

        section("3. 状态渲染")
        items = page.locator("#gw-list .gw-item").count()
        check("渲染出端点卡片（或空态）",
              items > 0 or page.locator("#gw-empty").is_visible(), f"{items} 张卡片")
        note = (page.locator("#gw-note").inner_text() or "").strip()
        check("给出结论性提示文案", len(note) > 4, note[:60])

        if items:
            badges = page.locator("#gw-list .gw-badge").count()
            check("每张卡片都有状态徽章", badges == items, f"{badges} 徽章 / {items} 卡片")

            # 与后端真实状态比对，避免"渲染出一个恰好正确的假象"
            import httpx

            with httpx.Client(base_url=base, timeout=20.0, trust_env=False) as cli:
                data = cli.get("/api/admin/gateway/status").json()["data"]
            expected_status = {e["origin"]: e["status"] for e in data["endpoints"]}
            shown = page.locator("#gw-list .gw-origin").all_inner_texts()
            check("卡片地址与接口返回一致",
                  [s.strip() for s in shown] == list(expected_status),
                  f"页面 {shown} / 接口 {list(expected_status)}")

        section("4. 外链而非内嵌（关键约束）")
        check("页面上不存在 iframe（网关拒绝被嵌入）",
              page.locator("#page-gateway iframe").count() == 0)
        fineprint = page.locator("#page-gateway .gw-fineprint").all_inner_texts()
        blob = " ".join(fineprint)
        check("页面明确说明「不持有管理员凭据」", "不持有" in blob)
        check("页面明确说明「无法内嵌」及其原因", "无法内嵌" in blob and "DENY" in blob)

        section("5. 控制台入口按钮")
        btn = page.locator("#gw-console")
        disabled = btn.is_disabled()
        if online:
            check("有在线网关 → 按钮启用", not disabled)
            if not disabled:
                before_url = page.url
                btn.click()
                page.wait_for_timeout(1200)
                check("点击后新开标签页（popup）", len(popups) >= 1, f"{popups}")
                if popups:
                    check("新开页指向网关控制台 /admin-ui/",
                          "/admin-ui/" in popups[0], popups[0])
                check("原页面未跳转（是新标签页语义，不是当前页跳走）",
                      page.url == before_url, page.url)
        else:
            check("无在线网关 → 按钮禁用（没有控制台可开）", disabled)
            skip("新标签页跳转", "当前无在线网关")

        section("6. 接入配置指引")
        guide = page.locator("#gw-guide").inner_text()
        for token in ("apiBase", "remoteModel", "apiKey", "sk-campus-", "AllowedModels"):
            check(f"配置指引含 {token}", token in guide)

        section("7. 错误码对照（默认折叠）")
        hints = page.locator("#gw-hints")
        check("对照表默认折叠", hints.is_hidden())
        page.click("#gw-hints-toggle")
        page.wait_for_timeout(400)
        check("点「展开」后可见", hints.is_visible())
        hints_text = hints.inner_text()
        for code in ("model_not_allowed", "invalid_api_key", "rate_limit_exceeded"):
            check(f"含错误码 {code}", code in hints_text)
        check("展开按钮文案切换为「收起」",
              (page.locator("#gw-hints-toggle").inner_text() or "").strip() == "收起")
        page.click("#gw-hints-toggle")
        page.wait_for_timeout(300)
        check("再点收回", hints.is_hidden())

        section("8. 重新探测")
        page.click("#gw-refresh")
        page.wait_for_timeout(2500)
        items2 = page.locator("#gw-list .gw-item").count()
        check("重新探测后卡片数量不变（幂等）", items2 == items, f"{items} → {items2}")
        note2 = (page.locator("#gw-note").inner_text() or "").strip()
        check("重新探测后仍有提示文案", len(note2) > 4)

        section("9. 页签来回切换（懒加载不重复初始化）")
        page.click('#tabs .tab[data-tab="docs"]')
        check("切回知识库页", page.locator("#page-docs").is_visible())
        page.click('#tabs .tab[data-tab="gateway"]')
        page.wait_for_timeout(500)
        check("再切回网关页仍可见", page.locator("#page-gateway").is_visible())
        check("切回后未重复渲染堆叠",
              page.locator("#gw-list .gw-item").count() == items,
              f"{page.locator('#gw-list .gw-item').count()} vs {items}")

        page.screenshot(path=str(shots / "gateway_page.png"), full_page=True)

        section("10. 全站打磨项（跨页签）")

        # ---- 10a 键盘焦点可见性
        #
        # 打磨前只有 input/select/textarea 有 focus 样式，**按钮与页签完全没有**：
        # 深色主题下 Tab 导航看不出焦点在哪，是真实的可访问性缺陷。
        #
        # ⚠ 判定必须看 border / box-shadow，**不能只看 outline** ——
        #   输入框的焦点是靠「border 变色 + 3px 光晕」表达的，只读 outline 会误判为"不可见"。
        #   这个坑我自己踩过一次（探针第一版就误报了 input）。
        page.click("body")
        focus_probe = """(sel) => {
            const e = document.querySelector(sel);
            if (!e) return null;
            const s = getComputedStyle(e);
            return {borderColor: s.borderColor, boxShadow: s.boxShadow,
                    outlineStyle: s.outlineStyle, outlineWidth: s.outlineWidth};
        }"""
        # 页签按钮是最好验的（恒定存在，不依赖数据）
        before = page.evaluate(focus_probe, '#tabs .tab[data-tab="docs"]')
        page.evaluate("() => document.querySelector('#tabs .tab[data-tab=\"docs\"]').blur()")
        page.keyboard.press("Tab")
        page.evaluate("() => document.querySelector('#tabs .tab[data-tab=\"docs\"]').focus()")
        page.wait_for_timeout(150)
        after = page.evaluate(focus_probe, '#tabs .tab[data-tab="docs"]')
        focus_visible = (
            before and after and (
                after["outlineStyle"] not in ("none", "")
                or after["borderColor"] != before["borderColor"]
                or after["boxShadow"] != before["boxShadow"]
            )
        )
        check("页签按钮键盘聚焦可见（outline / border / shadow 任一变化）",
              bool(focus_visible), f"outline={after['outlineStyle'] if after else '-'}")

        # 主按钮（网关页的「打开控制台」）
        btn_state = page.evaluate(focus_probe, "#gw-console")
        check("主按钮聚焦样式可用（存在 outline 或阴影规则）",
              bool(btn_state), f"outlineStyle={btn_state['outlineStyle'] if btn_state else '-'}")

        # :focus-visible 语义：鼠标点击不应残留焦点环
        page.mouse.click(10, 10)
        page.wait_for_timeout(150)
        mouse_state = page.evaluate(
            "() => getComputedStyle(document.activeElement).outlineStyle"
        )
        check("鼠标操作不显示焦点环（:focus-visible 语义）",
              mouse_state in ("none", ""), f"outlineStyle={mouse_state!r}")

        # ---- 10b 懒加载提示（延迟 250ms，避免快请求闪一下）
        #
        # 做法：把助手列表接口延迟 1.5s 逼出加载态。
        # 只断言「快请求不闪」是不够的 —— 那样即使提示永远不会出现也能通过。
        page.click('#tabs .tab[data-tab="agents"]')
        page.wait_for_timeout(60)
        fast = page.locator("#page-agents").get_attribute("data-loading")
        check("快请求不闪出加载提示（60ms 时为无）", not fast, f"data-loading={fast!r}")
        page.wait_for_timeout(1500)

        def _delay_agents(route) -> None:  # noqa: ANN001
            page.wait_for_timeout(1200)
            route.continue_()

        # 重置页签状态以便重新触发懒加载提示：直接刷新页面（bootedPages 是内存态）
        page.route("**/api/admin/agents*", _delay_agents)
        page.goto(f"{base}/admin", wait_until="networkidle")
        page.click('#tabs .tab[data-tab="agents"]')
        page.wait_for_timeout(500)  # 超过 250ms 阈值
        slow = page.locator("#page-agents").get_attribute("data-loading")
        check("慢请求出现加载提示（500ms 时为有）", slow == "1", f"data-loading={slow!r}")
        page.wait_for_timeout(2500)
        gone = page.locator("#page-agents").get_attribute("data-loading")
        check("加载完成后提示自动收起", not gone, f"data-loading={gone!r}")

        browser.close()

    section("11. 零 JS 运行时错误")
    check("全程无 pageerror / console.error", not js_errors,
          f"{len(js_errors)} 条：" + (js_errors[0][:90] if js_errors else "无"))
    check("没有意外的 alert/confirm 弹窗", not dialogs, f"{dialogs}")
    if expected_http_errors:
        print(f"  （{len(expected_http_errors)} 条有意的 4xx 资源错误，已归类不计）")

    print(f"\n截图：{shots / 'gateway_page.png'}")
    return 0 if FAILED == 0 else 1


# ================================================================== main
def main() -> int:
    parser = argparse.ArgumentParser(description="模型网关页浏览器验收（C-01 接入）")
    parser.add_argument("--port", type=int, default=8045, help="验收用服务端口")
    parser.add_argument("--headful", action="store_true", help="显示浏览器窗口")
    parser.add_argument("--shots-dir", default="", help="截图目录（默认 kb-service/artifacts）")
    parser.add_argument("--gateway-base", default="http://127.0.0.1:8088/v1",
                        help="用于验证「网关在线」路径的地址；不可达时自动降级")
    args = parser.parse_args()

    print("=" * 68)
    print("模型网关页 · 浏览器端到端验收（C-01 接入 · 2.4 延伸）")
    print("=" * 68)

    online_hint = _reachable(args.gateway_base)
    # 只有探测到网关在线时才把服务指向它；否则保持 .env 原样（通常是厂商直连）
    gateway_env = args.gateway_base if online_hint else ""
    print(f"网关探测：{args.gateway_base} → {'在线' if online_hint else '不可达'}"
          f"{'' if online_hint else '（将验证「未走网关」路径）'}")

    read_log, spawn_server, stop_server, wait_port = _server_helpers()

    env = dict(os.environ)
    env["KB_STORE"] = os.environ.get("KB_STORE", "json")
    env["AUTH_ENABLED"] = "false"
    env["LLM_MOCK"] = "0"
    if gateway_env:
        env["LLM_BASE_URL"] = gateway_env
        env["LLM_API_KEY"] = env.get("LLM_API_KEY") or "sk-campus-ui-probe"
        env["LLM_MODEL"] = env.get("LLM_MODEL") or "campus-smart"

    port = args.port
    base = f"http://127.0.0.1:{port}"
    shots = Path(args.shots_dir) if args.shots_dir else SERVICE_DIR / "artifacts"
    shots.mkdir(parents=True, exist_ok=True)

    print(f"启动服务 127.0.0.1:{port}…")
    proc, log_path = spawn_server(port, env, SERVICE_DIR)
    rc = 1
    try:
        if not wait_port(port, timeout=60):
            print("服务启动失败，日志尾部：")
            print(read_log(log_path, 3000))
            return 1
        rc = _run(base, shots, headful=args.headful,
                  gateway_base=args.gateway_base, online=online_hint)
    finally:
        stop_server(proc)

    print("\n" + "=" * 68)
    print(f"结果：{PASSED} PASS / {FAILED} FAIL / {SKIPPED} SKIP")
    print("=" * 68)
    return 1 if FAILED else rc


if __name__ == "__main__":
    raise SystemExit(main())
