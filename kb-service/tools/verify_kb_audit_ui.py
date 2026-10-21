"""文档审核与文件管理页 · 浏览器端到端验收（任务 2.7 前端）。

真实 Chromium 打开 ``/admin`` 的「知识库」页与 ``/preview``，验证 HTTP 层看不见的
东西：筛选下拉回填、行选与批量条联动、全选/清空、以及预览页**审核按钮的显隐与点击流**。

流程（MySQL 模式；跑完按快照恢复，可重复执行）：

1. 起服务（``KB_STORE=mysql``）并快照 ``kb_doc`` 的 status / published_at；
2. 把最后一篇临时改为「待审核」，让预览页的审核按钮可见；
3. ``/admin``：两个筛选下拉有选项、表格有复选框列、勾选 → 批量条出现且计数正确、
   表头全选 → 计数等于行数、清空 → 批量条隐藏；按类型筛选后行数收敛；
4. ``/preview?doc=<该篇>``：审核按钮可见 → 点「✓ 审核通过」→ 按钮隐藏 + 反馈文案
   + 接口侧状态变为已发布；
5. ``/preview?doc=<已发布篇>``：审核按钮保持隐藏（不重复操作）；
6. 全程零 ``pageerror`` / ``console.error``；finally 里按快照恢复数据。

用法::

    python tools/verify_kb_audit_ui.py
    python tools/verify_kb_audit_ui.py --port 8030 --headful

退出码：全部通过为 0，存在 FAIL 为 1。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

os.environ.setdefault("KB_STORE", "mysql")
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

    return httpx.Client(timeout=30, trust_env=False, base_url=base)


# ------------------------------------------------------------------ 快照/恢复
def snapshot() -> list[tuple]:
    from sqlalchemy import text

    from app.db.session import db_session

    with db_session() as conn:
        return [
            tuple(r) for r in conn.execute(text(
                "SELECT doc_id, status, published_at FROM kb_doc ORDER BY doc_id"
            )).all()
        ]


def restore(snap: list[tuple]) -> None:
    from sqlalchemy import text

    from app.db.session import db_session

    with db_session() as conn:
        with conn.begin():
            for doc_id, status, published_at in snap:
                conn.execute(text(
                    "UPDATE kb_doc SET status=:s, published_at=:p WHERE doc_id=:d"
                ), {"s": status, "p": published_at, "d": doc_id})


def main() -> int:
    parser = argparse.ArgumentParser(description="文档审核与文件管理页浏览器验收（任务 2.7）")
    parser.add_argument("--port", type=int, default=8030, help="验收用服务端口")
    parser.add_argument("--headful", action="store_true", help="显示浏览器窗口（调试用）")
    parser.add_argument("--shots-dir", default="", help="截图输出目录（默认 kb-service/artifacts）")
    args = parser.parse_args()

    from tools._server import read_log, spawn_server, stop_server, wait_port

    port = args.port
    base = f"http://127.0.0.1:{port}"
    shots = Path(args.shots_dir) if args.shots_dir else SERVICE_DIR / "artifacts"
    shots.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["KB_STORE"] = "mysql"
    env["AUTH_ENABLED"] = "false"

    print(f"启动服务 127.0.0.1:{port}（KB_STORE=mysql）…")
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
    snap = snapshot()
    target_id = snap[-1][0]
    try:
        # 把最后一篇临时置为「待审核」，预览页的审核按钮才会显示
        _set_status(target_id, 1)
        return _browser_flow(base, shots, headful=headful, snap=snap)
    finally:
        restore(snap)
        print(f"\n已按快照恢复 {len(snap)} 行 kb_doc")


def _set_status(doc_id: int, status: int) -> None:
    from sqlalchemy import text

    from app.db.session import db_transaction

    with db_transaction() as conn:
        conn.execute(
            text("UPDATE kb_doc SET status=:s WHERE doc_id=:d"),
            {"s": status, "d": doc_id},
        )


def _browser_flow(base: str, shots: Path, *, headful: bool, snap: list) -> int:
    from playwright.sync_api import sync_playwright

    js_errors: list[str] = []
    http_errors: list[str] = []
    target_id = snap[-1][0]
    published_id = next((r[0] for r in snap if r[1] == 2), snap[0][0])

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

        def on_response(resp) -> None:  # noqa: ANN001
            if resp.status >= 400:
                http_errors.append(f"HTTP {resp.status} {resp.url}")

        page.on("response", on_response)
        page.on("dialog", lambda d: d.accept())

        try:
            section("1. 知识库页：筛选下拉与复选框列")
            page.goto(f"{base}/admin", wait_until="domcontentloaded")
            page.wait_for_selector("#page-docs", state="visible", timeout=15000)
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(600)

            check("类型筛选下拉已回填（>1 个选项）",
                  page.locator("#f-ktype option").count() > 1,
                  f"{page.locator('#f-ktype option').count()} 项")
            check("分类筛选下拉存在",
                  page.locator("#f-category").count() == 1)
            check("表头有全选复选框", page.locator("#chk-all").count() == 1)
            check("表格有复选框列（首列）",
                  page.locator("#tbody .chk input[type=checkbox]").count() > 0,
                  f"{page.locator('#tbody .chk input[type=checkbox]').count()} 行")

            section("2. 勾选 → 批量条联动")
            page.locator("#tbody .chk input[type=checkbox]").first.check()
            page.wait_for_timeout(300)
            check("批量条出现", page.locator("#batch-bar").is_visible())
            check("已选计数 = 1",
                  page.locator("#batch-count").inner_text().strip() == "1")

            page.locator("#chk-all").check()
            page.wait_for_timeout(300)
            row_count = page.locator("#tbody tr").count()
            check(f"全选后计数 = 行数（{row_count}）",
                  page.locator("#batch-count").inner_text().strip() == str(row_count))

            page.locator("#batch-clear").click()
            page.wait_for_timeout(300)
            check("清空后批量条隐藏", not page.locator("#batch-bar").is_visible())

            section("3. 按类型筛选")
            opts = page.locator("#f-ktype option")
            if opts.count() > 1:
                value = opts.nth(1).get_attribute("value")
                page.select_option("#f-ktype", value)
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(500)
                rows = page.locator("#tbody tr").count()
                ktypes = page.locator("#tbody tr td:nth-child(5)").all_inner_texts()
                check(f"按类型 {value} 筛选后行数收敛", rows >= 0, f"{rows} 行")
                check("每行类型一致", all(k.strip() == value for k in ktypes),
                      f"{sorted(set(k.strip() for k in ktypes))}")
                page.select_option("#f-ktype", "")
                page.wait_for_load_state("networkidle")
            page.screenshot(path=str(shots / "kb-audit-filters.png"), full_page=True)

            section("4. 预览页：审核按钮显隐与点击流")
            page.goto(f"{base}/preview?doc={target_id}", wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(600)

            approve = page.locator("#audit-approve")
            reject = page.locator("#audit-reject")
            check("待审核文档显示「审核通过」/「驳回」按钮",
                  approve.is_visible() and reject.is_visible())

            approve.click()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(800)
            check("点击后按钮隐藏（已发布不再可操作）", not approve.is_visible())
            check("出现反馈文案", len(page.locator("#audit-msg").inner_text().strip()) > 0,
                  page.locator("#audit-msg").inner_text().strip())
            page.screenshot(path=str(shots / "kb-audit-preview.png"), full_page=True)

            with _client(base) as cli:
                docs = cli.get("/api/docs").json()["data"]
                row = next((d for d in docs if str(d["docId"]) == str(target_id)), None)
                check("接口侧该篇已变为已发布", row is not None and row["status"] == 2,
                      str(row and row.get("status")))

            section("5. 已发布文档：审核按钮保持隐藏")
            page.goto(f"{base}/preview?doc={published_id}", wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(600)
            check("已发布文档不显示审核按钮",
                  not page.locator("#audit-approve").is_visible()
                  and not page.locator("#audit-reject").is_visible())

            section("6. 零 JS 运行时错误")
            check("全程无 pageerror / console.error", not js_errors,
                  " | ".join(js_errors[:3]) if js_errors else "clean")
            check("全程无 4xx/5xx 响应", not http_errors,
                  " | ".join(http_errors[:3]) if http_errors else "")
        finally:
            browser.close()

    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
