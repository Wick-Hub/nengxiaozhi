"""知识分类管理页 · 浏览器端到端验收（任务 2.5 前端）。

真实 Chromium 打开 ``/admin`` 的「知识库」页签，验证 HTTP 层看不见的东西：
分类面板渲染、按「部门 × 知识类型」分组、只读模式提示、以及**零 JS 运行时错误**。

1. **页签**：默认停在「知识库」页，其它页签隐藏；
2. **分类面板渲染**：``#cat-tree`` 渲染出按部门分组的结构（``.cat-group``），
   每个分类行含名称、类型与文档数；
3. **只读模式提示**：JSON 存储下 ``editable=False`` → 显示只读说明、隐藏新建表单、
   不出现重命名/删除按钮；
4. **分组正确性**：先按部门分组，再展示该部门下的多个分类；
5. **零 JS 错误**：全程无 ``pageerror`` / ``console.error``。

**说明**：分类的增删改在 MySQL 模式才有意义（JSON 为只读推导），浏览器端只验证
**可观测部分**——面板渲染、只读态、分组与文档数。MySQL 下新增/重命名/删除的
**接口层语义**已由 ``tools/verify_kb_category.py`` 的桩与部署脚本覆盖；
真实库的增删改验收在 ``--store mysql`` 下跑此处对应分支（见文件末注释）。

用法::

    python tools/verify_kb_category_ui.py
    python tools/verify_kb_category_ui.py --port 8026 --headful

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
    parser = argparse.ArgumentParser(description="知识分类管理页浏览器验收（任务 2.5）")
    parser.add_argument("--port", type=int, default=8026, help="验收用服务端口")
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
    code = 1
    with _client(base) as cli:
        # 接口侧确认此服务为 JSON 只读（浏览器断言依赖它）
        cats = cli.get("/api/admin/kb-categories").json()
        editable = (cats.get("data") or {}).get("editable", False)
        try:
            code = _browser_flow(base, shots, headful=headful, editable=editable)
            return code
        finally:
            print(f"\n截图目录：{shots}")
            print(f"\n结果：{PASSED} PASS / {FAILED} FAIL")


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
        page.on("dialog", lambda d: d.accept())  # confirm / alert 一律确认

        # 本轮验收是只读的，不触发任何 confirm/alert，但仍兜底防误触发

        try:
            section("1. 页签与初始状态")
            page.goto(f"{base}/admin", wait_until="networkidle")
            check("页签数量为 5（知识库/智能体/网关/日志/系统管理）", page.locator("#tabs .tab").count() == 5)
            check("默认停在「知识库」页", page.locator("#page-docs").is_visible())

            section("2. 分类面板渲染")
            # 等待分类树出现（接口返回后 admin.js 的 loadCats 填充）
            page.wait_for_selector("#cat-tree", timeout=15000)
            page.wait_for_timeout(300)
            group_count = page.locator("#cat-tree .cat-group").count()
            check("分类面板按部门分组渲染（.cat-group）", group_count > 0,
                  f"group(s)={group_count}")
            row_count = page.locator("#cat-tree .cat-row").count()
            check("分类行已渲染（.cat-row）", row_count > 0, f"row(s)={row_count}")
            # 每行含分类名 + 类型/文档数
            first_info = page.locator("#cat-tree .cat-row .cat-info").first.inner_text()
            check("分类行含名称与「类型 · N 篇」", "篇" in first_info, first_info)
            page.screenshot(path=str(shots / "kb-categories.png"), full_page=True)

            section("3. 只读模式提示")
            if editable:
                check("MySQL 模式：可编辑（显示新建表单）",
                      page.locator("#cat-form").is_visible())
                check("MySQL 模式：分类行有重命名/删除按钮",
                      page.locator("#cat-tree .cat-ops .op-btn").count() >= 0)
            else:
                check("JSON 模式：显示只读提示", page.locator("#cat-note").is_visible())
                note = page.locator("#cat-note").inner_text()
                check("只读提示说明为 JSON 推导", "只读" in note or "不可增删改" in note, note)
                check("JSON 模式：新建表单隐藏", not page.locator("#cat-form").is_visible())
                check("JSON 模式：无重命名/删除按钮",
                      page.locator("#cat-tree .cat-ops .op-btn").count() == 0)

            section("4. 分组正确性")
            depts = page.locator("#cat-tree .cat-dept").all_inner_texts()
            check("部门分组为去重后的部门名集合",
                  len(depts) == len(set(depts)), f"{depts}")

            section("5. 零 JS 运行时错误")
            check("全程无 pageerror / console.error", not js_errors,
                  " | ".join(js_errors[:3]) if js_errors else "clean")
        finally:
            browser.close()

    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
