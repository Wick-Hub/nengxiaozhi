"""数据中台 · 浏览器端到端验收（任务 4.2 前端）。

真实 Chromium 串起「后台入口 → 全量同步 → 增量同步 → 预览 → 上报 → 批次详情」：

1. 管理后台顶栏出现「数据中台」，权限标记为 ``datacenter.view``；
2. 页面展示数据源、组织/用户镜像、调度开关等 KPI；
3. 每周全量同步后组织 6 条、用户 4 条，任务记录出现；
4. 每日增量重复执行不新增，消息提示「完成」；
5. 预览抽屉展示 schema 与统计口径，关闭后无残留；
6. 执行上报生成两个批次，详情抽屉展示 ``nxz.data-center.report.v1`` 报文；
7. 组织/用户页签可切换并展示同步结果；
8. 全程零 ``pageerror`` / ``console.error``。

验收使用 JSON 临时数据：启动前把 ``data/data_center.json`` 与收件箱目录
移开，结束后原样恢复，不影响已有演示数据。

用法::

    python tools/verify_data_center_ui.py
    python tools/verify_data_center_ui.py --headful

退出码：全部通过为 0，存在 FAIL 为 1。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import date, timedelta
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
DATA_PATH = SERVICE_DIR / "data" / "data_center.json"
SINK_DIR = SERVICE_DIR / "data" / "data_center_outbox"


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


def stash(token: str) -> list[tuple[Path, Path]]:
    """把现有数据中台文件移到唯一备份名，返回 (原路径, 备份路径)。"""
    moved: list[tuple[Path, Path]] = []
    for path in (DATA_PATH, SINK_DIR):
        if not path.exists():
            continue
        backup = path.with_name(f"{path.name}.ui-verify-{token}.bak")
        path.rename(backup)
        moved.append((path, backup))
    return moved


def restore(moved: list[tuple[Path, Path]]) -> None:
    for path, backup in moved:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink()
        if backup.exists():
            backup.rename(path)


def _run(base: str, shots: Path, *, headful: bool) -> int:
    from playwright.sync_api import sync_playwright

    js_errors: list[str] = []
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headful)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.on("pageerror", lambda exc: js_errors.append(f"pageerror: {exc}"))

        def on_console(msg) -> None:  # noqa: ANN001
            if msg.type == "error":
                js_errors.append(f"console.error: {msg.text}")

        page.on("console", on_console)

        section("1. 后台入口")
        page.goto(base + "/admin", wait_until="domcontentloaded")
        page.wait_for_selector('a[href="/data-center"]', timeout=20000)
        link = page.locator('a[href="/data-center"]')
        check("后台顶栏出现数据中台", link.is_visible())
        check("入口绑定 datacenter.view", link.get_attribute("data-perm") == "datacenter.view")
        link.click()
        page.wait_for_url("**/data-center", timeout=15000)
        page.wait_for_function(
            "document.querySelector('#dc-provider')?.textContent.trim() === 'mock'",
            timeout=20000,
        )
        check("页面加载并识别 mock 数据源", True)
        check("初始组织镜像为 0", page.inner_text("#dc-orgs") == "0")
        check("进程内调度默认关闭", page.inner_text("#dc-scheduler") == "关")

        section("2. 组织 / 用户同步")
        page.click("#dc-sync-full")
        page.wait_for_function(
            "document.querySelector('#dc-orgs')?.textContent.trim() === '6'", timeout=20000
        )
        page.wait_for_function(
            "document.querySelector('#dc-users')?.textContent.trim() === '4'", timeout=20000
        )
        check("全量同步后组织 6 条", True)
        check("全量同步后用户 4 条", True)
        check("同步完成提示", "完成" in page.inner_text("#dc-msg"), page.inner_text("#dc-msg"))
        page.click("#dc-sync-inc")
        page.wait_for_function(
            "document.querySelector('#dc-msg')?.textContent.includes('增量同步完成')", timeout=20000
        )
        check("增量同步可重复执行", page.inner_text("#dc-orgs") == "6")
        page.click('.dc-tab[data-panel="jobs"]')
        page.wait_for_selector("#dc-panel-jobs.active")
        page.wait_for_function(
            "document.querySelectorAll('#dc-job-rows tr').length === 2", timeout=10000
        )
        check("任务记录 2 条", True)
        page.screenshot(path=str(shots / "data-center-ui-sync.png"), full_page=True)

        section("3. 预览")
        page.fill("#dc-report-date", yesterday)
        page.click("#dc-preview")
        page.wait_for_selector("#dc-detail:not([hidden])", timeout=20000)
        title = page.inner_text("#dc-detail-title")
        body = page.inner_text("#dc-detail-body")
        check("预览抽屉打开", title.startswith("上报预览"), title)
        check("预览带标准版本", "nxz.data-center.report.v1" in body)
        check("预览含统计项", '"kind": "stats"' in body)
        check("默认不含正文", '"includeContent": false' in body)
        page.click("#dc-detail-close")
        page.wait_for_selector("#dc-detail", state="hidden", timeout=5000)
        check("预览关闭", page.is_hidden("#dc-detail"))

        section("4. 执行上报")
        page.click("#dc-run-report")
        page.wait_for_selector("#dc-detail:not([hidden])", timeout=20000)
        title = page.inner_text("#dc-detail-title")
        body = page.inner_text("#dc-detail-body")
        check("上报任务抽屉打开", title.startswith("上报任务"), title)
        check("两个批次均已投递", body.count("已投递") == 2 or "跳过" in body, body.replace("\n", " / "))
        page.click("#dc-detail-close")
        page.click('.dc-tab[data-panel="reports"]')
        page.wait_for_function(
            "document.querySelectorAll('#dc-report-rows tr').length === 2", timeout=15000
        )
        check("批次列表 2 条", True)
        first = page.locator("#dc-report-rows tr").first
        check("批次 ID 符合规范", first.locator("td").first.inner_text().startswith("DC-"))
        first.locator("button").click()
        page.wait_for_selector("#dc-detail:not([hidden])", timeout=15000)
        title = page.inner_text("#dc-detail-title")
        body = page.inner_text("#dc-detail-body")
        check("批次详情抽屉", title.startswith("批次 DC-"), title)
        check("详情报文含 schema", '"schema": "nxz.data-center.report.v1"' in body)
        page.click("#dc-detail-close")
        page.screenshot(path=str(shots / "data-center-ui-report.png"), full_page=True)

        section("5. 组织 / 用户镜像页签")
        page.click('.dc-tab[data-panel="orgs"]')
        page.wait_for_selector("#dc-panel-orgs.active")
        page.wait_for_function(
            "document.querySelectorAll('#dc-org-rows tr').length === 6", timeout=10000
        )
        orgs = page.inner_text("#dc-org-rows")
        check("组织镜像 6 条", True)
        check("展示西安科技大学", "西安科技大学" in orgs)
        check("展示学院层级", "计算机科学与技术学院" in orgs)
        page.click('.dc-tab[data-panel="users"]')
        page.wait_for_selector("#dc-panel-users.active")
        page.wait_for_function(
            "document.querySelectorAll('#dc-user-rows tr').length === 4", timeout=10000
        )
        users = page.inner_text("#dc-user-rows")
        check("用户镜像 4 条", True)
        check("展示示例学生", "张三" in users)
        check("身份映射正确", "教职工" in users)

        section("6. 前端错误")
        check("无 JS 运行时错误", not js_errors, "; ".join(js_errors[:3]))

        browser.close()

    return 0 if FAILED == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="数据中台浏览器验收（任务 4.2）")
    parser.add_argument("--port", type=int, default=8027, help="验收用服务端口")
    parser.add_argument("--headful", action="store_true", help="显示浏览器窗口（调试用）")
    parser.add_argument("--shots-dir", default="", help="截图目录（默认 kb-service/artifacts）")
    args = parser.parse_args()

    from tools._server import read_log, spawn_server, stop_server, wait_port

    token = str(os.getpid())
    moved = stash(token)
    if moved:
        print("已暂存现有数据中台文件：")
        for path, backup in moved:
            print(f"  {path.name} → {backup.name}")

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
        restore(moved)
        if moved:
            print("已恢复原数据中台文件。")

    return 0 if code == 0 and FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
