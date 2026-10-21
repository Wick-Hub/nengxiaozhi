"""问答分享与用户反馈验收（任务 4.9 / P-QA-07·08，JSON 存储）。

覆盖 HTTP 层：

1. 页面与静态资源：``/share/{id}``、``/feedback``、分享/反馈脚本可访问；
2. 生成分享：服务端快照问题 + AI 回答，同一消息重复分享复用原链接；
3. 公开数据：渲染后的安全 HTML、AI 标识、浏览计数、引用脱敏（不含 chunkId）；
4. 点赞 / 点踩：点踩必须带合法原因，重复评价覆盖原记录；
5. 管理端：鉴权 401/403/200、列表筛选、汇总、CSV 导出、标记处理 / 重新打开。

**数据隔离**：验收前备份 ``data/sessions.json`` / ``data/shares.json`` /
``data/feedback.json``，结束后恢复。请勿与写入同一数据目录的服务同时运行。

用法::

    python tools/verify_share_feedback.py
    python tools/verify_share_feedback.py --port 8042 --keep

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


SESSION_ID = "S-VERIFY-SHARE"
ANSWER = (
    "校园卡丢失后，请先通过「一卡通服务」挂失，再携带身份证到 **信息中心** 补办。\n\n"
    "1. 线上挂失：当日生效；\n"
    "2. 补办材料：身份证原件；\n"
    "3. 办理地点：行政楼 1 层 105。"
)
CITATIONS = [
    {
        "n": 1,
        "titlePath": "校园卡补办指南",
        "dept": "信息中心",
        "ktype": "faq",
        "similarity": 0.9123,
        "chunkId": "CHUNK-SECRET-1",
    },
    {
        "n": 2,
        "titlePath": "一卡通服务说明",
        "dept": "财务处",
        "ktype": "doc",
        "similarity": 0.8341,
        "chunkId": "CHUNK-SECRET-2",
    },
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
                        {
                            "msgId": 1,
                            "role": "user",
                            "content": "校园卡丢了怎么补办？",
                            "createdAt": stamp,
                        },
                        {
                            "msgId": 2,
                            "role": "ai",
                            "content": ANSWER,
                            "confidence": 0.88,
                            "refused": False,
                            "citations": CITATIONS,
                            "traceId": "T-VERIFY-SHARE",
                            "createdAt": stamp,
                        },
                    ],
                }
            }
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="问答分享与用户反馈验收（任务 4.9）")
    parser.add_argument("--port", type=int, default=8042, help="验收用服务端口")
    parser.add_argument("--keep", action="store_true", help="保留验收数据（默认恢复备份）")
    args = parser.parse_args()

    import httpx

    from tools._server import read_log, spawn_server, stop_server, wait_port

    data_dir = SERVICE_DIR / "data"
    sessions_path = data_dir / "sessions.json"
    shares_path = data_dir / "shares.json"
    feedback_path = data_dir / "feedback.json"

    tmp_dir = Path(tempfile.mkdtemp(prefix="nxz-share-verify-"))
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
    env = dict(os.environ)
    env["KB_STORE"] = "json"
    env["AUTH_ENABLED"] = "true"
    env["RATE_LIMIT_ENABLED"] = "false"
    env["LLM_MOCK"] = "1"

    print(f"启动服务 127.0.0.1:{port}（KB_STORE=json, AUTH_ENABLED=true）…")
    proc, log_path = spawn_server(port, env, SERVICE_DIR)
    try:
        if not wait_port(port, timeout=60):
            print("服务启动失败，日志尾部：")
            print(read_log(log_path, 3000))
            return 1

        from app.security.auth import create_token

        admin = create_token("admin-001", "管理员", user_id=1, expires_minutes=10)
        guest = create_token("visitor-001", "访客", user_id=None, expires_minutes=10)
        admin_headers = {"Authorization": f"Bearer {admin}"}
        guest_headers = {"Authorization": f"Bearer {guest}"}

        with httpx.Client(timeout=20, trust_env=False, base_url=base) as cli:
            section("1. 页面与静态资源")
            page = cli.get("/")
            check("对话页 200", page.status_code == 200)
            share_page = cli.get("/share/PLACEHOLDER")
            check("分享页路由 200", share_page.status_code == 200, str(share_page.status_code))
            check("分享页加载脚本", "share.js" in share_page.text and "share-card" in share_page.text)
            check("分享脚本可访问", cli.get("/share.js").status_code == 200)
            check("分享样式可访问", cli.get("/share.css").status_code == 200)
            fb_page = cli.get("/feedback")
            check("反馈后台页 200", fb_page.status_code == 200, str(fb_page.status_code))
            check("反馈页加载脚本", "feedback.js" in fb_page.text and "问答反馈记录" in fb_page.text)
            check("反馈脚本可访问", cli.get("/feedback.js").status_code == 200)
            missing = cli.get("/api/share/DOES-NOT-EXIST")
            check("不存在的分享 404", missing.status_code == 404, str(missing.status_code))

            section("2. 生成分享（服务端快照）")
            created = cli.post(
                "/api/share",
                json={
                    "sessionId": SESSION_ID,
                    "msgId": 2,
                    "agentName": "学业助手",
                    "channelCode": "lib",
                },
            )
            body = created.json()
            data = body.get("data") or {}
            share_id = data.get("shareId") or ""
            check("生成分享 200", created.status_code == 200 and body.get("ok") is True,
                  str(created.status_code))
            check("返回不可枚举的分享 ID", len(share_id) >= 8, share_id)
            check("分享链接指向 /share/", str(data.get("url", "")).endswith(f"/share/{share_id}"),
                  data.get("url", ""))
            check("首次生成非复用", data.get("reused") is False)
            check("携带原问题", data.get("question") == "校园卡丢了怎么补办？")

            again = cli.post(
                "/api/share", json={"sessionId": SESSION_ID, "msgId": 2}
            ).json().get("data") or {}
            check("同一条消息复用分享", again.get("shareId") == share_id and again.get("reused") is True)

            bad_msg = cli.post("/api/share", json={"sessionId": SESSION_ID, "msgId": 1})
            check("非 AI 消息不可分享", bad_msg.status_code == 404, str(bad_msg.status_code))
            missing_sess = cli.post("/api/share", json={"sessionId": "S-NOPE", "msgId": 2})
            check("会话不存在返回 404", missing_sess.status_code == 404,
                  str(missing_sess.status_code))

            section("3. 公开分享数据")
            public = cli.get(f"/api/share/{share_id}")
            pub = public.json().get("data") or {}
            check("分享数据 200", public.status_code == 200)
            check("隐式 AI 标识响应头", public.headers.get("X-AI-Generated") == "true",
                  public.headers.get("X-AI-Label", ""))
            check("显式 AI 标识元数据", (pub.get("aiLabel") or {}).get("aigc") is True)
            check("AI 标识可追溯到消息", (pub.get("aiLabel") or {}).get("produceId")
                  == f"{SESSION_ID}:2")
            check("回答 HTML 服务端渲染", "<strong>信息中心</strong>" in pub.get("answerHtml", ""))
            check("问题快照正确", pub.get("question") == "校园卡丢了怎么补办？")
            check("引用已脱敏（无 chunkId）",
                  all("chunkId" not in c for c in pub.get("citations") or []))
            check("引用字段齐全", len(pub.get("citations") or []) == 2
                  and pub["citations"][0]["title"] == "校园卡补办指南")
            check("浏览计数递增", int(pub.get("views") or 0) == 1, str(pub.get("views")))
            check("初始赞踩为 0", (pub.get("feedback") or {}) == {"likes": 0, "dislikes": 0})
            check("分享页可打开具体卡片", cli.get(f"/share/{share_id}").status_code == 200)

            section("4. 点赞 / 点踩")
            reasons = cli.get("/api/feedback/reasons").json().get("data") or []
            check("点踩原因字典 4 项", len(reasons) == 4,
                  ",".join(r.get("code", "") for r in reasons))

            like = cli.post(
                "/api/feedback",
                json={"rating": 1, "shareId": share_id, "sessionId": SESSION_ID, "msgId": 2},
            )
            check("点赞 200", like.status_code == 200, str(like.status_code))
            check("点赞返回文案", (like.json().get("data") or {}).get("ratingLabel") == "点赞")
            counts = (cli.get(f"/api/share/{share_id}").json().get("data") or {}).get("feedback") or {}
            check("点赞计数生效", counts.get("likes") == 1 and counts.get("dislikes") == 0,
                  str(counts))

            no_reason = cli.post(
                "/api/feedback",
                json={"rating": -1, "shareId": share_id, "sessionId": SESSION_ID, "msgId": 2},
            )
            check("点踩未选原因 400", no_reason.status_code == 400, str(no_reason.status_code))
            bad_reason = cli.post(
                "/api/feedback",
                json={
                    "rating": -1,
                    "shareId": share_id,
                    "sessionId": SESSION_ID,
                    "msgId": 2,
                    "reasonCode": "not-in-catalog",
                },
            )
            check("非法原因被拒绝", bad_reason.status_code == 400, str(bad_reason.status_code))

            dislike = cli.post(
                "/api/feedback",
                json={
                    "rating": -1,
                    "shareId": share_id,
                    "sessionId": SESSION_ID,
                    "msgId": 2,
                    "reasonCode": "inaccurate",
                    "reason": "补办地点和现场不一致",
                },
            )
            check("点踩 200", dislike.status_code == 200, str(dislike.status_code))
            counts = (cli.get(f"/api/share/{share_id}").json().get("data") or {}).get("feedback") or {}
            check("重复评价覆盖原记录", counts.get("likes") == 0 and counts.get("dislikes") == 1,
                  str(counts))

            by_msg = cli.post(
                "/api/feedback",
                json={
                    "rating": -1,
                    "sessionId": SESSION_ID,
                    "msgId": 2,
                    "reasonCode": "incomplete",
                    "reason": "缺少办理时限",
                },
            )
            check("不传 shareId 也能评价", by_msg.status_code == 200, str(by_msg.status_code))
            unknown = cli.post(
                "/api/feedback",
                json={"rating": 1, "shareId": "NOPE", "sessionId": SESSION_ID, "msgId": 2},
            )
            check("未知分享返回 404", unknown.status_code == 404, str(unknown.status_code))

            section("5. 管理端反馈记录")
            check("匿名访问 401",
                  cli.get("/api/admin/feedback").status_code == 401)
            check("访客访问 403",
                  cli.get("/api/admin/feedback", headers=guest_headers).status_code == 403)
            listing = cli.get("/api/admin/feedback", headers=admin_headers)
            payload = listing.json().get("data") or {}
            check("管理员访问 200", listing.status_code == 200, str(listing.status_code))
            check("同一条消息只保留一条记录", payload.get("total") == 1,
                  str(payload.get("total")))
            item = (payload.get("items") or [{}])[0]
            feedback_id = item.get("feedbackId")
            check("记录包含问题快照", item.get("question") == "校园卡丢了怎么补办？")
            check("记录包含回答摘要", "信息中心" in (item.get("answerExcerpt") or ""))
            check("记录包含点踩原因", item.get("reasonLabel") == "回答不完整"
                  and item.get("reasonCode") == "incomplete")
            check("汇总口径正确", (payload.get("summary") or {}).get("dislikes") == 1
                  and (payload.get("summary") or {}).get("pending") == 1)

            filtered = cli.get(
                "/api/admin/feedback", params={"rating": 1}, headers=admin_headers
            ).json().get("data") or {}
            check("按评价筛选", filtered.get("total") == 0, str(filtered.get("total")))
            keyword = cli.get(
                "/api/admin/feedback", params={"q": "缺少办理时限"}, headers=admin_headers
            ).json().get("data") or {}
            check("关键词覆盖补充说明", keyword.get("total") == 1, str(keyword.get("total")))

            exported = cli.get("/api/admin/feedback/export", headers=admin_headers)
            check("CSV 导出 200", exported.status_code == 200, str(exported.status_code))
            check("CSV 带 UTF-8 BOM", exported.content.startswith(b"\xef\xbb\xbf"))
            check("CSV 表头完整", "反馈ID" in exported.text and "处理状态" in exported.text)
            check("CSV 中文文件名", "filename*=UTF-8''" in exported.headers.get(
                "content-disposition", ""))
            check("匿名导出 401",
                  cli.get("/api/admin/feedback/export").status_code == 401)

            handled = cli.patch(
                f"/api/admin/feedback/{feedback_id}",
                json={"handled": True, "note": "已补充补办地点说明"},
                headers=admin_headers,
            )
            check("标记已处理 200", handled.status_code == 200, str(handled.status_code))
            handled_item = handled.json().get("data") or {}
            check("处理状态与备注落库",
                  handled_item.get("handled") == 1
                  and handled_item.get("handleNote") == "已补充补办地点说明")
            pending = cli.get(
                "/api/admin/feedback", params={"handled": 0}, headers=admin_headers
            ).json().get("data") or {}
            check("待处理筛选生效", pending.get("total") == 0, str(pending.get("total")))
            done = cli.get(
                "/api/admin/feedback", params={"handled": 1}, headers=admin_headers
            ).json().get("data") or {}
            check("已处理筛选生效", done.get("total") == 1, str(done.get("total")))

            reopened = cli.patch(
                f"/api/admin/feedback/{feedback_id}",
                json={"handled": False, "note": ""},
                headers=admin_headers,
            ).json().get("data") or {}
            check("重新打开", reopened.get("handled") == 0 and reopened.get("handledAt") is None)
            absent = cli.patch(
                "/api/admin/feedback/999999",
                json={"handled": True},
                headers=admin_headers,
            )
            check("不存在的反馈 404", absent.status_code == 404, str(absent.status_code))

            section("6. 权限与筛选字典")
            check("反馈筛选字典", cli.get(
                "/api/admin/feedback/filters", headers=admin_headers
            ).status_code == 200)
            check("页面权限映射", __import__(
                "app.security.permissions", fromlist=["required_for"]
            ).required_for("GET", "/api/admin/feedback") == "feedback.view")
    finally:
        stop_server(proc)
        if not args.keep:
            _restore(sessions_path, backups["sessions"])
            _restore(shares_path, backups["shares"])
            _restore(feedback_path, backups["feedback"])
            shutil.rmtree(tmp_dir, ignore_errors=True)
        else:
            print(f"\n验收数据保留在：{data_dir}（备份：{tmp_dir}）")

    print(f"\n{'=' * 60}")
    print(f"通过 {PASSED} 项，失败 {FAILED} 项")
    print(f"{'=' * 60}")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
