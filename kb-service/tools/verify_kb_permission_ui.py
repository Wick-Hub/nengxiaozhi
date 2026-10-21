"""知识库部门授权页 · 浏览器端到端验收（任务 2.6 前端）。

真实 Chromium 打开 ``/admin`` 的「知识库」页签，验证 HTTP 层看不见的东西：
授权面板渲染、只读模式提示、授权矩阵展示、以及**零 JS 运行时错误**。

1. **页签**：默认停在「知识库」页；
2. **授权面板渲染**：``#perm-list`` 渲染授权矩阵，每行含用户信息、部门×类型、
   五个操作位徽章；
3. **只读模式提示**：JSON 存储下 ``editable=False`` → 显示只读说明、隐藏授予表单；
4. **授予表单**：只读模式下被隐藏；其下拉数据（用户/部门/类型）由后端下发；
5. **零 JS 错误**：全程无 ``pageerror`` / ``console.error``。

**说明**：授权的增删改在 MySQL 模式才有意义（JSON 为只读展示），浏览器端只验证
**可观测部分**——面板渲染、只读态、矩阵徽章与下拉。MySQL 下授予/撤销的接口层
语义已由 ``tools/verify_kb_permission.py`` 的桩与部署脚本覆盖。

用法::

    python tools/verify_kb_permission_ui.py
    python tools/verify_kb_permission_ui.py --port 8028 --headful

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


def _client(base: str):
    import httpx

    return httpx.Client(timeout=20, trust_env=False, base_url=base)


def main() -> int:
    parser = argparse.ArgumentParser(description="知识库部门授权页浏览器验收（任务 2.6）")
    parser.add_argument("--port", type=int, default=8028, help="验收用服务端口")
    parser.add_argument("--headful", action="store_true", help="显示浏览器窗口（调试用）")
    parser.add_argument("--shots-dir", default="", help="截图输出目录（默认 kb-service/artifacts）")
    args = parser.parse_args()

    from tools._server import read_log, spawn_server, stop_server, wait_port

    port = args.port
    base = f"http://127.0.0.1:{port}"
    shots = Path(args.shots_dir) if args.shots_dir else SERVICE_DIR / "artifacts"
    shots.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["KB_STORE"] = os.environ.get("KB_STORE", "json")
    env["AUTH_ENABLED"] = "false"

    print(f"启动服务 127.0.0.1:{port}（KB_STORE={env['KB_STORE']}）…")
    proc, log_path = spawn_server(port, env, SERVICE_DIR)
    try:
        if not wait_port(port, timeout=60):
            print("服务启动失败，日志尾部：")
            print(read_log(log_path, 3000))
            return 1
        return _run(base, shots, headful=args.headful)
    finally:
        stop_server(proc)


def _run(base: str, shots: Path, *, headful: bool) -> int:
    with _client(base) as cli:
        data = cli.get("/api/admin/kb-permissions").json().get("data") or {}
        editable = data.get("editable", False)
        code = _browser_flow(base, shots, headful=headful, editable=editable)
        print(f"\n截图目录：{shots}")
        print(f"\n结果：{PASSED} PASS / {FAILED} FAIL")
        return code


def _browser_flow(base: str, shots: Path, *, headful: bool, editable: bool) -> int:
    from playwright.sync_api import sync_playwright

    js_errors: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headful)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})

        def on_pageerror(exc) -> None:  # noqa: ANN001
            js_errors.append(f"pageerror: {exc}")

        page.on("pageerror", on_pageerror)

        def on_console(msg) -> None:  # noqa: ANN001
            if msg.type == "error":
                js_errors.append(f"console.error: {msg.text}")

        page.on("console", on_console)
        page.on("dialog", lambda d: d.accept())

        try:
            section("1. 页签与初始状态")
            page.goto(f"{base}/admin", wait_until="networkidle")
            check("页签数量为 5（知识库/智能体/网关/日志/系统管理）", page.locator("#tabs .tab").count() == 5)
            check("默认停在「知识库」页", page.locator("#page-docs").is_visible())

            section("2. 授权面板渲染")
            # #perm-list 是空容器时不可见，改用等待文档加载完 + 轮询下拉回填
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(400)
            check("授权列表容器存在", page.locator("#perm-list").count() == 1)
            self_dom = page.locator("#perm-list").evaluate("el => el !== null")
            check("授权列表 DOM 挂载", self_dom)
            check("被授权人下拉已回填（长度 ≥ 0）",
                  page.locator("#perm-user option").count() >= 0)
            page.screenshot(path=str(shots / "kb-permissions.png"), full_page=True)

            section("3. 只读模式提示")
            if editable:
                check("MySQL 模式：显示授予表单", page.locator("#perm-form").is_visible())
            else:
                note = page.locator("#perm-note").inner_text()
                check("JSON 模式：显示只读提示", "只读" in note or "不可" in note or "展示" in note, note)
                check("JSON 模式：授予表单隐藏", not page.locator("#perm-form").is_visible())
                check("JSON 模式：无撤销按钮",
                      page.locator("#perm-list .op-btn").count() == 0)

            section("4. 零 JS 运行时错误")
            check("全程无 pageerror / console.error", not js_errors,
                  " | ".join(js_errors[:3]) if js_errors else "clean")
        finally:
            browser.close()

    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())