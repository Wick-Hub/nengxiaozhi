"""集成中心 · 浏览器端到端验收（任务 4.8 前端）。

真实 Chromium 串起「后台入口 → 示例配置 → 新增/测试/启用/删除 本地模型 →
新增/测试 MCP → 新增/测试第三方智能体 → 校验提示」：

1. 管理后台顶栏出现「集成中心」，权限标记为 ``integrations.view``；
2. 空态下 KPI 全为 0，模型来源显示「网关」；
3. 写入示例配置后 2/2/2 条，重复点击不重复写入；
4. 新增本地模型（带 Key、创建后启用）→ 当前模型切换、密钥显示「已配置」；
5. 测试本地模型 → 结果抽屉显示「通过（mock）」与模拟回复；
6. 把当前模型切回示例配置；
7. 新增 MCP（自定义 Header）→ 只显示 Header 名称，测试得到 3 个工具；
8. 新增 Dify 智能体（Token）→ 显示「已配置」，测试得到模拟回复；
9. 重复编码在表单内提示「已被使用」，删除本地模型后计数回落；
10. 全程零 ``pageerror`` / ``console.error``。

验收使用 JSON 临时数据：启动前把 ``data/integrations.json`` 移开，结束后原样恢复。

用法::

    python tools/verify_integrations_ui.py
    python tools/verify_integrations_ui.py --headful

退出码：全部通过为 0，存在 FAIL 为 1。
"""

from __future__ import annotations

import argparse
import os
import shutil
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
DATA_PATH = SERVICE_DIR / "data" / "integrations.json"


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


def stash(token: str) -> Path | None:
    if not DATA_PATH.exists():
        return None
    backup = DATA_PATH.with_name(f"{DATA_PATH.name}.ui-verify-{token}.bak")
    DATA_PATH.rename(backup)
    return backup


def restore(backup: Path | None) -> None:
    if backup is None:
        return
    if DATA_PATH.exists():
        DATA_PATH.unlink()
    backup.rename(DATA_PATH)


def _row(page, table: str, code: str):  # noqa: ANN001
    return page.locator(f"#{table} tr", has_text=code)


def _run(base: str, shots: Path, *, headful: bool) -> int:
    from playwright.sync_api import sync_playwright

    js_errors: list[str] = []
    #: 第 8 节会故意提交重复编码，浏览器必然为这次 409 记录一条资源加载错误；
    #: 它是负向用例的预期结果，不算前端运行时异常。
    expected_console = (
        "Failed to load resource: the server responded with a status of 409 (Conflict)",
    )

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headful)
        page = browser.new_page(viewport={"width": 1560, "height": 1000})
        page.on("pageerror", lambda exc: js_errors.append(f"pageerror: {exc}"))

        def on_console(msg) -> None:  # noqa: ANN001
            if msg.type != "error":
                return
            if any(text in msg.text for text in expected_console):
                return
            js_errors.append(f"console.error: {msg.text}")

        page.on("console", on_console)
        page.on("dialog", lambda dialog: dialog.accept())

        section("1. 后台入口")
        page.goto(base + "/admin", wait_until="domcontentloaded")
        page.wait_for_selector('a[href="/integrations"]', timeout=20000)
        link = page.locator('a[href="/integrations"]')
        check("后台顶栏出现集成中心", link.is_visible())
        check("入口绑定 integrations.view",
              link.get_attribute("data-perm") == "integrations.view")
        link.click()
        page.wait_for_url("**/integrations", timeout=15000)
        page.wait_for_function(
            "document.querySelector('#ig-provider')?.textContent.trim() === '网关'",
            timeout=20000,
        )
        check("页面加载并显示网关模式", True)
        check("初始本地模型 0", page.inner_text("#ig-local-count") == "0")
        check("初始 MCP 0", page.inner_text("#ig-mcp-count") == "0")
        check("初始智能体 0", page.inner_text("#ig-agent-count") == "0")
        check("测试来源 mock", page.inner_text("#ig-test-mode") == "mock")
        check("空态提示", "尚无本地模型" in page.inner_text("#ig-local-empty"))
        page.screenshot(path=str(shots / "integrations-ui-empty.png"), full_page=True)

        section("2. 写入示例配置")
        page.click("#ig-seed")
        page.wait_for_function(
            "document.querySelector('#ig-local-count')?.textContent.trim() === '2'",
            timeout=20000,
        )
        check("示例本地模型 2 条", page.inner_text("#ig-local-count") == "2")
        check("示例 MCP 2 条", page.inner_text("#ig-mcp-count") == "2")
        check("示例智能体 2 条", page.inner_text("#ig-agent-count") == "2")
        check("已自动启用示例模型",
              "本地 Qwen2.5-7B（示例）" in page.inner_text("#ig-active"),
              page.inner_text("#ig-active"))
        check("提示写入 6 条", "已写入 6 条示例配置" in page.inner_text("#ig-msg"),
              page.inner_text("#ig-msg"))
        rows = page.locator("#ig-local-rows tr")
        check("本地模型表格 2 行", rows.count() == 2, str(rows.count()))
        page.click("#ig-seed")
        page.wait_for_function(
            "document.querySelector('#ig-msg')?.textContent.includes('未重复写入')",
            timeout=15000,
        )
        check("重复写入被跳过", page.inner_text("#ig-local-count") == "2")
        page.screenshot(path=str(shots / "integrations-ui-seeded.png"), full_page=True)

        section("3. 新增本地模型")
        page.click("#ig-add-local")
        page.wait_for_selector("#ig-form:not([hidden])", timeout=5000)
        page.fill("#ig-local-code", "vfui-local")
        page.fill("#ig-local-name", "UI 验收本地模型")
        page.fill("#ig-local-url", "http://127.0.0.1:18081/v1")
        page.fill("#ig-local-model", "ui-model")
        page.fill("#ig-local-key", "sk-ui-verify-secret")
        page.fill("#ig-local-timeout", "25")
        page.check("#ig-local-active")
        check("表单标题为新增本地模型",
              page.inner_text("#ig-form-title") == "新增本地模型")
        page.click("#ig-form-save")
        page.wait_for_selector("#ig-form", state="hidden", timeout=15000)
        page.wait_for_function(
            "document.querySelector('#ig-local-count')?.textContent.trim() === '3'",
            timeout=20000,
        )
        check("新增后本地模型 3 条", True)
        check("提示保存成功", "已保存「UI 验收本地模型」" in page.inner_text("#ig-msg"),
              page.inner_text("#ig-msg"))
        check("新模型成为当前启用",
              page.inner_text("#ig-active") == "UI 验收本地模型",
              page.inner_text("#ig-active"))
        row = _row(page, "ig-local-rows", "vfui-local")
        check("密钥只显示已配置", "已配置" in row.inner_text())
        check("密文不出现在页面", "sk-ui-verify-secret" not in page.content())
        check("显示当前启用徽标", "当前启用" in row.inner_text())

        section("4. 测试本地模型")
        row.get_by_role("button", name="测试").click()
        page.wait_for_selector("#ig-result:not([hidden])", timeout=20000)
        title = page.inner_text("#ig-result-title")
        body = page.inner_text("#ig-result-body")
        check("测试结果抽屉标题", title.startswith("本地模型测试 · UI 验收本地模型"), title)
        check("结果状态为通过（mock）", "状态：通过（mock）" in body)
        check("结果含模拟回复", "回复：pong（模拟回复）" in body)
        page.screenshot(path=str(shots / "integrations-ui-test.png"), full_page=True)
        page.click("#ig-result-close")
        page.wait_for_selector("#ig-result", state="hidden", timeout=5000)
        check("结果抽屉可关闭", page.is_hidden("#ig-result"))
        check("表格测试徽标更新",
              "通过" in _row(page, "ig-local-rows", "vfui-local").inner_text())

        section("5. 切换当前模型")
        seed_row = _row(page, "ig-local-rows", "local-qwen")
        seed_row.get_by_role("button", name="启用").click()
        page.wait_for_function(
            "document.querySelector('#ig-active')?.textContent.includes('本地 Qwen2.5-7B')",
            timeout=15000,
        )
        check("切换回示例模型",
              page.inner_text("#ig-active").startswith("本地 Qwen2.5-7B"),
              page.inner_text("#ig-active"))
        check("切换提示", "已切换当前本地模型" in page.inner_text("#ig-msg"),
              page.inner_text("#ig-msg"))

        section("6. MCP 服务")
        page.click('.ig-tab[data-tab="mcp"]')
        page.wait_for_selector("#ig-panel-mcp.active", timeout=5000)
        page.click("#ig-add-mcp")
        page.wait_for_selector("#ig-form:not([hidden])", timeout=5000)
        page.fill("#ig-mcp-code", "vfui-mcp")
        page.fill("#ig-mcp-name", "UI 验收 MCP")
        page.fill("#ig-mcp-endpoint", "http://127.0.0.1:19081/mcp")
        page.fill("#ig-mcp-headers", "X-Token: ui-secret\nAuthorization: Bearer ui")
        page.click("#ig-form-save")
        page.wait_for_selector("#ig-form", state="hidden", timeout=15000)
        page.wait_for_function(
            "document.querySelector('#ig-mcp-count')?.textContent.trim() === '3'",
            timeout=20000,
        )
        check("新增后 MCP 3 条", True)
        row = _row(page, "ig-mcp-rows", "vfui-mcp")
        text = row.inner_text()
        check("Header 只显示名称",
              "Header：Authorization, X-Token" in text, text.replace("\n", " / "))
        check("Header 值不出现在页面", "ui-secret" not in page.content())
        row.get_by_role("button", name="测试").click()
        page.wait_for_selector("#ig-result:not([hidden])", timeout=20000)
        body = page.inner_text("#ig-result-body")
        check("MCP 测试通过", "状态：通过（mock）" in body)
        check("MCP 返回 3 个工具", "工具数：3" in body)
        check("MCP 工具名可见", "vfui_mcp_search_kb" in body)
        page.click("#ig-result-close")
        page.wait_for_selector("#ig-result", state="hidden", timeout=5000)
        page.screenshot(path=str(shots / "integrations-ui-mcp.png"), full_page=True)

        section("7. 第三方智能体")
        page.click('.ig-tab[data-tab="agent"]')
        page.wait_for_selector("#ig-panel-agent.active", timeout=5000)
        page.click("#ig-add-agent")
        page.wait_for_selector("#ig-form:not([hidden])", timeout=5000)
        page.fill("#ig-agent-code", "vfui-dify")
        page.fill("#ig-agent-name", "UI 验收 Dify")
        page.select_option("#ig-agent-kind", "dify")
        page.fill("#ig-agent-url", "https://dify.example.edu/v1")
        page.fill("#ig-agent-token", "ui-agent-token")
        page.click("#ig-form-save")
        page.wait_for_selector("#ig-form", state="hidden", timeout=15000)
        page.wait_for_function(
            "document.querySelector('#ig-agent-count')?.textContent.trim() === '3'",
            timeout=20000,
        )
        check("新增后智能体 3 条", True)
        row = _row(page, "ig-agent-rows", "vfui-dify")
        text = row.inner_text()
        check("类型标签为 Dify 应用", "Dify 应用" in text)
        check("Token 只显示已配置", "已配置" in text)
        check("Token 不出现在页面", "ui-agent-token" not in page.content())
        row.get_by_role("button", name="测试").click()
        page.wait_for_selector("#ig-result:not([hidden])", timeout=20000)
        body = page.inner_text("#ig-result-body")
        check("智能体测试通过", "状态：通过（mock）" in body)
        check("智能体模拟回复", "pong（Dify 应用 模拟回复）" in body)
        page.screenshot(path=str(shots / "integrations-ui-agent.png"), full_page=True)
        page.click("#ig-result-close")
        page.wait_for_selector("#ig-result", state="hidden", timeout=5000)

        section("8. 校验与删除")
        page.click('.ig-tab[data-tab="local"]')
        page.wait_for_selector("#ig-panel-local.active", timeout=5000)
        page.click("#ig-add-local")
        page.wait_for_selector("#ig-form:not([hidden])", timeout=5000)
        page.fill("#ig-local-code", "vfui-local")
        page.fill("#ig-local-name", "重复编码")
        page.fill("#ig-local-url", "http://127.0.0.1:18082/v1")
        page.fill("#ig-local-model", "dup-model")
        page.click("#ig-form-save")
        page.wait_for_function(
            "document.querySelector('#ig-form-msg')?.textContent.includes('已被使用')",
            timeout=15000,
        )
        check("重复编码表单内提示", "已被使用" in page.inner_text("#ig-form-msg"),
              page.inner_text("#ig-form-msg"))
        page.click("#ig-form-cancel")
        page.wait_for_selector("#ig-form", state="hidden", timeout=5000)

        row = _row(page, "ig-local-rows", "vfui-local")
        row.get_by_role("button", name="删除").click()
        page.wait_for_function(
            "document.querySelector('#ig-local-count')?.textContent.trim() === '2'",
            timeout=15000,
        )
        check("删除后本地模型 2 条", True)
        check("删除提示", "已删除「UI 验收本地模型」" in page.inner_text("#ig-msg"))
        page.screenshot(path=str(shots / "integrations-ui-final.png"), full_page=True)

        section("9. 前端错误")
        check("无 JS 运行时错误", not js_errors, "; ".join(js_errors[:3]))

        browser.close()

    return 0 if FAILED == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="集成中心浏览器验收（任务 4.8）")
    parser.add_argument("--port", type=int, default=8031, help="验收用服务端口")
    parser.add_argument("--headful", action="store_true", help="显示浏览器窗口（调试用）")
    parser.add_argument("--shots-dir", default="", help="截图目录（默认 kb-service/artifacts）")
    args = parser.parse_args()

    from tools._server import read_log, spawn_server, stop_server, wait_port

    token = str(os.getpid())
    backup = stash(token)
    if backup:
        print(f"已暂存现有集成配置：{backup.name}")

    port = args.port
    base = f"http://127.0.0.1:{port}"
    shots = Path(args.shots_dir) if args.shots_dir else SERVICE_DIR / "artifacts"
    shots.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["KB_STORE"] = "json"
    env["AUTH_ENABLED"] = "false"
    env["RATE_LIMIT_ENABLED"] = "false"
    env["LLM_MOCK"] = "1"

    print(f"启动服务 127.0.0.1:{port}（KB_STORE=json）…")
    proc, log_path = spawn_server(port, env, SERVICE_DIR)
    code = 1
    try:
        if not wait_port(port, timeout=60):
            print("服务启动失败，日志尾部：")
            print(read_log(log_path, 3000))
            return 1
        try:
            code = _run(base, shots, headful=args.headful)
        finally:
            print(f"\n截图目录：{shots}")
            print(f"\n结果：{PASSED} PASS / {FAILED} FAIL")
    finally:
        stop_server(proc)
        restore(backup)
        if backup:
            print("已恢复原集成配置。")

    return 0 if code == 0 and FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
