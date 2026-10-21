"""未识别问题管理页浏览器验收（任务 3.11 / M-S-07～09）。

真实 Chromium 覆盖 HTTP 层看不到的行为：

1. 独立页 ``/unrecognized`` 加载，聚合行、来源/阈值说明与语义簇渲染；
2. 状态 / 来源筛选可联动列表；
3. 详情弹窗展示原始会话（用户原话 + AI 拒答）与同义问法；
4. 批量忽略 / 重新打开改变行状态；
5. 导出 CSV 触发下载；
6. JSON 只读模式下反哺弹窗给出 MySQL 指引且提交按钮禁用；
7. 全程零 ``pageerror`` / ``console.error``。

数据与 :mod:`tools.verify_unrecognized` 共用同一套种子，验收后恢复。
用法::

    python tools/verify_unrecognized_ui.py
    python tools/verify_unrecognized_ui.py --headful --keep

退出码：全部通过为 0，存在 FAIL 为 1。
"""

from __future__ import annotations

import argparse
import os
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
    parser = argparse.ArgumentParser(description="未识别问题管理页浏览器验收（任务 3.11）")
    parser.add_argument("--port", type=int, default=8037, help="验收用服务端口")
    parser.add_argument("--headful", action="store_true", help="显示浏览器窗口（调试用）")
    parser.add_argument("--keep", action="store_true", help="保留验收数据（默认恢复备份）")
    parser.add_argument("--shots-dir", default="", help="截图目录（默认 kb-service/artifacts）")
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    from tools._server import read_log, spawn_server, stop_server, wait_port
    from tools.verify_unrecognized import _backup, _restore, _seed

    data_dir = SERVICE_DIR / "data"
    pool_path = data_dir / "unrecognized.json"
    sessions_path = data_dir / "sessions.json"
    backup_dir = Path(tempfile.mkdtemp(prefix="nxz-unrecognized-ui-"))
    pool_backup = _backup(pool_path, backup_dir)
    sessions_backup = _backup(sessions_path, backup_dir)
    _seed(pool_path, sessions_path)

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
    try:
        if not wait_port(port, timeout=60):
            print("服务启动失败，日志尾部：")
            print(read_log(log_path, 3000))
            return 1

        errors: list[str] = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not args.headful)
            page = browser.new_page(viewport={"width": 1500, "height": 950})
            page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))

            def on_console(msg) -> None:  # noqa: ANN001
                if msg.type == "error":
                    errors.append(f"console.error: {msg.text}")

            page.on("console", on_console)
            page.on("dialog", lambda d: d.accept())
            try:
                section("1. 页面加载与聚合列表")
                page.goto(f"{base}/unrecognized", wait_until="networkidle")
                page.wait_for_selector("#un-body tr", timeout=20000)
                check("表格渲染 2 组", page.locator("#un-body tr").count() == 2,
                      str(page.locator("#un-body tr").count()))
                note = page.locator("#un-note").inner_text()
                check("口径说明含来源与阈值",
                      "弱召回" in note and str(0.35) in note, note[:80])
                # 哈希向量兜底时语义簇不一定合并（合并逻辑由单测用假向量覆盖），
                # 这里只要求每行渲染出所属簇，不强制出现跨组语义簇。
                check("聚合行渲染所属语义簇",
                      page.locator("#un-body .un-cluster").count() >= 1,
                      str(page.locator("#un-body .un-cluster").count()))
                check("JSON 模式提示反哺需 MySQL",
                      "MySQL" in page.locator("#un-hint").inner_text())
                page.screenshot(path=str(shots / "unrecognized-ui.png"), full_page=True)

                section("2. 筛选")
                page.select_option("#un-source", "refused")
                page.click("#un-search")
                page.wait_for_timeout(600)
                check("按来源筛选为 1 组", page.locator("#un-body tr").count() == 1,
                      str(page.locator("#un-body tr").count()))
                page.click("#un-reset")
                page.wait_for_timeout(600)
                check("重置恢复 2 组", page.locator("#un-body tr").count() == 2,
                      str(page.locator("#un-body tr").count()))

                card_row = page.locator("#un-body tr", has_text="校园卡丢了怎么补办")
                check("同义问法合并为一行", card_row.count() == 1, str(card_row.count()))

                section("3. 详情追溯")
                card_row.locator("[data-detail]").click()
                page.wait_for_selector("#un-detail:not([hidden])", timeout=10000)
                page.wait_for_timeout(400)
                detail_text = page.locator("#un-detail-session").inner_text()
                check("弹窗标题为问题原文",
                      "校园卡丢了怎么补办" in page.locator("#un-detail-title").inner_text())
                check("展示用户原话", "校园卡丢了怎么补办" in detail_text)
                check("展示 AI 拒答", page.locator("#un-detail-session .un-bubble.refused").count() == 1)
                check("同义问法 2 条", page.locator("#un-variants .un-variant").count() == 2,
                      str(page.locator("#un-variants .un-variant").count()))

                section("4. JSON 模式反哺门禁")
                page.click("#un-detail-feedback")
                page.wait_for_selector("#un-feedback:not([hidden])", timeout=10000)
                banner = page.locator("#un-feedback-banner").inner_text()
                check("反哺弹窗提示只读模式", "只读" in banner, banner[:80])
                check("JSON 模式提交按钮禁用", page.locator("#un-feedback-submit").is_disabled())
                page.click("#un-feedback-cancel")
                page.wait_for_timeout(200)
                page.click("#un-detail-close-btn")
                page.wait_for_timeout(200)

                section("5. 批量忽略 / 重新打开")
                card_row.locator("[data-chk]").check()
                page.wait_for_timeout(200)
                check("批量条出现并计数 1",
                      page.locator("#un-batch").is_visible()
                      and page.locator("#un-batch-count").inner_text() == "1")
                page.click("#un-batch-ignore")
                page.wait_for_timeout(900)
                state_text = page.locator("#un-body tr", has_text="校园卡丢了怎么补办") \
                    .locator(".un-state").inner_text()
                check("忽略后状态变为已忽略", state_text == "已忽略", state_text)

                card_row = page.locator("#un-body tr", has_text="校园卡丢了怎么补办")
                card_row.locator("[data-chk]").check()
                page.click("#un-batch-reopen")
                page.wait_for_timeout(900)
                state_text = page.locator("#un-body tr", has_text="校园卡丢了怎么补办") \
                    .locator(".un-state").inner_text()
                check("重新打开后状态变为待处理", state_text == "待处理", state_text)

                section("6. 导出 CSV")
                with page.expect_download(timeout=15000) as dl:
                    page.click("#un-export")
                download = dl.value
                check("导出触发 CSV 下载",
                      download.suggested_filename.lower().endswith(".csv"),
                      download.suggested_filename)

                section("7. 运行时错误")
                check("零 pageerror / console.error", not errors, "; ".join(errors[:3]))
            finally:
                browser.close()

        print(f"\n截图目录：{shots}")
        print(f"\n结果：{PASSED} PASS / {FAILED} FAIL")
        return 1 if FAILED else 0
    finally:
        stop_server(proc)
        if args.keep:
            print(f"验收数据保留：{pool_path}；备份目录：{backup_dir}")
        else:
            _restore(pool_path, pool_backup)
            _restore(sessions_path, sessions_backup)
            print(f"已恢复数据备份（{backup_dir}）")


if __name__ == "__main__":
    raise SystemExit(main())
