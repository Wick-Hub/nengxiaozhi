"""统一身份认证浏览器验收（任务 4.1）。

真实 Chromium 覆盖 HTTP 层看不到的行为：登录弹窗里的 SSO 入口、
CAS 回跳后的哈希交换码消费、token 落盘、徽标身份展示、无权限提示与访客模式。

用法::

    python tools/verify_sso_ui.py
    python tools/verify_sso_ui.py --headful

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
    parser = argparse.ArgumentParser(description="统一身份认证浏览器验收（任务 4.1）")
    parser.add_argument("--port", type=int, default=8041, help="验收用服务端口")
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
    env["AUTH_ENABLED"] = "true"
    env["RATE_LIMIT_ENABLED"] = "false"
    env["LLM_MOCK"] = "1"

    print(f"启动服务 127.0.0.1:{port}（KB_STORE=json, AUTH_ENABLED=true）…")
    proc, log_path = spawn_server(port, env, SERVICE_DIR)
    try:
        if not wait_port(port, timeout=60):
            print("服务启动失败，日志尾部：")
            print(read_log(log_path, 3000))
            return 1

        errors: list[str] = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not args.headful)
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))

            def on_console(msg) -> None:  # noqa: ANN001
                if msg.type == "error":
                    errors.append(f"console.error: {msg.text}")

            page.on("console", on_console)
            try:
                section("1. 访客访问管理端")
                page.goto(f"{base}/admin", wait_until="networkidle")
                page.wait_for_selector("#nxz-auth-modal:not([hidden])", timeout=15000)
                check("未登录弹出登录框", page.locator("#nxz-auth-modal").is_visible())
                check("SSO 入口可见", page.locator("#nxz-auth-sso").is_visible())
                href = page.locator("#nxz-auth-sso").get_attribute("href") or ""
                check("SSO 入口指向 CAS 登录", href.startswith("/api/auth/cas/login?redirect="), href)
                page.screenshot(path=str(shots / "sso-login.png"))

                section("2. 统一身份认证登录")
                page.click("#nxz-auth-sso")
                page.wait_for_function(
                    "() => { const t = localStorage.getItem('nxz_token'); return !!t; }",
                    timeout=20000,
                )
                page.wait_for_load_state("networkidle")
                token = page.evaluate("localStorage.getItem('nxz_token')")
                check("token 已落 localStorage", bool(token))
                check("URL 不残留交换码", "#cas=" not in page.url, page.url)
                me = page.evaluate(
                    "async () => { const t = localStorage.getItem('nxz_token');"
                    " const r = await fetch('/api/auth/me', {headers: {Authorization: 'Bearer ' + t}});"
                    " return (await r.json()).data; }"
                )
                check("会话身份=20230001", me.get("userNo") == "20230001", me.get("userNo", ""))
                check("会话角色=本科生", me.get("role") == "本科生", me.get("role", ""))
                check("交换码未进 URL/history", "#cas" not in page.url)
                badge = page.locator("#nxz-auth-badge").inner_text()
                check("徽标展示身份", "20230001" in badge and "本科生" in badge, badge)
                page.wait_for_selector("#nxz-denied:not([hidden])", timeout=10000)
                check("无管理权限时展示 403 提示", page.locator("#nxz-denied").is_visible())
                page.screenshot(path=str(shots / "sso-authed.png"))

                section("3. 退出与访客模式")
                page.evaluate("window.NXZAuth.logout()")
                page.wait_for_load_state("networkidle")
                page.wait_for_selector("#nxz-auth-modal:not([hidden])", timeout=15000)
                check("退出后回到登录框", page.locator("#nxz-auth-modal").is_visible())
                check("token 已清除", not page.evaluate("localStorage.getItem('nxz_token')"))

                guest = browser.new_page(viewport={"width": 1440, "height": 900})
                guest.goto(f"{base}/", wait_until="networkidle")
                check("访客可打开门户", guest.locator("#chat").count() == 1)
                check("访客可见登录入口", guest.locator("#login-btn").count() == 1)
                check("门户未自动弹登录框", guest.locator("#login-modal").is_hidden())
                guest.goto(f"{base}/unrecognized", wait_until="networkidle")
                check("访客访问未识别页被拦截", guest.locator("#nxz-denied").is_visible())
                guest.screenshot(path=str(shots / "sso-guest.png"))
                guest.close()

                section("4. 运行时错误")
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
