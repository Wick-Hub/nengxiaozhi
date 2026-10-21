"""导办事项与免登录票据浏览器验收（任务 4.5 / P-DB-01·02、M-G-01）。

真实 Chromium 覆盖 HTTP 层看不到的行为：

1. 用户端提问后出现事项卡片，卡片带部门 / 标签 / 匹配理由；
2. 点击「去办理」弹出大厅页，票据被一次性兑换并展示登录身份；
3. 地址栏票据被立即移除，重放票据显示「已使用」，缺票进入错误态；
4. 大厅模拟表单可提交并给出演示受理号；
5. 后台 `/gov`：同步、指标、筛选、新建、编辑、停用 / 启用、删除；
6. 全程零 ``pageerror`` / ``console.error``。

**数据隔离**：验收前备份 ``data/gov_items.json`` / ``data/gov_tickets.json``，
以空数据启动，结束后恢复。

用法::

    python tools/verify_gov_ui.py
    python tools/verify_gov_ui.py --headful

退出码：全部通过为 0，存在 FAIL 为 1。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
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
APP_PREFIX = "vf-ui-gov-"


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


def _backup(path: Path, backup_dir: Path) -> Path | None:
    if not path.is_file():
        return None
    target = backup_dir / path.name
    shutil.copy2(path, target)
    return target


def _restore(path: Path, backup: Path | None) -> None:
    if backup is None:
        if path.exists():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup, path)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".verify.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _used_ticket(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return ""
    for ticket_id, record in (data.get("tickets") or {}).items():
        if isinstance(record, dict) and record.get("usedAt"):
            return ticket_id
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="导办事项与票据浏览器验收（任务 4.5）")
    parser.add_argument("--port", type=int, default=8046, help="验收用服务端口")
    parser.add_argument("--headful", action="store_true", help="显示浏览器窗口（调试用）")
    parser.add_argument("--shots-dir", default="", help="截图目录（默认 kb-service/artifacts）")
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    from tools._server import read_log, spawn_server, stop_server, wait_port

    data_dir = SERVICE_DIR / "data"
    items_path = data_dir / "gov_items.json"
    tickets_path = data_dir / "gov_tickets.json"
    tmp_dir = Path(tempfile.mkdtemp(prefix="nxz-gov-ui-"))
    backups = {
        "items": _backup(items_path, tmp_dir),
        "tickets": _backup(tickets_path, tmp_dir),
    }
    _write_json(items_path, {"items": {}, "nextId": 1})
    _write_json(tickets_path, {"tickets": {}})

    port = args.port
    base = f"http://127.0.0.1:{port}"
    shots = Path(args.shots_dir) if args.shots_dir else SERVICE_DIR / "artifacts"
    shots.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["KB_STORE"] = "json"
    env["AUTH_ENABLED"] = "false"  # 演示模式：聚焦卡片 / 票据 / 管理页交互
    env["RATE_LIMIT_ENABLED"] = "false"
    env["LLM_MOCK"] = "1"
    env["GOV_SYNC_PROVIDER"] = "mock"

    print(f"启动服务 127.0.0.1:{port}（KB_STORE=json, AUTH_ENABLED=false）…")
    proc, log_path = spawn_server(port, env, SERVICE_DIR)
    try:
        if not wait_port(port, timeout=60):
            print("服务启动失败，日志尾部：")
            print(read_log(log_path, 3000))
            return 1

        import httpx

        with httpx.Client(timeout=20, trust_env=False, base_url=base) as cli:
            seeded = cli.post("/api/admin/gov/sync")
            if seeded.status_code != 200:
                print("初始化导办事项失败：", seeded.status_code, seeded.text[:300])
                return 1

        errors: list[str] = []

        def watch(target, ignore: tuple[str, ...] = ()) -> None:  # noqa: ANN001
            target.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))

            def on_console(msg) -> None:  # noqa: ANN001
                if msg.type == "error" and not any(k in msg.text for k in ignore):
                    errors.append(f"console.error: {msg.text}")

            target.on("console", on_console)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not args.headful)
            ctx = browser.new_context(viewport={"width": 1440, "height": 950})
            page = ctx.new_page()
            watch(page)
            page.on("dialog", lambda d: d.accept())
            popup = None
            replay = None
            empty = None
            admin = None
            try:
                section("1. 对话事项卡片")
                page.goto(f"{base}/", wait_until="networkidle")
                page.wait_for_selector("#input", timeout=15000)
                page.select_option("#role", "本科生")
                page.fill("#input", "校园卡丢了怎么补办？")
                page.click("#send")
                page.wait_for_selector(".guide-cards .guide-card", timeout=30000)

                check("回答下方出现事项卡片",
                      page.locator(".guide-cards .guide-card").count() >= 1,
                      str(page.locator(".guide-cards .guide-card").count()))
                card = page.locator(".guide-card", has_text="校园卡补办").first
                check("命中「校园卡补办」", card.count() == 1)
                card_text = card.inner_text()
                check("卡片展示主管部门", "信网中心" in card_text, card_text[:60])
                check("卡片展示标签", "校园卡" in card_text)
                check("卡片展示匹配理由", "匹配" in card_text)
                check("卡片按钮为去办理",
                      card.locator(".guide-go").inner_text().strip() == "去办理")
                page.screenshot(path=str(shots / "gov-guide-card.png"), full_page=True)

                section("2. 票据跳转与大厅身份")
                with page.expect_popup(timeout=15000) as pop_info:
                    card.locator(".guide-go").click()
                popup = pop_info.value
                watch(popup)
                popup.wait_for_selector("#state-ok:not([hidden])", timeout=20000)
                check("大厅核验成功", popup.locator("#ok-item").inner_text() == "校园卡补办",
                      popup.locator("#ok-item").inner_text())
                check("大厅展示办理人", popup.locator("#ok-name").inner_text() == "演示用户",
                      popup.locator("#ok-name").inner_text())
                check("大厅展示学工号", popup.locator("#ok-no").inner_text() == "demo",
                      popup.locator("#ok-no").inner_text())
                check("大厅展示身份", popup.locator("#ok-role").inner_text() == "本科生",
                      popup.locator("#ok-role").inner_text())
                check("大厅展示主管部门", popup.locator("#ok-dept").inner_text() == "信网中心")
                check("大厅展示办理条件",
                      "校园卡" in popup.locator("#ok-conditions").inner_text())
                check("大厅展示所需材料",
                      "身份证" in popup.locator("#ok-materials").inner_text())
                check("大厅展示票据元数据",
                      "票据签发" in popup.locator("#ok-meta").inner_text())
                check("地址栏票据已移除", "ticket=" not in popup.url, popup.url)
                popup.screenshot(path=str(shots / "gov-hall-success.png"), full_page=True)

                popup.fill("#apply-phone", "13800000000")
                popup.fill("#apply-note", "校园卡在图书馆遗失")
                popup.click("#apply-submit")
                popup.wait_for_selector("#apply-result:not([hidden])", timeout=5000)
                check("模拟表单可提交",
                      "申请已受理" in popup.locator("#apply-result").inner_text())
                check("提交后按钮锁定",
                      popup.locator("#apply-submit").inner_text() == "已提交"
                      and popup.locator("#apply-submit").is_disabled())

                section("3. 票据重放与缺票")
                ticket = _used_ticket(tickets_path)
                check("票据已落库并标记已用", bool(ticket), ticket[:10])
                replay = ctx.new_page()
                # 重放是刻意构造的负向用例，Chromium 会为 409 打一条资源加载错误
                watch(replay, ignore=("409",))
                replay.goto(f"{base}/hall?ticket={ticket}", wait_until="networkidle")
                replay.wait_for_selector("#state-error:not([hidden])", timeout=15000)
                check("重放票据进入错误态",
                      replay.locator("#err-title").inner_text() == "票据不可用",
                      replay.locator("#err-title").inner_text())
                check("重放提示票据已使用",
                      "已使用" in replay.locator("#err-desc").inner_text(),
                      replay.locator("#err-desc").inner_text())
                replay.screenshot(path=str(shots / "gov-hall-replay.png"), full_page=True)

                empty = ctx.new_page()
                watch(empty)
                empty.goto(f"{base}/hall", wait_until="networkidle")
                empty.wait_for_selector("#state-error:not([hidden])", timeout=15000)
                check("无票据提示重新办理",
                      "缺少办理票据" in empty.locator("#err-title").inner_text(),
                      empty.locator("#err-title").inner_text())

                section("4. 后台导办事项页")
                admin = ctx.new_page()
                watch(admin)
                admin.on("dialog", lambda d: d.accept())
                admin.goto(f"{base}/admin", wait_until="networkidle")
                entry = admin.locator('a[href="/gov"]')
                check("后台顶栏有「导办事项」入口",
                      entry.count() == 1 and entry.inner_text().strip() == "导办事项")
                entry.click()
                admin.wait_for_url("**/gov", timeout=10000)
                admin.wait_for_selector("#gv-tbody tr", timeout=15000)
                check("列表加载 9 条事项",
                      admin.locator("#gv-tbody tr").count() == 9)
                check("KPI 总量为 9", admin.locator("#gv-total").inner_text() == "9",
                      admin.locator("#gv-total").inner_text())
                check("KPI 需票据为 8", admin.locator("#gv-ticket").inner_text() == "8",
                      admin.locator("#gv-ticket").inner_text())

                admin.click("#gv-sync")
                admin.wait_for_function(
                    "() => document.getElementById('gv-sync-note').textContent.includes('同步完成')",
                    timeout=15000,
                )
                check("重复同步不新增",
                      "新增 0" in admin.locator("#gv-sync-note").inner_text()
                      and "更新 9" in admin.locator("#gv-sync-note").inner_text(),
                      admin.locator("#gv-sync-note").inner_text())
                admin.screenshot(path=str(shots / "gov-admin.png"), full_page=True)

                section("5. 新建 / 编辑 / 启停 / 删除")
                name = f"{APP_PREFIX}验收事项"
                admin.click("#gv-new")
                admin.wait_for_selector("#gv-modal:not([hidden])", timeout=5000)
                admin.fill("#gv-f-name", name)
                admin.select_option("#gv-f-dept", "2")
                admin.fill("#gv-f-url", "/hall")
                admin.fill("#gv-f-tags", "验收关键词，临时事项")
                admin.fill("#gv-f-conditions", "仅用于浏览器验收。")
                admin.fill("#gv-f-materials", "无需材料。")
                admin.uncheck('#gv-f-roles input[value="全部"]')
                admin.check('#gv-f-roles input[value="本科生"]')
                admin.click("#gv-save")
                admin.wait_for_selector("#gv-modal", state="hidden", timeout=10000)
                admin.wait_for_function(
                    f"document.getElementById('gv-tbody').textContent.includes({name!r})",
                    timeout=10000,
                )
                check("新建事项出现在列表", admin.locator("#gv-tbody tr").count() == 10,
                      str(admin.locator("#gv-tbody tr").count()))
                check("新建事项 KPI 更新", admin.locator("#gv-total").inner_text() == "10")
                row = admin.locator("#gv-tbody tr", has_text=name)
                check("列表展示主管部门", "教务处" in row.inner_text())
                check("列表展示授权角色", "本科生" in row.inner_text())

                admin.fill("#gv-q", APP_PREFIX)
                admin.press("#gv-q", "Enter")
                admin.wait_for_function(
                    "document.querySelectorAll('#gv-tbody tr').length === 1",
                    timeout=10000,
                )
                check("关键词筛选命中", admin.locator("#gv-tbody tr").count() == 1)

                row = admin.locator("#gv-tbody tr", has_text=name)
                row.locator('button[data-op="edit"]').click()
                admin.wait_for_selector("#gv-modal:not([hidden])", timeout=5000)
                edited = f"{APP_PREFIX}验收事项改"
                admin.fill("#gv-f-name", edited)
                admin.click("#gv-save")
                admin.wait_for_selector("#gv-modal", state="hidden", timeout=10000)
                admin.wait_for_function(
                    f"document.getElementById('gv-tbody').textContent.includes({edited!r})",
                    timeout=10000,
                )
                check("编辑后名称更新", admin.locator("#gv-tbody tr").count() == 1)

                row = admin.locator("#gv-tbody tr", has_text=edited)
                row.locator('button[data-op="toggle"]').click()
                admin.wait_for_function(
                    f"document.getElementById('gv-tbody').textContent.includes('已停用')",
                    timeout=10000,
                )
                check("停用后状态徽标更新",
                      "已停用" in admin.locator("#gv-tbody tr").first.inner_text())
                row = admin.locator("#gv-tbody tr", has_text=edited)
                row.locator('button[data-op="toggle"]').click()
                admin.wait_for_function(
                    f"document.getElementById('gv-tbody').textContent.includes('生效中')",
                    timeout=10000,
                )
                check("重新启用后恢复生效",
                      "生效中" in admin.locator("#gv-tbody tr").first.inner_text())

                admin.fill("#gv-q", "")
                admin.press("#gv-q", "Enter")
                admin.wait_for_function(
                    "document.querySelectorAll('#gv-tbody tr').length === 10",
                    timeout=10000,
                )
                row = admin.locator("#gv-tbody tr", has_text=edited)
                row.locator('button[data-op="delete"]').click()
                admin.wait_for_function(
                    "document.querySelectorAll('#gv-tbody tr').length === 9",
                    timeout=10000,
                )
                check("删除后回到 9 条",
                      admin.locator("#gv-tbody tr").count() == 9)
                check("删除事项已消失",
                      admin.locator("#gv-tbody tr", has_text=edited).count() == 0)

                section("6. 运行时错误")
                check("零 pageerror / console.error", not errors, "; ".join(errors[:3]))
            finally:
                for target in (popup, replay, empty, admin):
                    if target is not None:
                        try:
                            target.close()
                        except Exception:  # noqa: BLE001
                            pass
                browser.close()

        print(f"\n截图目录：{shots}")
        print(f"\n结果：{PASSED} PASS / {FAILED} FAIL")
        return 1 if FAILED else 0
    finally:
        stop_server(proc)
        _restore(items_path, backups["items"])
        _restore(tickets_path, backups["tickets"])
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
