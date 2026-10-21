"""知识点管理页 · 浏览器端到端验收（任务 2.8 前端 / M-K-10）。

真实 Chromium 打开 ``/points``，验证 HTTP 层看不见的东西：筛选下拉回填、
勾选与批量条联动、编辑弹窗、再加工三个面板的真实点击流、新建切片、
批量导出（真实下载事件）、删除确认流，以及**零 JS 运行时错误**。

流程（MySQL 模式；跑完按快照恢复，可重复执行）：

1. 起服务（``KB_STORE=mysql``）并快照目标文档的切片 / 向量 / chunk_count；
2. 列表：筛选项回填、行数与总数一致、勾选 → 批量条计数、全选 → 计数=行数、
   清空 → 批量条隐藏；
3. 检索：关键词收敛行数，重置后恢复；
4. 编辑弹窗：元信息标签、正文回填、**追加文本 → 删内容**（真实写库）→ 保存；
5. 新建切片：填文档编号与正文 → 创建成功、列表多 1 行；
6. 批量导出：勾选新切片 → 导出 CSV → 断言真实下载文件名；
7. 删除：编辑弹窗里删掉临时切片（confirm 自动确认）→ 行数回退；
8. 全程零 ``pageerror`` / ``console.error`` / 非预期 4xx-5xx；finally 按快照恢复。

用法::

    python tools/verify_kb_points_ui.py
    python tools/verify_kb_points_ui.py --port 8032 --headful

退出码：全部通过为 0，存在 FAIL 为 1。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

# 必须在导入 verify_kb_points（它内部 setdefault 成 json）之前定死
os.environ["KB_STORE"] = "mysql"
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


# ------------------------------------------------------------------ 快照/恢复
def _pick_doc() -> int:
    from sqlalchemy import text

    from app.db.session import db_session

    with db_session() as conn:
        row = conn.execute(
            text("SELECT doc_id FROM kb_doc WHERE chunk_count > 0 ORDER BY doc_id LIMIT 1")
        ).first()
    if row is None:
        raise RuntimeError("kb_doc 中没有含切片的文档，无法验收")
    return int(row[0])


def main() -> int:
    parser = argparse.ArgumentParser(description="知识点管理页浏览器验收（任务 2.8）")
    parser.add_argument("--port", type=int, default=8032, help="验收用服务端口")
    parser.add_argument("--headful", action="store_true", help="显示浏览器窗口（调试用）")
    parser.add_argument("--shots-dir", default="", help="截图输出目录（默认 kb-service/artifacts）")
    args = parser.parse_args()

    from tools._server import read_log, spawn_server, stop_server, wait_port
    from tools.verify_kb_points import _restore_doc, _snapshot_doc

    port = args.port
    base = f"http://127.0.0.1:{port}"
    shots = Path(args.shots_dir) if args.shots_dir else SERVICE_DIR / "artifacts"
    shots.mkdir(parents=True, exist_ok=True)

    doc_id = _pick_doc()
    snap = _snapshot_doc(doc_id)
    print(f"目标文档 D{doc_id:03d}：快照 {len(snap['chunks'])} 切片 / "
          f"{len(snap['vecs'])} 向量 / chunk_count={snap['chunkCount']}")

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
        return _browser_flow(base, shots, headful=args.headful, doc_id=doc_id,
                             expected_rows=snap["chunkCount"])
    finally:
        stop_server(proc)
        _restore_doc(doc_id, snap)
        after = _snapshot_doc(doc_id)
        print(f"\n已按快照恢复：切片 {len(after['chunks'])}/{len(snap['chunks'])} · "
              f"向量 {len(after['vecs'])}/{len(snap['vecs'])} · "
              f"chunk_count {after['chunkCount']}/{snap['chunkCount']}")


def _total_of(text: str) -> int:
    """从「（35 条）」里取总数。"""
    import re as _re

    m = _re.search(r"(\d+)", text or "")
    return int(m.group(1)) if m else -1


def _browser_flow(base: str, shots: Path, *, headful: bool, doc_id: int, expected_rows: int) -> int:
    from playwright.sync_api import sync_playwright

    js_errors: list[str] = []
    http_errors: list[str] = []
    added = False

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headful)
        page = browser.new_page(viewport={"width": 1680, "height": 1040})

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
            section("1. 列表：筛选项回填与总数")
            page.goto(f"{base}/points", wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(800)

            check("部门下拉有选项", page.locator("#pt-dept option").count() > 1,
                  f"{page.locator('#pt-dept option').count()} 项")
            check("块类型下拉有选项", page.locator("#pt-block option").count() > 1,
                  f"{page.locator('#pt-block option').count()} 项")
            rows = page.locator("#pt-body tr").count()
            check("表格有数据行", rows > 0, f"{rows} 行")
            count_text = page.locator("#pt-count").inner_text()
            check("总数文案含条数", "条" in count_text, count_text)
            # 列表是分页的（每页 20），所以「多/少一条」只能看总数文案，不能看行数
            total0 = _total_of(count_text)
            check("只读提示未出现（MySQL 模式可编辑）",
                  "只读" not in page.locator("#pt-note").inner_text(),
                  page.locator("#pt-note").inner_text()[:40])
            page.screenshot(path=str(shots / "kb-points-list.png"), full_page=True)

            section("2. 勾选 → 批量条联动")
            page.locator("#pt-body [data-chk]").first.check()
            page.wait_for_timeout(250)
            check("批量条出现", page.locator("#pt-batch").is_visible())
            check("已选计数 = 1", page.locator("#pt-batch-count").inner_text().strip() == "1")

            page.locator("#pt-chk-all").check()
            page.wait_for_timeout(250)
            check(f"全选后计数 = 行数（{rows}）",
                  page.locator("#pt-batch-count").inner_text().strip() == str(rows),
                  page.locator("#pt-batch-count").inner_text())

            page.locator("#pt-batch-clear").click()
            page.wait_for_timeout(250)
            check("清空后批量条隐藏", not page.locator("#pt-batch").is_visible())

            section("3. 关键词检索与重置")
            page.fill("#pt-q", "校园")
            page.click("#pt-search")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(500)
            hit_rows = page.locator("#pt-body tr").count()
            check("检索有结果", hit_rows > 0, f"{hit_rows} 行")
            check("检索后行数不超过总数", hit_rows <= rows, f"{hit_rows} <= {rows}")

            page.click("#pt-reset")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(500)
            check("重置后行数恢复", page.locator("#pt-body tr").count() == rows,
                  f"{page.locator('#pt-body tr').count()} vs {rows}")

            section("4. 编辑弹窗：再加工三件套")
            page.locator("#pt-body [data-edit]").first.click()
            page.wait_for_selector("#pt-modal", state="visible", timeout=10000)
            page.wait_for_timeout(500)

            chunk_id = page.locator("#pt-modal-title").inner_text().split("·")[-1].strip()
            check("弹窗标题含知识点编号", bool(chunk_id), chunk_id)
            check("元信息标签 ≥ 3 个", page.locator("#pt-modal-meta .pt-tag").count() >= 3,
                  f"{page.locator('#pt-modal-meta .pt-tag').count()} 个")
            body0 = page.locator("#pt-f-content").input_value()
            check("正文已回填", bool(body0), f"{len(body0)} 字")
            check("再加工面板默认收起",
                  not page.locator("#pt-op-append").is_visible()
                  and not page.locator("#pt-op-remove").is_visible())

            # 追加文本
            page.click("#pt-op-append-toggle")
            page.wait_for_timeout(200)
            check("「添加文本内容」面板展开", page.locator("#pt-op-append").is_visible())
            page.fill("#pt-append-text", "【UI验收追加】")
            page.click("#pt-append-run")
            page.wait_for_timeout(1200)
            msg = page.locator("#pt-modal-msg").inner_text()
            check("追加成功提示", "已追加" in msg, msg[:40])
            body1 = page.locator("#pt-f-content").input_value()
            check("正文变长且含追加内容",
                  "【UI验收追加】" in body1 and len(body1) == len(body0) + len("【UI验收追加】"),
                  f"{len(body0)} → {len(body1)}")

            # 删除内容（把刚追加的删掉）
            page.click("#pt-op-remove-toggle")
            page.wait_for_timeout(200)
            check("「删除内容」面板展开", page.locator("#pt-op-remove").is_visible())
            page.fill("#pt-remove-text", "【UI验收追加】")
            page.click("#pt-remove-run")
            page.wait_for_timeout(1200)
            msg = page.locator("#pt-modal-msg").inner_text()
            check("删除内容提示", "已删除" in msg, msg[:40])
            body2 = page.locator("#pt-f-content").input_value()
            check("正文恢复原长", len(body2) == len(body0), f"{len(body2)} vs {len(body0)}")

            # 保存
            page.fill("#pt-f-title", "UI验收/临时路径")
            page.click("#pt-save")
            page.wait_for_timeout(1500)
            msg = page.locator("#pt-modal-msg").inner_text()
            check("保存成功提示", "已保存" in msg, msg[:50])
            check("保存后向量同步成功", "重算向量成功" in msg, msg[:50])
            page.screenshot(path=str(shots / "kb-points-editor.png"), full_page=True)

            page.click("#pt-close")
            page.wait_for_timeout(400)
            check("关闭后弹窗隐藏", not page.locator("#pt-modal").is_visible())

            section("5. 新建切片")
            page.click("#pt-new")
            page.wait_for_selector("#pt-create", state="visible", timeout=8000)
            page.fill("#pt-new-doc", f"D{doc_id:03d}")
            page.fill("#pt-new-title", "UI验收/新建")
            page.fill("#pt-new-content", "UI 验收新建的临时知识点，脚本结束会自动清理。")
            page.click("#pt-create-submit")
            page.wait_for_timeout(2000)
            msg = page.locator("#pt-create-msg").inner_text()
            check("创建成功提示", "已创建" in msg, msg[:50])
            added = True
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(800)
            total1 = _total_of(page.locator("#pt-count").inner_text())
            check("总数 +1", total1 == total0 + 1, f"{total0} → {total1}")

            section("6. 批量导出（真实下载）")
            # 勾选新切片：按内容摘要定位（新切片排在同文档末尾，直接勾最后一行的同文档行）
            page.locator("#pt-body [data-chk]").first.check()
            page.wait_for_timeout(250)
            with page.expect_download(timeout=20000) as dl_info:
                page.click("#pt-export-csv")
            download = dl_info.value
            name = download.suggested_filename
            path = download.path()
            check("触发真实下载", path is not None)
            check("文件名是 .csv", name.endswith(".csv"), name)
            size = Path(path).stat().st_size if path else 0
            check("导出文件非空", size > 100, f"{size} 字节")
            check("导出成功提示", "已导出" in page.locator("#pt-batch-msg").inner_text(),
                  page.locator("#pt-batch-msg").inner_text()[:40])

            section("7. 删除临时切片（confirm 自动确认）")
            # 打开新建那条：按摘要文本定位行
            row = page.locator("#pt-body tr", has_text="UI 验收新建的临时知识点")
            check("找到新建的临时行", row.count() == 1, f"{row.count()} 行")
            row.locator("[data-edit]").click()
            page.wait_for_selector("#pt-modal", state="visible", timeout=10000)
            page.wait_for_timeout(500)
            page.click("#pt-delete")
            page.wait_for_timeout(1800)
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(400)
            total2 = _total_of(page.locator("#pt-count").inner_text())
            check("总数回到原值", total2 == total0, f"{total2} vs {total0}")
            check("删除后弹窗关闭", not page.locator("#pt-modal").is_visible())
            added = False
        finally:
            if added:
                print("         （临时切片将在快照恢复阶段一并清除）")
            section("8. 零运行时错误")
            check("无 pageerror / console.error", not js_errors,
                  " | ".join(js_errors[:3]) if js_errors else "clean")
            check("无非预期 4xx/5xx", not http_errors,
                  " | ".join(http_errors[:3]) if http_errors else "clean")
            browser.close()

    print(f"\n{'=' * 60}")
    print(f"结果：{PASSED} PASS / {FAILED} FAIL")
    print(f"截图：{shots}")
    print("=" * 60)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
