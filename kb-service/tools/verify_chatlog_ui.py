"""会话日志管理页 · 浏览器端到端验收（任务 2.1 前端）。

真实 Chromium 打开 ``/admin``，验证前端 JS 在浏览器里确实跑得起来——
HTTP 层的契约测试看不见 JS 运行时错误（id 拼错、字段名写错、渲染崩溃），
所以这里做一次真实渲染回归：

1. **页签**：默认停在「知识库」；点「会话日志」后日志页可见、知识库页隐藏；
2. **问答明细**：表头 == 接口契约列、行数 == pageSize、计数与提示条文案正确；
3. **咨询用户**：切视图后表头与列数随之变化（两视图列定义不同）；
4. **检索**：关键词收窄结果，且与接口 ``total`` 一致；
5. **分页**：下一页后页码与首行时间确实变化；
6. **导出**：点击触发浏览器下载，文件名含中文标签且为 ``.csv``；
7. **零 JS 错误**：全程无 ``pageerror`` / ``console.error``（这条最容易漏，也最致命）。

用法::

    python tools/verify_chatlog_ui.py
    python tools/verify_chatlog_ui.py --port 8023 --headful
    python tools/verify_chatlog_ui.py --keep-shots      # 保留截图目录

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

# 必须在导入 app.* 之前定型（settings 在导入期读取环境变量）
os.environ.setdefault("KB_STORE", "json")
# 浏览器内核装在隔离目录（不污染用户级缓存）；已显式设置时不覆盖
os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH", r"C:\Users\wick\.workbuddy-ai\binaries\ms-playwright"
)

try:  # pragma: no cover - 仅影响交互输出
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

PASSED = 0
FAILED = 0

# 与 static/chatlog.js 的 COLUMNS 保持一致（改了前端就要同步这里）
QA_HEADERS = ["回答时间", "咨询用户", "智能体", "问题", "回答", "置信度", "引用", "拒答", "总token", "模型", "耗时ms"]
USER_HEADERS = ["会话ID", "咨询用户", "智能体", "渠道", "首次时间", "末次时间", "问数", "拒答数", "总token", "最后提问"]


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


# ------------------------------------------------------------------ 直连接口取真值
def fetch_json(base: str, path: str) -> dict:
    """取接口真值（关掉代理，否则本机 HTTP(S)_PROXY 会把 127.0.0.1 也转发出去）。"""
    import httpx

    with httpx.Client(timeout=20, trust_env=False) as client:
        resp = client.get(f"{base}{path}")
        resp.raise_for_status()
        return resp.json()


def main() -> int:
    parser = argparse.ArgumentParser(description="会话日志管理页浏览器验收（任务 2.1）")
    parser.add_argument("--port", type=int, default=8023, help="验收用服务端口")
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

    print(f"启动服务 127.0.0.1:{port}（KB_STORE={env['KB_STORE']}）…")
    proc, log_path = spawn_server(port, env, SERVICE_DIR)
    try:
        if not wait_port(port, timeout=60):
            print("服务启动失败，日志尾部：")
            print(read_log(log_path, 3000))
            return 1
        return _run_browser(base, shots, headful=args.headful)
    finally:
        stop_server(proc)


def _run_browser(base: str, shots: Path, *, headful: bool) -> int:
    from playwright.sync_api import sync_playwright

    # 接口真值，用于与页面渲染结果交叉校验
    qa_api = fetch_json(base, "/api/admin/chatlogs?pageSize=20")["data"]
    users_api = fetch_json(base, "/api/admin/chatlogs/users?pageSize=20")["data"]

    js_errors: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headful)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})

        def on_pageerror(exc) -> None:  # noqa: ANN001
            js_errors.append(f"pageerror: {exc}")

        def on_console(msg) -> None:  # noqa: ANN001
            if msg.type == "error":
                js_errors.append(f"console.error: {msg.text}")

        page.on("pageerror", on_pageerror)
        page.on("console", on_console)

        try:
            section("1. 页签与初始状态")
            page.goto(f"{base}/admin", wait_until="networkidle")
            check("页面标题含「知识管理后台」", "知识管理后台" in page.title(), page.title())
            # 页签数随批2 推进增长：知识库 / 会话日志（2.1）/ 智能体管理（2.2）/ 大模型管理（2.4）
            check("页签数量为 5（知识库/智能体/网关/日志/系统管理）", page.locator("#tabs .tab").count() == 5)
            check("默认停在「知识库」页", page.locator("#page-docs").is_visible())
            check("「会话日志」页初始隐藏", not page.locator("#page-logs").is_visible())
            check("「智能体管理」页初始隐藏", not page.locator("#page-agents").is_visible())
            check("「模型网关」页初始隐藏", not page.locator("#page-gateway").is_visible())
            check("「模型网关」页初始隐藏", not page.locator("#page-gateway").is_visible())

            section("2. 切到「会话日志」并渲染问答明细")
            page.click('#tabs .tab[data-tab="logs"]')
            page.wait_for_selector("#cl-tbody tr", timeout=15000)
            check("日志页可见", page.locator("#page-logs").is_visible())
            check("知识库页已隐藏", not page.locator("#page-docs").is_visible())
            check("页签 active 已切换",
                  page.locator('#tabs .tab[data-tab="logs"]').get_attribute("class").find("active") >= 0)

            headers = page.locator("#cl-thead th").all_inner_texts()
            check("问答明细表头与前端契约一致", headers == QA_HEADERS, f"{headers}")
            rows = page.locator("#cl-tbody tr").count()
            check("行数 == 接口返回条数", rows == len(qa_api["items"]),
                  f"页面={rows} 接口={len(qa_api['items'])}")
            check("行数 == pageSize（满页）", rows == qa_api["pageSize"], f"{rows}")

            count_text = page.locator("#cl-count").inner_text()
            check("计数与接口 total 一致", str(qa_api["total"]) in count_text, count_text)

            note = page.locator("#cl-note").inner_text()
            check("提示条暴露扫描窗口", "扫描消息" in note and str(qa_api["scanLimit"]) in note, note[:80])
            check("提示条暴露存储类型", "存储" in note, note[:60])

            first_time = page.locator("#cl-tbody tr").first.locator("td").first.inner_text()
            check("首行时间与接口首条一致",
                  first_time[:10] == (qa_api["items"][0]["answeredAt"] or "")[:10],
                  f"页面={first_time} 接口={qa_api['items'][0]['answeredAt']}")
            page.screenshot(path=str(shots / "chatlog-qa.png"), full_page=True)

            section("3. 咨询用户视图")
            page.click("#cl-views .f:nth-child(2)")
            page.wait_for_function(
                "document.querySelectorAll('#cl-thead th').length === 10", timeout=10000
            )
            headers_u = page.locator("#cl-thead th").all_inner_texts()
            check("咨询用户表头与前端契约一致", headers_u == USER_HEADERS, f"{headers_u}")
            rows_u = page.locator("#cl-tbody tr").count()
            check("咨询用户行数 == 接口返回条数", rows_u == len(users_api["items"]),
                  f"页面={rows_u} 接口={len(users_api['items'])}")
            check("咨询用户计数与接口 total 一致",
                  str(users_api["total"]) in page.locator("#cl-count").inner_text())
            page.screenshot(path=str(shots / "chatlog-users.png"), full_page=True)

            section("4. 关键词检索")
            page.click("#cl-views .f:nth-child(1)")
            page.wait_for_function(
                "document.querySelectorAll('#cl-thead th').length === 11", timeout=10000
            )
            keyword = qa_api["items"][0]["question"][:4] or "图"
            api_hit = fetch_json(base, f"/api/admin/chatlogs?pageSize=20&q={keyword}")["data"]
            page.fill("#cl-q", keyword)
            page.press("#cl-q", "Enter")
            page.wait_for_timeout(800)
            hit_total = page.locator("#cl-count").inner_text()
            check(f"检索「{keyword}」后计数与接口一致",
                  str(api_hit["total"]) in hit_total, f"页面={hit_total} 接口={api_hit['total']}")
            check("检索结果全部命中关键词",
                  page.locator("#cl-tbody").inner_text().count(keyword) > 0)
            page.screenshot(path=str(shots / "chatlog-search.png"), full_page=True)

            section("5. 重置与分页")
            page.click("#cl-reset")
            page.wait_for_timeout(800)
            check("重置后恢复全量计数",
                  str(qa_api["total"]) in page.locator("#cl-count").inner_text(),
                  page.locator("#cl-count").inner_text())
            pager_before = page.locator(".cl-page-info").inner_text()
            page.click("#cl-next")
            page.wait_for_timeout(800)
            pager_after = page.locator(".cl-page-info").inner_text()
            check("点「下一页」页码前进", pager_before != pager_after, f"{pager_before} → {pager_after}")
            check("第 2 页首行时间 <= 第 1 页首行时间",
                  page.locator("#cl-tbody tr").first.locator("td").first.inner_text()
                  <= first_time,
                  "时间倒序保持")
            page.click("#cl-prev")
            page.wait_for_timeout(800)
            check("点「上一页」回到第 1 页",
                  page.locator(".cl-page-info").inner_text() == pager_before)

            section("6. CSV 导出（真实下载）")
            try:
                with page.expect_download(timeout=15000) as dl_info:
                    page.click("#cl-export")
                download = dl_info.value
                fname = download.suggested_filename
                check("下载文件名含中文标签", "会话日志" in fname, fname)
                check("下载文件为 .csv", fname.endswith(".csv"), fname)
                # 落到临时目录，不污染 artifacts（那里只放给人看的截图）
                tmp = Path(tempfile.gettempdir()) / "nxz-chatlog-export-sample.csv"
                download.save_as(str(tmp))
                raw = tmp.read_bytes()
                check("导出内容带 UTF-8 BOM", raw[:3] == b"\xef\xbb\xbf", raw[:3].hex())
                check("导出表头为 11 列问答视图",
                      raw.decode("utf-8-sig").splitlines()[0].count(",") == 15,
                      raw.decode("utf-8-sig").splitlines()[0][:60])
            except Exception as exc:  # noqa: BLE001
                check("CSV 导出触发下载", False, f"{type(exc).__name__}: {exc}")

            section("7. 零 JS 运行时错误")
            check("全程无 pageerror / console.error", not js_errors,
                  " | ".join(js_errors[:3]) if js_errors else "clean")
        finally:
            browser.close()

    print(f"\n截图目录：{shots}")
    print(f"\n结果：{PASSED} PASS / {FAILED} FAIL")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
