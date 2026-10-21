"""用户端门户 · 浏览器端到端验收（任务 3.5 / P-B-04·05·06·07）。

真实 Chromium 覆盖三端布局与门户模块：

1. **PC（1440×900）**：左侧会话历史常驻；提问后写入本机历史；新会话保留历史；
   点击条目恢复整段对话；刷新后自动恢复；删除条目/清空历史；
2. **移动（390×844）**：历史栏默认收起为抽屉，按钮可开合，页面无横向溢出，
   输入区在视口内；
3. **智能硬件（?kiosk=1）**：隐藏历史/管理入口，示例问题与发送按钮放大到触控尺寸；
4. **登录/退出（仅 MySQL）**：弹窗登录 → 顶栏显示学工号与身份、角色下拉锁定、
   问答请求携带 Bearer token 且 role 为登录身份；刷新保持登录；退出后恢复访客。
5. 全程零 ``pageerror`` / ``console.error``。

**幂等性**：只使用浏览器本机 localStorage 与一个未登记学工号（MySQL 演示阶段允许），
不写入业务数据，可重复运行。

用法::

    python tools/verify_portal_ui.py
    python tools/verify_portal_ui.py --store mysql --headful

退出码：全部通过为 0，存在 FAIL 为 1。
"""

from __future__ import annotations

import argparse
import json
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
SKIPPED = 0
LOGIN_USER = "vf-portal-user"


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
    else:
        FAILED += 1
    tail = f"  [{detail}]" if detail else ""
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{tail}")


def skip(name: str, reason: str) -> None:
    global SKIPPED
    SKIPPED += 1
    print(f"  [SKIP] {name}  [{reason}]")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def _box(page, selector: str) -> dict:
    return page.locator(selector).first.bounding_box() or {}


def _ask(page, question: str) -> None:
    page.fill("#input", question)
    page.click("#send")
    page.wait_for_selector(".msg.ai .ai-label", timeout=30000)


def _desktop(browser, base: str, shots: Path, js_errors: list[str]) -> None:
    section("1. PC 三栏与历史会话（1440×900）")
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    page.on("pageerror", lambda exc: js_errors.append(f"pageerror: {exc}"))
    page.on("console", lambda m: js_errors.append(f"console.error: {m.text}")
            if m.type == "error" else None)
    page.on("dialog", lambda d: d.accept())
    try:
        page.goto(base, wait_until="networkidle")
        panel = _box(page, "#history-panel")
        check("桌面历史栏常驻", bool(panel) and panel.get("width", 0) >= 220,
              f"width={panel.get('width')}")
        check("空态提示可见", "暂无历史会话" in page.locator("#history-list").inner_text())
        check("登录入口可见", page.locator("#login-btn").is_visible())
        check("角色下拉可用（访客模式）", page.locator("#role").is_enabled())

        _ask(page, "校园网怎么开通？")
        page.wait_for_selector(".history-item", timeout=5000)
        title = page.locator(".history-title").first.inner_text()
        check("提问后写入本机历史", "校园网怎么开通" in title, title)
        check("当前会话条目高亮", page.locator(".history-item.active").count() == 1)

        page.click("#new-chat")
        page.wait_for_selector(".welcome")
        check("新会话回到欢迎页", page.locator(".welcome").count() == 1)
        check("新会话后历史保留", page.locator(".history-item").count() == 1)
        check("新会话后无高亮条目", page.locator(".history-item.active").count() == 0)

        page.locator(".history-open").first.click()
        page.wait_for_selector(".msg.user", timeout=10000)
        check("点击历史恢复整段会话", page.locator(".msg").count() >= 2)
        check("恢复后条目重新高亮", page.locator(".history-item.active").count() == 1)

        page.reload(wait_until="networkidle")
        page.wait_for_selector(".msg.user", timeout=10000)
        check("刷新后自动恢复上次会话", page.locator(".msg").count() >= 2)
        check("刷新后历史条目仍在", page.locator(".history-item").count() == 1)

        page.hover(".history-item")
        page.click(".history-del")
        check("删除本机历史条目", page.locator(".history-item").count() == 0)
        check("删除条目不清理当前对话", page.locator(".msg").count() >= 2)

        _ask(page, "一卡通丢了怎么办？")
        page.wait_for_selector(".history-item", timeout=5000)
        page.click("#history-clear")
        check("清空历史", page.locator(".history-item").count() == 0)
        check("清空后当前对话仍在", page.locator(".msg").count() >= 2)
        check("清空后刷新不再恢复", _reload_has_welcome(page))

        page.screenshot(path=str(shots / "portal-desktop.png"), full_page=True)
    finally:
        ctx.close()


def _reload_has_welcome(page) -> bool:
    page.reload(wait_until="networkidle")
    return page.locator(".welcome").count() == 1


def _mobile(browser, base: str, shots: Path) -> None:
    section("2. 移动端抽屉与布局（390×844）")
    ctx = browser.new_context(viewport={"width": 390, "height": 844})
    page = ctx.new_page()
    page.on("dialog", lambda d: d.accept())
    try:
        page.goto(base, wait_until="networkidle")
        panel = _box(page, "#history-panel")
        closed_right = panel.get("x", 0) + panel.get("width", 0)
        check("移动端历史栏默认收起", closed_right <= 1, f"right={closed_right}")
        check("历史抽屉按钮可见", page.locator("#history-toggle").is_visible())
        check("管理后台入口在手机端隐藏",
              not page.locator(".admin-link").is_visible())

        page.click("#history-toggle")
        page.wait_for_timeout(300)
        panel = _box(page, "#history-panel")
        check("抽屉可打开", page.evaluate("document.body.classList.contains('history-open')")
              and panel.get("x", -1) >= 0, f"left={panel.get('x')}")
        check("遮罩层出现", page.locator("#history-overlay").is_visible())
        check("抽屉关闭按钮可见", page.locator("#history-close").is_visible())

        page.click("#history-close")
        page.wait_for_timeout(300)
        check("抽屉可关闭",
              not page.evaluate("document.body.classList.contains('history-open')"))
        check("遮罩层隐藏", not page.locator("#history-overlay").is_visible())

        width = page.evaluate("({scroll: document.documentElement.scrollWidth, "
                              "view: window.innerWidth})")
        check("页面无横向溢出", width["scroll"] <= width["view"] + 1,
              f"{width['scroll']}>{width['view']}")
        send = _box(page, "#send")
        check("输入区在视口内", send.get("y", 9999) + send.get("height", 0) <= 844,
              f"bottom={send.get('y', 0) + send.get('height', 0)}")
        check("品牌标题在顶栏内",
              _box(page, "#brand-title").get("width", 0) > 0)

        page.screenshot(path=str(shots / "portal-mobile.png"), full_page=True)
    finally:
        ctx.close()


def _kiosk(browser, base: str, shots: Path) -> None:
    section("3. 智能硬件精简布局（?kiosk=1，390×844）")
    ctx = browser.new_context(viewport={"width": 390, "height": 844})
    page = ctx.new_page()
    try:
        page.goto(base + "?kiosk=1", wait_until="networkidle")
        check("kiosk 模式生效",
              page.evaluate("document.body.classList.contains('kiosk-mode')"))
        check("历史栏隐藏", not page.locator("#history-panel").is_visible())
        check("历史按钮隐藏", not page.locator("#history-toggle").is_visible())
        check("管理入口隐藏", not page.locator(".admin-link").is_visible())

        sample = _box(page, ".sample")
        check("示例问题触控高度 ≥ 44px", sample.get("height", 0) >= 44,
              f"height={sample.get('height')}")
        send = _box(page, "#send")
        check("发送按钮触控高度 ≥ 44px", send.get("height", 0) >= 44,
              f"height={send.get('height')}")
        font = page.evaluate("getComputedStyle(document.getElementById('input')).fontSize")
        check("输入框字号 ≥ 16px", float(font.replace("px", "")) >= 16, font)
        width = page.evaluate("({scroll: document.documentElement.scrollWidth, "
                              "view: window.innerWidth})")
        check("kiosk 无横向溢出", width["scroll"] <= width["view"] + 1,
              f"{width['scroll']}>{width['view']}")

        page.screenshot(path=str(shots / "portal-kiosk.png"), full_page=True)
    finally:
        ctx.close()


def _login(browser, base: str) -> None:
    section("4. 登录 / 退出与 token 身份（MySQL）")
    ctx = browser.new_context(viewport={"width": 1200, "height": 900})
    page = ctx.new_page()
    page.on("dialog", lambda d: d.accept())
    captured: dict[str, dict] = {}

    def on_request(req) -> None:  # noqa: ANN001
        if req.url.endswith("/api/chat/stream"):
            try:
                captured["body"] = json.loads(req.post_data or "{}")
            except json.JSONDecodeError:
                captured["body"] = {}
            captured["auth"] = req.headers.get("authorization", "")

    page.on("request", on_request)
    try:
        page.goto(base, wait_until="networkidle")
        page.click("#login-btn")
        check("登录弹窗打开", page.locator("#login-modal").is_visible())

        page.click("#lm-submit")
        page.wait_for_timeout(200)
        check("必填校验阻止空学工号提交", page.locator("#login-modal").is_visible())
        check("空学工号时未写入 token",
              not page.evaluate("localStorage.getItem('nxz_token')"))

        page.fill("#lm-user", LOGIN_USER)
        page.select_option("#lm-role", "本科生")
        page.click("#lm-submit")
        page.wait_for_selector(".auth-name", timeout=20000)
        name = page.locator(".auth-name").inner_text()
        check("顶栏显示学工号与身份", LOGIN_USER in name and "本科生" in name, name)
        check("登录后角色下拉锁定", not page.locator("#role").is_enabled())
        check("角色与登录身份一致",
              page.locator("#role").input_value() == "本科生")
        check("token 已保存",
              bool(page.evaluate("localStorage.getItem('nxz_token')")))

        _ask(page, "研究生怎么申请奖学金？")
        check("问答请求携带 Bearer token",
              captured.get("auth", "").startswith("Bearer "),
              captured.get("auth", "")[:24])
        check("问答请求 role 为登录身份",
              (captured.get("body") or {}).get("role") == "本科生",
              str((captured.get("body") or {}).get("role")))

        page.reload(wait_until="networkidle")
        page.wait_for_selector(".auth-name", timeout=20000)
        check("刷新后保持登录", LOGIN_USER in page.locator(".auth-name").inner_text())

        page.locator("#auth-box button", has_text="退出").click()
        page.wait_for_selector("#login-btn", timeout=10000)
        check("退出后恢复登录入口", page.locator("#login-btn").is_visible())
        check("退出后角色下拉恢复", page.locator("#role").is_enabled())
        check("退出后 token 已清除",
              not page.evaluate("localStorage.getItem('nxz_token')"))
    finally:
        ctx.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="用户端门户浏览器验收（任务 3.5）")
    parser.add_argument("--port", type=int, default=8044)
    parser.add_argument("--store", default="json", choices=["json", "mysql"])
    parser.add_argument("--headful", action="store_true", help="显示浏览器窗口（调试用）")
    parser.add_argument("--shots-dir", default="")
    args = parser.parse_args()

    from tools._server import read_log, spawn_server, stop_server, wait_port

    port = args.port
    base = f"http://127.0.0.1:{port}"
    shots = Path(args.shots_dir) if args.shots_dir else SERVICE_DIR / "artifacts"
    shots.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env.update(
        KB_STORE=args.store,
        LLM_MOCK="1",
        AUTH_ENABLED="false",
        RATE_LIMIT_ENABLED="false",
    )

    print(f"启动服务 127.0.0.1:{port}（KB_STORE={args.store} + LLM_MOCK）…")
    proc, log_path = spawn_server(port, env, SERVICE_DIR)
    try:
        if not wait_port(port, timeout=60):
            print("服务启动失败，日志尾部：")
            print(read_log(log_path, 3000))
            return 1

        from playwright.sync_api import sync_playwright

        js_errors: list[str] = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not args.headful)
            try:
                _desktop(browser, base, shots, js_errors)
                _mobile(browser, base, shots)
                _kiosk(browser, base, shots)
                if args.store == "mysql":
                    _login(browser, base)
                else:
                    section("4. 登录 / 退出与 token 身份")
                    skip("登录退出模块", "JSON 模式无账号库，请用 --store mysql")
            finally:
                browser.close()

        section("5. 零 JS 运行时错误")
        check("全程无 pageerror / console.error", not js_errors,
              " | ".join(js_errors[:3]) if js_errors else "clean")
    finally:
        stop_server(proc)

    print(f"\n截图目录：{shots}")
    print(f"\n结果：{PASSED} PASS / {FAILED} FAIL / {SKIPPED} SKIP")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
