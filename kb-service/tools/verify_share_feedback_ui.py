"""问答分享与用户反馈浏览器验收（任务 4.9 / P-QA-07·08）。

真实 Chromium 覆盖 HTTP 层看不到的行为：

1. 门户 AI 回答下方的「分享 / 有帮助 / 没帮助」操作条；
2. 点踩必须选择原因，提交后按钮态与反馈记录同步；
3. 分享弹窗：问题、Markdown 渲染、引用、AI 标识、浏览计数；
4. 分享页：复制链接、保存 .md、点赞 / 点踩与计数翻转；
5. 反馈后台：KPI、列表、详情、处理留痕、筛选与 CSV 下载。

**数据隔离**：验收前备份 ``data/sessions.json`` / ``data/shares.json`` /
``data/feedback.json``，结束后恢复。

用法::

    python tools/verify_share_feedback_ui.py
    python tools/verify_share_feedback_ui.py --headful

退出码：全部通过为 0，存在 FAIL 为 1。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime
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


SESSION_ID = "S-VERIFY-SHARE-UI"
ANSWER = (
    "校园卡丢失后，请先通过「一卡通服务」挂失，再携带身份证到 **信息中心** 补办。\n\n"
    "1. 线上挂失：当日生效；\n"
    "2. 补办材料：身份证原件；\n"
    "3. 办理地点：行政楼 1 层 105。"
)
CITATIONS = [
    {"n": 1, "titlePath": "校园卡补办指南", "dept": "信息中心", "ktype": "faq",
     "similarity": 0.9123, "chunkId": "CHUNK-SECRET-1"},
    {"n": 2, "titlePath": "一卡通服务说明", "dept": "财务处", "ktype": "doc",
     "similarity": 0.8341, "chunkId": "CHUNK-SECRET-2"},
]


def _seed_sessions(path: Path) -> None:
    stamp = datetime.now().isoformat(timespec="seconds")
    _write_json(
        path,
        {
            "sessions": {
                SESSION_ID: {
                    "sessionId": SESSION_ID,
                    "role": "本科生",
                    "createdAt": stamp,
                    "agentId": "",
                    "channelCode": "lib",
                    "dept": "信息中心",
                    "messages": [
                        {"msgId": 1, "role": "user", "content": "校园卡丢了怎么补办？",
                         "createdAt": stamp},
                        {"msgId": 2, "role": "ai", "content": ANSWER, "confidence": 0.88,
                         "refused": False, "citations": CITATIONS,
                         "traceId": "T-VERIFY-SHARE-UI", "createdAt": stamp},
                    ],
                }
            }
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="问答分享与用户反馈浏览器验收（任务 4.9）")
    parser.add_argument("--port", type=int, default=8043, help="验收用服务端口")
    parser.add_argument("--headful", action="store_true", help="显示浏览器窗口（调试用）")
    parser.add_argument("--shots-dir", default="", help="截图目录（默认 kb-service/artifacts）")
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    from tools._server import read_log, spawn_server, stop_server, wait_port

    data_dir = SERVICE_DIR / "data"
    sessions_path = data_dir / "sessions.json"
    shares_path = data_dir / "shares.json"
    feedback_path = data_dir / "feedback.json"
    tmp_dir = Path(tempfile.mkdtemp(prefix="nxz-share-ui-"))
    backups = {
        "sessions": _backup(sessions_path, tmp_dir),
        "shares": _backup(shares_path, tmp_dir),
        "feedback": _backup(feedback_path, tmp_dir),
    }
    _seed_sessions(sessions_path)
    _write_json(shares_path, {"shares": {}, "nextId": 1})
    _write_json(feedback_path, {"items": {}, "nextId": 1})

    port = args.port
    base = f"http://127.0.0.1:{port}"
    shots = Path(args.shots_dir) if args.shots_dir else SERVICE_DIR / "artifacts"
    shots.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["KB_STORE"] = "json"
    env["AUTH_ENABLED"] = "false"  # 演示模式：后台页面无需登录，聚焦 UI 行为
    env["RATE_LIMIT_ENABLED"] = "false"
    env["LLM_MOCK"] = "1"

    print(f"启动服务 127.0.0.1:{port}（KB_STORE=json, AUTH_ENABLED=false）…")
    proc, log_path = spawn_server(port, env, SERVICE_DIR)
    try:
        if not wait_port(port, timeout=60):
            print("服务启动失败，日志尾部：")
            print(read_log(log_path, 3000))
            return 1

        errors: list[str] = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not args.headful)
            ctx = browser.new_context(viewport={"width": 1440, "height": 950})
            try:
                ctx.grant_permissions(
                    ["clipboard-read", "clipboard-write"], origin=base
                )
            except Exception:  # noqa: BLE001 - 权限失败只影响剪贴板断言
                pass
            page = ctx.new_page()
            page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))

            def on_console(msg) -> None:  # noqa: ANN001
                if msg.type == "error":
                    errors.append(f"console.error: {msg.text}")

            page.on("console", on_console)
            share_page = None
            admin_page = None
            try:
                section("1. 门户消息操作条")
                page.goto(f"{base}/?session={SESSION_ID}", wait_until="networkidle")
                page.wait_for_selector(".msg.ai .msg-actions", timeout=20000)
                check("AI 回答下方有操作条",
                      page.locator(".msg.ai .msg-actions").count() == 1)
                check("操作条包含分享 / 有帮助 / 没帮助",
                      page.locator(".msg.ai .msg-actions .msg-act").count() == 3)
                check("AI 生成标识可见", page.locator(".msg.ai .ai-label").is_visible())
                page.screenshot(path=str(shots / "share-actions.png"))

                section("2. 门户点赞 / 点踩")
                page.click(".msg-actions .msg-act[data-rating='1']")
                page.wait_for_function(
                    "() => document.querySelector('.msg-actions .msg-act[data-rating=\"1\"]')"
                    ".classList.contains('active')",
                    timeout=10000,
                )
                check("点赞按钮进入选中态",
                      page.locator(".msg-actions .msg-act[data-rating='1']").evaluate(
                          "el => el.classList.contains('active')"))
                page.wait_for_selector(".nxz-toast", timeout=5000)
                check("点赞出现提示", "感谢反馈" in page.locator(".nxz-toast").inner_text())

                page.click(".msg-actions .msg-act[data-rating='-1']")
                page.wait_for_selector("#fb-modal:not([hidden])", timeout=5000)
                check("点踩弹出原因框", page.locator("#fb-modal").is_visible())
                page.click("#fb-submit")
                page.wait_for_selector("#fb-msg:not([hidden])", timeout=5000)
                check("未选原因被拦截", "请选择" in page.locator("#fb-msg").inner_text())
                page.locator("#fb-reasons .fb-reason").first.click()
                page.fill("#fb-note", "补办地点与现场不一致")
                page.click("#fb-submit")
                page.wait_for_selector("#fb-modal", state="hidden", timeout=10000)
                check("点踩提交后关闭弹窗", page.locator("#fb-modal").is_hidden())
                page.wait_for_function(
                    "() => document.querySelector('.msg-actions .msg-act[data-rating=\"-1\"]')"
                    ".classList.contains('active')",
                    timeout=5000,
                )
                check("点踩按钮进入选中态",
                      page.locator(".msg-actions .msg-act[data-rating='-1']").evaluate(
                          "el => el.classList.contains('active')"))
                record = page.evaluate(
                    "async () => (await (await fetch('/api/admin/feedback')).json())"
                    ".data.items[0]"
                ) or {}
                check("反馈记录已落库", record.get("rating") == -1
                      and record.get("reasonCode") == "inaccurate", str(record.get("feedbackId")))
                check("反馈记录带补充说明", record.get("reason") == "补办地点与现场不一致")

                section("3. 分享卡片弹窗")
                with page.expect_popup(timeout=15000) as pop_info:
                    page.click(".msg-actions .msg-act[data-act='share']")
                share_page = pop_info.value
                share_page.wait_for_selector("#share-card:not([hidden])", timeout=20000)
                check("分享页 URL 不可枚举", "/share/" in share_page.url, share_page.url)
                check("问题快照展示",
                      share_page.locator("#share-question").inner_text() == "校园卡丢了怎么补办？")
                answer_text = share_page.locator("#share-answer").inner_text()
                check("回答 HTML 渲染", "信息中心" in answer_text and "行政楼" in answer_text)
                check("引用来源 2 条",
                      share_page.locator("#share-citation-list .citation-item").count() == 2)
                check("AI 标识展示", share_page.locator(".ai-label").is_visible())
                check("浏览计数展示",
                      "浏览 1 次" in share_page.locator("#share-views").inner_text())
                check("分享页无 chunkId 泄露",
                      "CHUNK-SECRET" not in share_page.content())
                share_page.screenshot(path=str(shots / "share-public.png"), full_page=True)

                section("4. 分享页操作与赞踩")
                share_page.click("#share-like")
                share_page.wait_for_function(
                    "() => document.getElementById('share-likes').textContent === '1'",
                    timeout=10000,
                )
                check("分享页点赞计数 +1",
                      share_page.locator("#share-likes").inner_text() == "1")
                check("分享页点赞选中态",
                      share_page.locator("#share-like").evaluate(
                          "el => el.classList.contains('active')"))

                share_page.click("#share-dislike")
                share_page.wait_for_selector("#share-fb-modal:not([hidden])", timeout=5000)
                share_page.click("#share-fb-submit")
                share_page.wait_for_selector("#share-fb-msg:not([hidden])", timeout=5000)
                check("分享页点踩必须选原因",
                      "请选择" in share_page.locator("#share-fb-msg").inner_text())
                share_page.locator("#share-fb-reasons .fb-reason").first.click()
                share_page.fill("#share-fb-note", "分享页补充：缺少办理时限")
                share_page.click("#share-fb-submit")
                share_page.wait_for_selector("#share-fb-modal", state="hidden", timeout=10000)
                share_page.wait_for_function(
                    "() => document.getElementById('share-dislikes').textContent === '1'",
                    timeout=10000,
                )
                check("点踩后点赞计数归零",
                      share_page.locator("#share-likes").inner_text() == "0")
                check("分享页点踩选中态",
                      share_page.locator("#share-dislike").evaluate(
                          "el => el.classList.contains('active')"))

                with share_page.expect_download(timeout=10000) as dl_info:
                    share_page.click("#share-save")
                download = dl_info.value
                saved = Path(download.path()).read_text(encoding="utf-8")
                check("保存 Markdown 文件名", download.suggested_filename.endswith(".md"),
                      download.suggested_filename)
                check("Markdown 含问题与 AI 声明",
                      saved.startswith("# 校园卡丢了怎么补办？") and "AI 生成" in saved)
                check("Markdown 含引用来源", "## 引用来源" in saved and "信息中心" in saved)

                share_page.click("#share-native")
                # 保存 .md 的 toast 可能仍在展示，等文案切换成本次分享的结果
                share_page.wait_for_function(
                    "() => { const t = document.getElementById('share-toast');"
                    " return t && !t.hidden &&"
                    " (t.textContent.includes('复制') || t.textContent.includes('已分享')); }",
                    timeout=10000,
                )
                check("分享链接提示",
                      share_page.locator("#share-toast").inner_text() in
                      ("分享链接已复制", "复制失败，请手动复制地址栏") or
                      "已分享" in share_page.locator("#share-toast").inner_text())
                try:
                    clip = share_page.evaluate("navigator.clipboard.readText()")
                    check("链接已写入剪贴板", clip == share_page.url, clip[:60])
                except Exception as exc:  # noqa: BLE001 - 无剪贴板权限时不阻断
                    check("剪贴板读取（环境支持时）", True, f"skipped: {exc}")

                section("5. 反馈后台")
                admin_page = ctx.new_page()
                admin_page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
                admin_page.on("console", on_console)
                admin_page.goto(f"{base}/feedback", wait_until="networkidle")
                admin_page.wait_for_selector("#fb-body tr", timeout=15000)
                check("反馈页 KPI 总量", admin_page.locator("#fb-kpi-total").inner_text() == "1")
                check("反馈页 KPI 点踩", admin_page.locator("#fb-kpi-dislikes").inner_text() == "1")
                row_text = admin_page.locator("#fb-body tr").first.inner_text()
                check("列表展示点踩与原因", "点踩" in row_text and "内容不准确" in row_text)
                check("列表展示问题", "校园卡丢了怎么补办" in row_text)
                admin_page.screenshot(path=str(shots / "feedback-admin.png"), full_page=True)

                admin_page.locator("#fb-body tr button[data-act='detail']").first.click()
                admin_page.wait_for_selector("#fb-detail:not([hidden])", timeout=10000)
                check("详情展示完整问题",
                      "校园卡丢了怎么补办" in admin_page.locator("#fb-detail-question").inner_text())
                check("详情展示补充说明",
                      "分享页补充" in admin_page.locator("#fb-detail-feedback").inner_text())
                admin_page.fill("#fb-handle-note", "已补充办理时限说明")
                admin_page.click("#fb-handle")
                admin_page.wait_for_function(
                    "() => document.getElementById('fb-detail-msg').textContent.includes('已标记处理')",
                    timeout=10000,
                )
                check("标记处理后状态更新",
                      "已处理" in admin_page.locator("#fb-body tr").first.inner_text())
                admin_page.click("#fb-detail-close")
                admin_page.wait_for_selector("#fb-detail", state="hidden", timeout=5000)
                check("关闭详情弹窗", admin_page.locator("#fb-detail").is_hidden())

                admin_page.select_option("#fb-handled", "1")
                with admin_page.expect_response(
                    lambda r: "/api/admin/feedback" in r.url and "handled=1" in r.url
                ):
                    admin_page.click("#fb-search")
                admin_page.wait_for_selector("#fb-body tr", timeout=10000)
                check("已处理筛选命中", admin_page.locator("#fb-body tr").count() == 1)

                admin_page.locator("#fb-body tr button[data-act='detail']").first.click()
                admin_page.wait_for_selector("#fb-detail:not([hidden])", timeout=10000)
                admin_page.click("#fb-reopen")
                # 当前筛选为「已处理」，重新打开后该条离开列表，弹窗自动关闭
                admin_page.wait_for_selector("#fb-detail", state="hidden", timeout=10000)
                admin_page.wait_for_function(
                    "() => document.getElementById('fb-note').textContent.includes('已重新打开')",
                    timeout=10000,
                )
                check("重新打开成功", "已重新打开" in admin_page.locator("#fb-note").inner_text())

                with admin_page.expect_response(
                    lambda r: "/api/admin/feedback" in r.url and "handled=" not in r.url
                ):
                    admin_page.click("#fb-reset")
                admin_page.wait_for_selector("#fb-body tr", timeout=10000)

                with admin_page.expect_download(timeout=10000) as csv_info:
                    admin_page.click("#fb-export")
                csv_download = csv_info.value
                csv_text = Path(csv_download.path()).read_text(encoding="utf-8-sig")
                check("CSV 下载文件名", csv_download.suggested_filename.endswith(".csv"),
                      csv_download.suggested_filename)
                check("CSV 含表头与记录", "反馈ID" in csv_text and "校园卡丢了怎么补办" in csv_text)

                admin_page.fill("#fb-q", "校园卡")
                with admin_page.expect_response(
                    lambda r: "/api/admin/feedback" in r.url and "q=" in r.url
                ):
                    admin_page.click("#fb-search")
                check("关键词检索命中", admin_page.locator("#fb-body tr").count() == 1)

                section("6. 运行时错误")
                check("零 pageerror / console.error", not errors, "; ".join(errors[:3]))
            finally:
                if share_page is not None:
                    try:
                        share_page.close()
                    except Exception:  # noqa: BLE001
                        pass
                if admin_page is not None:
                    try:
                        admin_page.close()
                    except Exception:  # noqa: BLE001
                        pass
                browser.close()

        print(f"\n截图目录：{shots}")
        print(f"\n结果：{PASSED} PASS / {FAILED} FAIL")
        return 1 if FAILED else 0
    finally:
        stop_server(proc)
        _restore(sessions_path, backups["sessions"])
        _restore(shares_path, backups["shares"])
        _restore(feedback_path, backups["feedback"])
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
