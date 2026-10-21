"""系统管理页 · 浏览器端到端验收（任务 2.10 前端）。

真实 Chromium 打开 ``/admin`` 的「系统管理」页签，在 **MySQL 模式**下走完整
增删改与操作日志筛选：

1. 页签：5 个页签，系统管理可进入且只加载一次；
2. 词库：新增词条 → 列表出现 → 状态「启用」；停用 → 状态「停用」；删除 → 行消失；
3. 操作日志：动作筛选「词条新增」能查到刚才的新增记录（操作人 / 明细可见）；
4. 零 JS 错误：全程无 ``pageerror`` / ``console.error``。

用法::

    python tools/verify_sensitive_ui.py
    python tools/verify_sensitive_ui.py --port 8033 --headful

前置：MySQL 已启动且 ``sensitive_word`` 表已迁移（``tools/migrate_sensitive_2_10.py``）。
"""

from __future__ import annotations

import argparse
import os
import random
import string
import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

os.environ.setdefault("KB_STORE", "mysql")
os.environ.setdefault("LLM_MOCK", "1")
os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH", r"C:\Users\wick\.workbuddy-ai\binaries\ms-playwright"
)

try:  # pragma: no cover
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
    parser = argparse.ArgumentParser(description="系统管理页浏览器验收（任务 2.10）")
    parser.add_argument("--port", type=int, default=8033, help="验收用服务端口")
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
    from playwright.sync_api import sync_playwright

    # 词条名不能含 8 位以上连续数字：日志脱敏会把数字段当学号掩码，
    # 那样操作日志明细里的词条名就对不上原文了。
    suffix = "".join(random.choices(string.ascii_uppercase, k=6))
    word = f"界面验收词{suffix}"
    js_errors: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headful)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})

        page.on("pageerror", lambda exc: js_errors.append(f"pageerror: {exc}"))

        def on_console(msg) -> None:  # noqa: ANN001
            if msg.type == "error":
                js_errors.append(f"console.error: {msg.text}")

        page.on("console", on_console)
        page.on("dialog", lambda d: d.accept())

        try:
            section("1. 页签与系统管理入口")
            page.goto(f"{base}/admin", wait_until="networkidle")
            check("页签数量为 5", page.locator("#tabs .tab").count() == 5)
            check("系统管理页签存在", page.locator('#tabs .tab[data-tab="system"]').count() == 1)

            page.click('#tabs .tab[data-tab="system"]')
            page.wait_for_selector("#page-system", state="visible")
            # 词库可能为空：等提示文案出现即视为首屏加载完成
            page.wait_for_function(
                "() => (document.querySelector('#sw-note')?.textContent || '').trim().length > 0",
                timeout=15000,
            )
            check("切到系统管理页", page.locator("#page-system").is_visible())
            check("MySQL 模式显示词库表单", page.locator("#sw-form").is_visible())
            note = page.locator("#sw-note").inner_text()
            check("提示词条改动即时生效", "即时生效" in note, note)

            section("2. 新增词条")
            page.fill("#sw-word", word)
            page.select_option("#sw-form-kind", "sensitive")
            page.fill("#sw-category", "界面验收")
            page.click("#sw-add")
            page.wait_for_selector(f'#sw-tbody tr:has-text("{word}")', timeout=15000)
            row = page.locator(f'#sw-tbody tr:has-text("{word}")')
            check("新增后列表出现该词条", row.count() == 1)
            check("新词条状态为启用", "启用" in row.inner_text(), row.inner_text())
            check("新增提示出现", "已新增" in page.locator("#sw-msg").inner_text())

            section("3. 停用与删除")
            row.locator('button[data-op="toggle"]').click()
            page.wait_for_function(
                """(w) => [...document.querySelectorAll('#sw-tbody tr')]
                    .some(tr => tr.innerText.includes(w) && tr.innerText.includes('停用'))""",
                arg=word,
                timeout=15000,
            )
            check("点击停用后状态变为停用",
                  "停用" in page.locator(f'#sw-tbody tr:has-text("{word}")').inner_text())

            page.locator(f'#sw-tbody tr:has-text("{word}")').locator('button[data-op="delete"]').click()
            page.wait_for_selector(f'#sw-tbody tr:has-text("{word}")', state="detached", timeout=15000)
            check("删除后行消失", page.locator(f'#sw-tbody tr:has-text("{word}")').count() == 0)

            section("4. 操作日志筛选")
            page.select_option("#op-action", "SENSITIVE_CREATE")
            page.click("#op-search")
            # 旧列表里本来就有历史「词条新增」行，必须等本次词条那一行出现再断言
            page.wait_for_selector(f'#op-tbody tr:has-text("{word}")', timeout=15000)
            op_row = page.locator(f'#op-tbody tr:has-text("{word}")').first
            check("按「词条新增」筛选能查到本次记录", op_row.count() == 1)
            check("日志明细含词条名", word in op_row.inner_text(), op_row.inner_text()[:120])

            page.screenshot(path=str(shots / "system-admin.png"), full_page=True)

            section("5. 零 JS 运行时错误")
            check("全程无 pageerror / console.error", not js_errors,
                  " | ".join(js_errors[:3]) if js_errors else "clean")
        finally:
            browser.close()

    with __import__("httpx").Client(timeout=10, trust_env=False) as cli:
        data = cli.get(f"{base}/api/admin/sensitive-words", params={"q": word}).json().get("data") or {}
        if data.get("items"):
            for it in data["items"]:
                cli.delete(f"{base}/api/admin/sensitive-words/{it['wordId']}")

    print(f"\n截图目录：{shots}")
    print(f"\n结果：{PASSED} PASS / {FAILED} FAIL")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
