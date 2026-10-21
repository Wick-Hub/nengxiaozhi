"""通行查询智能体浏览器验收（任务 4.4 / P-DQ-04、I-04）。

真实 Chromium 覆盖 HTTP 层看不到的行为：

1. 空库首屏提示「尚未同步」，点击同步后 KPI / 趋势 / 排行 / 明细全部刷新；
2. 看板各区块渲染数量正确，学工号脱敏可见；
3. 筛选（方向 / 异常 / 关键词）与重置、明细分页；
4. 校区行点击下钻，展示学院 / 门禁点 / 人员构成；
5. 报告预览 / Markdown 下载、CSV 导出（截断提示）均可用；
6. 后台顶栏存在「通行分析」入口；
7. 全程零 ``pageerror`` / ``console.error``。

**数据隔离**：验收前备份 ``data/access_events.json``，以空库启动，结束后恢复。

用法::

    python tools/verify_access_ui.py
    python tools/verify_access_ui.py --headful

退出码：全部通过为 0，存在 FAIL 为 1。
"""

from __future__ import annotations

import argparse
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
SYNC_DAYS = 10
EXPORT_LIMIT = 100


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


def main() -> int:
    parser = argparse.ArgumentParser(description="通行查询智能体浏览器验收（任务 4.4）")
    parser.add_argument("--port", type=int, default=8047, help="验收用服务端口")
    parser.add_argument("--headful", action="store_true", help="显示浏览器窗口（调试用）")
    parser.add_argument("--shots-dir", default="", help="截图目录（默认 kb-service/artifacts）")
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    from tools._server import read_log, spawn_server, stop_server, wait_port

    data_dir = SERVICE_DIR / "data"
    events_path = data_dir / "access_events.json"
    tmp_dir = Path(tempfile.mkdtemp(prefix="nxz-access-ui-"))
    backup = _backup(events_path, tmp_dir)
    if events_path.exists():
        events_path.unlink()

    port = args.port
    base = f"http://127.0.0.1:{port}"
    shots = Path(args.shots_dir) if args.shots_dir else SERVICE_DIR / "artifacts"
    shots.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env.update({
        "KB_STORE": "json",
        "AUTH_ENABLED": "false",
        "RATE_LIMIT_ENABLED": "false",
        "LLM_MOCK": "1",
        "ACCESS_SYNC_PROVIDER": "mock",
        "ACCESS_SYNC_DAYS": str(SYNC_DAYS),
        "ACCESS_EXPORT_LIMIT": str(EXPORT_LIMIT),
    })

    print(f"启动服务 127.0.0.1:{port}（KB_STORE=json, AUTH_ENABLED=false）…")
    proc, log_path = spawn_server(port, env, SERVICE_DIR)
    try:
        if not wait_port(port, timeout=60):
            print("服务启动失败，日志尾部：")
            print(read_log(log_path, 3000))
            return 1

        errors: list[str] = []

        def watch(target) -> None:  # noqa: ANN001
            target.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))

            def on_console(msg) -> None:  # noqa: ANN001
                if msg.type == "error":
                    errors.append(f"console.error: {msg.text}")

            target.on("console", on_console)

        def kpi(page, selector: str) -> int:  # noqa: ANN001
            return int(page.locator(selector).inner_text().replace(",", "").strip() or 0)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not args.headful)
            ctx = browser.new_context(viewport={"width": 1440, "height": 1000})
            page = ctx.new_page()
            watch(page)
            admin = None
            try:
                section("1. 空库首屏与同步")
                page.goto(f"{base}/access", wait_until="networkidle")
                page.wait_for_selector("#ac-meta", timeout=15000)
                page.wait_for_function(
                    "() => document.getElementById('ac-meta').textContent.includes('尚未同步')",
                    timeout=15000,
                )
                check("空库提示尚未同步",
                      "尚未同步" in page.locator("#ac-meta").inner_text())
                check("空库总量为 0", kpi(page, "#ac-total") == 0)
                check("空库显示明细空态",
                      page.locator("#ac-event-empty").is_visible())

                page.click("#ac-sync")
                page.wait_for_function(
                    "() => document.getElementById('ac-msg').textContent.includes('同步完成')",
                    timeout=30000,
                )
                page.wait_for_function(
                    "() => document.querySelectorAll('#ac-event-rows tr').length > 0",
                    timeout=30000,
                )
                sync_msg = page.locator("#ac-msg").inner_text()
                check("同步完成提示",
                      "演示数据" in sync_msg, sync_msg[-120:])
                total = kpi(page, "#ac-total")
                entries = kpi(page, "#ac-entries")
                exits = kpi(page, "#ac-exits")
                check("同步后总量为正", total > 0, str(total))
                check("方向合计等于总量", entries + exits == total,
                      f"{entries}+{exits}={total}")
                check("同步元信息已刷新",
                      "本地流水" in page.locator("#ac-meta").inner_text(),
                      page.locator("#ac-meta").inner_text()[:80])

                section("2. 看板渲染")
                check("KPI 去重人数为正", kpi(page, "#ac-persons") > 0)
                check("KPI 异常通行非负", kpi(page, "#ac-abnormal") >= 0)
                check("KPI 高峰时段已填充",
                      page.locator("#ac-peak").inner_text().strip() not in ("", "-"),
                      page.locator("#ac-peak").inner_text())

                check("逐日趋势 30 根柱",
                      page.locator("#ac-trend .ac-trend-col").count() == 30,
                      str(page.locator("#ac-trend .ac-trend-col").count()))
                check("24 小时分布 24 根柱",
                      page.locator("#ac-hourly .ac-hour-col").count() == 24)
                check("校区分布 3 行",
                      page.locator("#ac-campus-rows .ac-bar-row").count() == 3,
                      str(page.locator("#ac-campus-rows .ac-bar-row").count()))
                check("学院 TOP5 最多 5 行",
                      page.locator("#ac-college-rows .ac-bar-row").count() <= 5)
                check("门禁点压力最多 8 行",
                      page.locator("#ac-gate-rows .ac-bar-row").count() <= 8)

                person_rows = page.locator("#ac-person-rows tr")
                check("个人 TOP10 有数据",
                      0 < person_rows.count() <= 10, str(person_rows.count()))
                person_no = page.locator("#ac-person-rows tr td:nth-child(4)").first.inner_text()
                check("个人学工号脱敏", "*" in person_no, person_no)
                check("异常区域有统计或空态",
                      page.locator("#ac-reason-rows").inner_text().strip() != "")
                check("明细首批 20 条",
                      page.locator("#ac-event-rows tr").count() == 20,
                      str(page.locator("#ac-event-rows tr").count()))
                first_page_label = page.locator("#ac-page-label").inner_text()
                check("分页标签正确", "第 1 /" in first_page_label, first_page_label)
                page.screenshot(path=str(shots / "access-dashboard.png"), full_page=True)

                section("3. 筛选与分页")
                page.select_option("#ac-direction", "in")
                page.click("#ac-apply")
                page.wait_for_function(
                    "() => document.getElementById('ac-exits').textContent.trim() === '0'",
                    timeout=15000,
                )
                check("方向筛选后离开为 0", kpi(page, "#ac-exits") == 0)
                check("方向筛选后进入等于总量",
                      kpi(page, "#ac-entries") == kpi(page, "#ac-total"))
                labels = page.locator("#ac-event-rows tr td:nth-child(2)").all_inner_texts()
                check("明细方向均为进入",
                      bool(labels) and all("进入" in text for text in labels))

                page.click("#ac-reset")
                page.wait_for_function(
                    "() => document.getElementById('ac-exits').textContent.trim() !== '0'",
                    timeout=15000,
                )
                check("重置恢复全部方向", kpi(page, "#ac-exits") > 0)

                page.select_option("#ac-abnormal-filter", "true")
                page.click("#ac-apply")
                page.wait_for_function(
                    "() => { const t = document.getElementById('ac-total').textContent;"
                    " const a = document.getElementById('ac-abnormal').textContent;"
                    " return t !== '0' && t === a; }",
                    timeout=15000,
                )
                check("异常筛选后异常数等于总量",
                      kpi(page, "#ac-abnormal") == kpi(page, "#ac-total"))
                abnormal_text = page.locator("#ac-event-rows tr td:nth-child(8)").first.inner_text()
                check("异常明细不显示正常", "正常" not in abnormal_text, abnormal_text)
                page.click("#ac-reset")
                page.wait_for_function(
                    "() => document.getElementById('ac-exits').textContent.trim() !== '0'",
                    timeout=15000,
                )

                name = page.locator("#ac-person-rows tr td:nth-child(2) b").first.inner_text()
                page.fill("#ac-q", name)
                page.press("#ac-q", "Enter")
                page.wait_for_function(
                    "() => document.getElementById('ac-total').textContent.trim() !== '0'",
                    timeout=15000,
                )
                check("姓名关键词筛选有结果", kpi(page, "#ac-total") > 0, name)
                page.click("#ac-reset")
                page.wait_for_function(
                    "() => document.getElementById('ac-exits').textContent.trim() !== '0'",
                    timeout=15000,
                )
                page.wait_for_function(
                    "() => {"
                    " const t = document.getElementById('ac-total').textContent.replace(/,/g, '');"
                    " const e = document.getElementById('ac-event-total').textContent.replace(/,/g, '');"
                    " return t !== '0' && e.startsWith(t)"
                    " && document.getElementById('ac-page-label').textContent.includes('第 1');"
                    "}",
                    timeout=15000,
                )

                first_time = page.locator("#ac-event-rows tr td:nth-child(1)").first.inner_text()
                page.click("#ac-next")
                page.wait_for_function(
                    "() => document.getElementById('ac-page-label').textContent.includes('第 2')",
                    timeout=15000,
                )
                second_time = page.locator("#ac-event-rows tr td:nth-child(1)").first.inner_text()
                check("下一页切换成功", second_time != first_time,
                      f"{first_time} → {second_time}")
                page.click("#ac-prev")
                page.wait_for_function(
                    "() => document.getElementById('ac-page-label').textContent.includes('第 1')",
                    timeout=15000,
                )
                check("上一页返回成功",
                      page.locator("#ac-event-rows tr td:nth-child(1)").first.inner_text()
                      == first_time)

                section("4. 层级下钻")
                campus_name = page.locator("#ac-campus-rows .ac-bar-row .ac-bar-name").first.inner_text()
                page.locator("#ac-campus-rows .ac-bar-row").first.click()
                page.wait_for_selector("#ac-drill-card:not([hidden])", timeout=15000)
                drill_title = page.locator("#ac-drill-title").inner_text()
                check("下钻卡片展开", "校区下钻" in drill_title, drill_title)
                check("下钻标题含校区名", campus_name in drill_title)
                check("下钻 KPI 四项",
                      page.locator("#ac-drill-kpis .ac-kpi").count() == 4)
                check("下钻学院构成非空",
                      page.locator("#ac-drill-colleges .ac-bar-row").count() > 0)
                check("下钻门禁点构成非空",
                      page.locator("#ac-drill-gates .ac-bar-row").count() > 0)
                check("下钻人员 TOP5 有数据",
                      0 < page.locator("#ac-drill-persons .ac-recent-row").count() <= 5)
                page.screenshot(path=str(shots / "access-drilldown.png"), full_page=True)
                page.click("#ac-drill-close")
                check("下钻卡片可收起", page.locator("#ac-drill-card").is_hidden())

                section("5. 智能报告与导出")
                page.click("#ac-report")
                page.wait_for_selector("#ac-report-modal:not([hidden])", timeout=20000)
                content = page.locator("#ac-report-content").inner_text()
                check("报告弹窗打开", page.locator("#ac-report-modal").is_visible())
                for marker in ("校园通行智能分析报告", "总体概况", "隐私说明", "结论与建议"):
                    check(f"报告含「{marker}」", marker in content)
                check("报告不含原始学工号", "20230001" not in content)
                check("报告文件名展示",
                      page.locator("#ac-report-msg").inner_text().endswith(".md"),
                      page.locator("#ac-report-msg").inner_text())

                with page.expect_download(timeout=20000) as report_dl:
                    page.click("#ac-report-download")
                report_file = report_dl.value
                report_path = shots / "access-report.md"
                report_file.save_as(str(report_path))
                report_text = report_path.read_text(encoding="utf-8")
                check("Markdown 下载文件名正确",
                      report_file.suggested_filename.endswith(".md"),
                      report_file.suggested_filename)
                check("下载内容为报告",
                      "校园通行智能分析报告" in report_text and len(report_text) > 1000,
                      str(len(report_text)))
                page.keyboard.press("Escape")
                page.wait_for_selector("#ac-report-modal", state="hidden", timeout=5000)
                check("Esc 关闭报告弹窗", page.locator("#ac-report-modal").is_hidden())

                with page.expect_download(timeout=20000) as csv_dl:
                    page.click("#ac-csv")
                csv_file = csv_dl.value
                csv_path = shots / "access-events.csv"
                csv_file.save_as(str(csv_path))
                raw = csv_path.read_bytes()
                csv_text = raw.decode("utf-8-sig")
                lines = csv_text.strip().splitlines()
                check("CSV 下载文件名正确",
                      csv_file.suggested_filename.endswith(".csv"),
                      csv_file.suggested_filename)
                check("CSV 带 BOM", raw.startswith(b"\xef\xbb\xbf"))
                check("CSV 表头正确",
                      lines[0].startswith("时间,方向,校区,学院,门禁点"))
                check("CSV 受单次上限截断", len(lines) == EXPORT_LIMIT + 1,
                      str(len(lines)))
                check("CSV 学工号脱敏",
                      "****" in csv_text and "20230001" not in csv_text)
                page.wait_for_function(
                    "() => document.getElementById('ac-msg').textContent.includes('明细已导出')",
                    timeout=5000,
                )
                check("导出截断提示",
                      "截断" in page.locator("#ac-msg").inner_text(),
                      page.locator("#ac-msg").inner_text())

                section("6. 后台入口")
                admin = ctx.new_page()
                watch(admin)
                admin.goto(f"{base}/admin", wait_until="networkidle")
                entry = admin.locator('a[href="/access"]')
                check("后台顶栏有「通行分析」入口", entry.count() == 1)
                check("入口文案正确", entry.inner_text().strip() == "通行分析",
                      entry.inner_text().strip())

                section("7. 运行时错误")
                check("零 pageerror / console.error", not errors, "; ".join(errors[:3]))
            finally:
                if admin is not None:
                    try:
                        admin.close()
                    except Exception:  # noqa: BLE001
                        pass
                browser.close()

        print(f"\n截图与下载目录：{shots}")
        print(f"\n结果：{PASSED} PASS / {FAILED} FAIL")
        return 1 if FAILED else 0
    finally:
        stop_server(proc)
        _restore(events_path, backup)
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
