"""问答分享与用户反馈测试（任务 4.9 / P-QA-07·08）。

覆盖：

1. **分享快照**：服务端取「用户问题 + AI 回答 + 引用」，重复分享复用短链，
   原会话变化不影响已生成卡片；公开页只暴露脱敏引用（无 ``chunkId``）。
2. **反馈**：点赞即记、点踩必须选原因，原因字典校验；同一消息改评覆盖原记录。
3. **管理端**：清单筛选（评价/处理状态/关键词/日期）、汇总、CSV 导出、处理留痕。
4. **鉴权**：开启后匿名 401、普通用户 403、管理员放行；权限点映射正确。
5. **安全**：回答 HTML 由服务端渲染，脚本标签被转义。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db import chatlog, share_store
from app.main import app
from app.security import auth, permissions
from app.security.deps import Principal

client = TestClient(app)

QUESTION = "校园卡丢了怎么补办？"
ANSWER = "请携带身份证前往信息中心一楼服务大厅办理，工本费 20 元，当场可取。"


def _seed(session_id: str = "S-SHARE-1", role: str = "本科生", answer: str = ANSWER) -> tuple[str, int]:
    chatlog.append_message(session_id, "user", QUESTION, user_role=role)
    ai = chatlog.append_message(
        session_id,
        "ai",
        answer,
        confidence=0.91,
        citations=[
            {
                "chunkId": "C-SECRET-001",
                "docId": 7,
                "titlePath": "校园卡 > 补办流程",
                "docTitle": "校园卡管理办法",
                "dept": "信息中心",
                "ktype": "办事指南",
                "similarity": 0.8765,
                "n": 1,
            }
        ],
    )
    return session_id, int(ai["msgId"])


def _share(session_id: str = "S-SHARE-1", msg_id: int | None = None) -> dict:
    if msg_id is None:
        session_id, msg_id = _seed(session_id)
    resp = client.post(
        "/api/share",
        json={"sessionId": session_id, "msgId": msg_id, "channelCode": "portal-web"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


# ------------------------------------------------------------------ 分享
def test_share_snapshot_and_public_view() -> None:
    session_id, msg_id = _seed()
    data = _share(session_id, msg_id)
    assert data["shareId"].startswith("Q")
    assert data["url"].endswith("/share/" + data["shareId"])
    assert data["reused"] is False

    resp = client.get(f"/api/share/{data['shareId']}")
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["question"] == QUESTION
    assert body["answer"] == ANSWER
    assert "信息中心" in body["answerHtml"]
    assert body["citations"][0]["title"] == "校园卡 > 补办流程"
    assert body["citations"][0]["dept"] == "信息中心"
    assert "chunkId" not in body["citations"][0]
    assert body["aiGenerated"] is True
    assert body["aiLabel"]["produceId"].endswith(f":{msg_id}")
    assert body["views"] == 1
    assert resp.headers["X-AI-Generated"] == "true"

    again = client.get(f"/api/share/{data['shareId']}").json()["data"]
    assert again["views"] == 2


def test_share_is_frozen_after_source_changes() -> None:
    session_id, msg_id = _seed("S-FROZEN")
    data = _share(session_id, msg_id)
    chatlog.append_message(session_id, "user", "后续追问", user_role="本科生")
    chatlog.append_message(session_id, "ai", "后续回答", confidence=0.5)
    body = client.get(f"/api/share/{data['shareId']}").json()["data"]
    assert body["question"] == QUESTION
    assert body["answer"] == ANSWER


def test_share_reuses_link_per_message() -> None:
    session_id, msg_id = _seed("S-REUSE")
    first = _share(session_id, msg_id)
    second = _share(session_id, msg_id)
    assert second["shareId"] == first["shareId"]
    assert second["reused"] is True


def test_share_rejects_missing_or_user_message() -> None:
    resp = client.post("/api/share", json={"sessionId": "S-NOPE", "msgId": 1})
    assert resp.status_code == 404

    session_id, _ = _seed("S-USER-MSG")
    session = chatlog.load_session(session_id)
    user_msg_id = next(m["msgId"] for m in session["messages"] if m["role"] == "user")
    resp = client.post(
        "/api/share", json={"sessionId": session_id, "msgId": user_msg_id}
    )
    assert resp.status_code == 404

    resp = client.post("/api/share", json={"sessionId": session_id, "msgId": 0})
    assert resp.status_code == 422

    resp = client.get("/api/share/Q-NOT-EXIST")
    assert resp.status_code == 404


def test_public_answer_html_escapes_scripts() -> None:
    session_id, msg_id = _seed(
        "S-XSS", answer="**请到信息中心** <script>alert('x')</script>"
    )
    data = _share(session_id, msg_id)
    body = client.get(f"/api/share/{data['shareId']}").json()["data"]
    assert "<script>" not in body["answerHtml"]
    assert "&lt;script&gt;" in body["answerHtml"]


# ------------------------------------------------------------------ 反馈
def test_like_feedback_and_reasons() -> None:
    reasons = client.get("/api/feedback/reasons").json()["data"]
    assert {r["code"] for r in reasons} == {"inaccurate", "incomplete", "irrelevant", "other"}

    session_id, msg_id = _seed("S-LIKE")
    resp = client.post(
        "/api/feedback",
        json={"sessionId": session_id, "msgId": msg_id, "rating": 1, "reasonCode": "inaccurate"},
    )
    assert resp.status_code == 200, resp.text
    item = resp.json()["data"]
    assert item["rating"] == 1
    assert item["ratingLabel"] == "点赞"

    stored = share_store.get_feedback(item["feedbackId"])
    assert stored["reasonCode"] == ""
    assert stored["question"] == QUESTION
    assert stored["answerExcerpt"].startswith("请携带身份证")


def test_dislike_requires_valid_reason() -> None:
    session_id, msg_id = _seed("S-DISLIKE")
    missing = client.post(
        "/api/feedback", json={"sessionId": session_id, "msgId": msg_id, "rating": -1}
    )
    assert missing.status_code == 400
    assert "原因" in missing.json()["detail"]

    invalid = client.post(
        "/api/feedback",
        json={"sessionId": session_id, "msgId": msg_id, "rating": -1, "reasonCode": "bad"},
    )
    assert invalid.status_code == 400

    ok = client.post(
        "/api/feedback",
        json={
            "sessionId": session_id,
            "msgId": msg_id,
            "rating": -1,
            "reasonCode": "outdated",
            "reason": "工本费已经涨了",
        },
    )
    # 目录外的原因同样拒绝（避免脏数据进入统计）
    assert ok.status_code == 400

    ok = client.post(
        "/api/feedback",
        json={
            "sessionId": session_id,
            "msgId": msg_id,
            "rating": -1,
            "reasonCode": "inaccurate",
            "reason": "工本费已经涨了",
        },
    )
    assert ok.status_code == 200, ok.text
    stored = share_store.get_feedback(ok.json()["data"]["feedbackId"])
    assert stored["reasonCode"] == "inaccurate"
    assert stored["reasonLabel"] == "内容不准确"
    assert stored["reason"] == "工本费已经涨了"
    assert stored["handled"] == 0


def test_feedback_via_share_and_rerating_upserts() -> None:
    session_id, msg_id = _seed("S-UPSERT")
    data = _share(session_id, msg_id)
    first = client.post(
        "/api/feedback",
        json={"sessionId": session_id, "msgId": msg_id, "rating": 1},
    ).json()["data"]

    second = client.post(
        "/api/feedback",
        json={
            "shareId": data["shareId"],
            "rating": -1,
            "reasonCode": "incomplete",
            "reason": "没说清楚要带什么材料",
        },
    )
    assert second.status_code == 200, second.text
    assert second.json()["data"]["feedbackId"] == first["feedbackId"]

    listing = client.get("/api/admin/feedback").json()["data"]
    assert listing["total"] == 1
    assert listing["items"][0]["rating"] == -1
    assert listing["items"][0]["shareId"] == data["shareId"]

    counts = client.get(f"/api/share/{data['shareId']}").json()["data"]["feedback"]
    assert counts == {"likes": 0, "dislikes": 1}


def test_feedback_unknown_share_or_session() -> None:
    assert (
        client.post("/api/feedback", json={"shareId": "Q-NO", "rating": 1}).status_code
        == 404
    )
    assert (
        client.post(
            "/api/feedback", json={"sessionId": "S-NO", "msgId": 1, "rating": 1}
        ).status_code
        == 404
    )
    assert client.post("/api/feedback", json={"rating": 1}).status_code == 400


# ------------------------------------------------------------------ 管理端
def _make_feedback(session_prefix: str, rating: int, reason_code: str = "") -> dict:
    session_id, msg_id = _seed(session_prefix)
    payload = {"sessionId": session_id, "msgId": msg_id, "rating": rating}
    if rating < 0:
        payload.update({"reasonCode": reason_code or "inaccurate", "reason": "答错了"})
    resp = client.post("/api/feedback", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def test_admin_list_filters_and_summary() -> None:
    _make_feedback("S-A1", 1)
    _make_feedback("S-A2", 1)
    bad = _make_feedback("S-A3", -1, "irrelevant")

    data = client.get("/api/admin/feedback").json()["data"]
    assert data["total"] == 3
    assert data["summary"]["likes"] == 2
    assert data["summary"]["dislikes"] == 1
    assert data["summary"]["pending"] == 3
    assert data["summary"]["likeRate"] == pytest.approx(0.6667)

    only_bad = client.get("/api/admin/feedback?rating=-1").json()["data"]
    assert only_bad["total"] == 1
    assert only_bad["items"][0]["feedbackId"] == bad["feedbackId"]

    keyword = client.get("/api/admin/feedback?q=答错了").json()["data"]
    assert keyword["total"] == 1

    future = client.get("/api/admin/feedback?dateFrom=2099-01-01").json()["data"]
    assert future["total"] == 0

    page = client.get("/api/admin/feedback?pageSize=2&page=2").json()["data"]
    assert page["total"] == 3 and page["pages"] == 2 and len(page["items"]) == 1


def test_admin_handle_and_reopen() -> None:
    item = _make_feedback("S-HANDLE", -1, "incomplete")
    url = f"/api/admin/feedback/{item['feedbackId']}"

    resp = client.patch(url, json={"handled": True, "note": "已联系业务部门更新"})
    assert resp.status_code == 200, resp.text
    updated = resp.json()["data"]
    assert updated["handled"] == 1
    assert updated["handleNote"] == "已联系业务部门更新"
    assert updated["handledAt"]

    pending = client.get("/api/admin/feedback?handled=1").json()["data"]
    assert pending["total"] == 1

    reopened = client.patch(url, json={"handled": False}).json()["data"]
    assert reopened["handled"] == 0
    assert reopened["handledAt"] is None

    assert client.patch("/api/admin/feedback/99999", json={"handled": True}).status_code == 404
    assert client.get("/api/admin/feedback/99999").status_code == 404


def test_admin_filters_and_export() -> None:
    _make_feedback("S-EXPORT1", 1)
    _make_feedback("S-EXPORT2", -1, "inaccurate")

    options = client.get("/api/admin/feedback/filters").json()["data"]
    assert {r["code"] for r in options["reasons"]} == {
        "inaccurate",
        "incomplete",
        "irrelevant",
        "other",
    }
    assert options["reasonLabels"]["inaccurate"] == "内容不准确"

    resp = client.get("/api/admin/feedback/export")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("text/csv")
    assert "filename*=UTF-8''" in resp.headers["Content-Disposition"]
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    text = resp.content.decode("utf-8-sig")
    lines = text.splitlines()
    assert lines[0].startswith("反馈ID,反馈时间,评价")
    assert len(lines) == 3
    assert "点踩" in text and "内容不准确" in text


def test_json_store_filter_options_collects_channels() -> None:
    session_id, msg_id = _seed("S-CHANNEL")
    client.post(
        "/api/feedback",
        json={
            "sessionId": session_id,
            "msgId": msg_id,
            "rating": 1,
            "channelCode": "kiosk-1",
        },
    )
    options = client.get("/api/admin/feedback/filters").json()["data"]
    assert "kiosk-1" in options["channels"]


# ------------------------------------------------------------------ 鉴权 / 权限
def test_admin_feedback_auth_guards(monkeypatch) -> None:
    monkeypatch.setattr(settings, "auth_enabled", True)

    assert client.get("/api/admin/feedback").status_code == 401

    user_token = auth.create_token("20230001", "本科生", user_id=1)
    assert (
        client.get(
            "/api/admin/feedback", headers={"Authorization": f"Bearer {user_token}"}
        ).status_code
        == 403
    )

    admin_token = auth.create_token("admin", "管理员", user_id=1)
    assert (
        client.get(
            "/api/admin/feedback", headers={"Authorization": f"Bearer {admin_token}"}
        ).status_code
        == 200
    )


def test_feedback_permission_mapping() -> None:
    assert permissions.required_for("GET", "/api/admin/feedback") == "feedback.view"
    assert permissions.required_for("GET", "/api/admin/feedback/12") == "feedback.view"
    assert (
        permissions.required_for("GET", "/api/admin/feedback/export")
        == "feedback.manage"
    )
    assert (
        permissions.required_for("PATCH", "/api/admin/feedback/12")
        == "feedback.manage"
    )
    assert "feedback.view" in permissions.all_codes()
    assert len(permissions.GROUPS) == 19
    assert len(permissions.ALL_CODES) == 42

    monkeypatch_principal = Principal(role="管理员", authenticated=True, manager=True)
    assert permissions.has(monkeypatch_principal, "feedback.manage")
