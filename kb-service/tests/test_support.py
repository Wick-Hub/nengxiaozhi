"""人工客服体系测试（任务 4.6 / P-B-09～13、M-S-02～06、M-SYS-04·05）。

覆盖：
1. 转人工路由（按渠道匹配分组、在线坐席、离线留言）与会话幂等复用；
2. 用户凭证（高熵 accessKey，服务端只存哈希、错误凭证 403、无法枚举）；
3. 工单生命周期（待接入 → 接待中 → 已回复 → 接待中 → 已关闭）；
4. 管理端清单筛选、情绪标签、分组 / 坐席 CRUD、上下线与回复日志；
5. 鉴权与权限点映射（support.view / support.manage）；
6. 聊天主链路：转人工建议与负面情绪打标。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db import support_store
from app.main import app
from app.security import auth, permissions
from app.services import sentiment, support

client = TestClient(app)


def _groups() -> list[dict]:
    resp = client.get("/api/admin/support/groups")
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _seats() -> list[dict]:
    resp = client.get("/api/admin/support/seats")
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _create(question: str = "我想转人工查询校园卡补办进度", session: str = "S-SUP-1") -> dict:
    resp = client.post(
        "/api/support/tickets",
        json={
            "sessionId": session,
            "question": question,
            "role": "本科生",
            "channelCode": "portal-web",
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _seat_id(online: bool | None = True) -> int:
    seats = _seats()
    if online is None:
        return int(seats[0]["seatId"])
    return int(next(s for s in seats if bool(s["online"]) is online)["seatId"])


def _set_online(seat_id: int, online: bool) -> None:
    resp = client.post(f"/api/admin/support/seats/{seat_id}/status", json={"online": online})
    assert resp.status_code == 200, resp.text


def _offline_all() -> None:
    for seat in _seats():
        if seat["online"]:
            _set_online(int(seat["seatId"]), False)


# ------------------------------------------------------------------ 种子与转人工
def test_seed_groups_and_seats() -> None:
    groups = _groups()
    assert [g["groupName"] for g in groups] == ["综合客服组", "教务客服组"]
    assert groups[0]["seatCount"] == 2
    seats = _seats()
    assert len(seats) == 3
    assert seats[0]["seatName"] == "演示坐席·小智"
    assert seats[0]["online"] is True
    assert seats[0]["groupName"] == "综合客服组"


def test_create_ticket_routes_online_seat_and_hides_key_hash() -> None:
    data = _create()
    ticket = data["ticket"]
    assert data["reused"] is False
    assert ticket["state"] == 1
    assert ticket["stateLabel"] == "待接入"
    assert ticket["seatName"] == "演示坐席·小智"
    assert ticket["groupName"] == "综合客服组"
    assert ticket["ticketNo"].startswith("SUP")
    assert ticket["messages"][0]["senderType"] == 1
    assert ticket["messages"][-1]["senderType"] == 3
    assert "accessKeyHash" not in ticket
    assert len(data["accessKey"]) >= 20
    # 高熵凭证：服务端只落哈希
    assert support_store.ticket_access_hash(ticket["ticketId"]) != data["accessKey"]


def test_session_reuse_rotates_access_key() -> None:
    first = _create(session="S-SUP-REUSE")
    second = _create(session="S-SUP-REUSE")
    assert second["reused"] is True
    assert second["ticket"]["ticketId"] == first["ticket"]["ticketId"]
    assert second["accessKey"] != first["accessKey"]

    old = client.get(
        f"/api/support/tickets/{first['ticket']['ticketId']}",
        params={"accessKey": first["accessKey"]},
    )
    assert old.status_code == 403
    new = client.get(
        f"/api/support/tickets/{first['ticket']['ticketId']}",
        params={"accessKey": second["accessKey"]},
    )
    assert new.status_code == 200


def test_user_side_requires_access_key() -> None:
    data = _create(session="S-SUP-KEY")
    ticket_id = data["ticket"]["ticketId"]
    assert client.get(f"/api/support/tickets/{ticket_id}").status_code == 422
    bad = client.get(
        f"/api/support/tickets/{ticket_id}", params={"accessKey": "x" * 32}
    )
    assert bad.status_code == 403
    bad_msg = client.post(
        f"/api/support/tickets/{ticket_id}/messages",
        json={"content": "在吗", "accessKey": "y" * 32},
    )
    assert bad_msg.status_code == 403
    # 顺序工单号不能靠猜读取：不存在/无凭证统一 403
    assert (
        client.get("/api/support/tickets/99999", params={"accessKey": "z" * 32}).status_code
        == 403
    )


# ------------------------------------------------------------------ 生命周期
def test_ticket_full_lifecycle() -> None:
    data = _create(session="S-SUP-LIFE")
    ticket_id = data["ticket"]["ticketId"]
    key = data["accessKey"]
    seat_id = _seat_id(online=True)

    # 坐席接入
    resp = client.post(f"/api/admin/support/tickets/{ticket_id}/claim", params={"seatId": seat_id})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["state"] == 2

    # 坐席回复
    resp = client.post(
        f"/api/admin/support/tickets/{ticket_id}/reply",
        json={"content": "同学你好，补办进度可在信息中心查询。", "seatId": seat_id},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["ticket"]["unreadUser"] >= 1

    # 用户拉取：拿到回复并标记已推送
    view = client.get(f"/api/support/tickets/{ticket_id}", params={"accessKey": key})
    assert view.status_code == 200
    body = view.json()["data"]
    assert body["newReplies"] >= 1
    assert any("信息中心" in m["content"] for m in body["ticket"]["messages"])
    again = client.get(f"/api/support/tickets/{ticket_id}", params={"accessKey": key})
    assert again.json()["data"]["newReplies"] == 0

    # 用户继续发言
    resp = client.post(
        f"/api/support/tickets/{ticket_id}/messages",
        json={"content": "好的，谢谢！", "accessKey": key},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["ticket"]["state"] == 2

    # 坐席结束
    resp = client.post(
        f"/api/admin/support/tickets/{ticket_id}/close",
        json={"seatId": seat_id, "reason": "问题已解答"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["state"] == 5
    closed_view = client.get(f"/api/support/tickets/{ticket_id}", params={"accessKey": key})
    assert closed_view.json()["data"]["ticket"]["state"] == 5

    # 已结束会话不能再发言
    resp = client.post(
        f"/api/support/tickets/{ticket_id}/messages",
        json={"content": "还有一个问题", "accessKey": key},
    )
    assert resp.status_code == 409


def test_offline_leave_message_and_reply_resumes_active() -> None:
    _offline_all()
    data = _create(question="我要投诉食堂一直没人处理，太差了", session="S-SUP-OFFLINE")
    ticket = data["ticket"]
    assert ticket["state"] == 3
    assert ticket["stateLabel"] == "离线留言"
    assert ticket["sentiment"] == 1
    assert ticket["sentimentLabel"] == "负面"
    ticket_id = ticket["ticketId"]

    seat_id = _seat_id(online=None)
    _set_online(seat_id, True)
    resp = client.post(
        f"/api/admin/support/tickets/{ticket_id}/reply",
        json={"content": "抱歉给您带来不便，我们已记录并转交后勤。", "seatId": seat_id},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["ticket"]["state"] == 4

    view = client.get(
        f"/api/support/tickets/{ticket_id}", params={"accessKey": data["accessKey"]}
    ).json()["data"]
    assert view["ticket"]["state"] == 4
    assert view["notice"]["kind"] == "new_reply"
    assert view["newReplies"] >= 1

    # 用户回话后恢复接待中
    resp = client.post(
        f"/api/support/tickets/{ticket_id}/messages",
        json={"content": "收到，谢谢", "accessKey": data["accessKey"]},
    )
    assert resp.json()["data"]["ticket"]["state"] == 2


def test_negative_sentiment_accumulates() -> None:
    _offline_all()
    data = _create(question="校园卡怎么补办", session="S-SUP-SENT")
    assert data["ticket"]["sentiment"] == 0
    ticket_id = data["ticket"]["ticketId"]
    client.post(
        f"/api/support/tickets/{ticket_id}/messages",
        json={"content": "你们处理太慢了，我很不满", "accessKey": data["accessKey"]},
    )
    detail = client.get(f"/api/admin/support/tickets/{ticket_id}").json()["data"]
    assert detail["sentiment"] == 1
    assert any(m["sentiment"] == 1 for m in detail["messages"])


def test_user_can_close_ticket() -> None:
    data = _create(session="S-SUP-UCLOSE")
    resp = client.post(
        f"/api/support/tickets/{data['ticket']['ticketId']}/close",
        json={"accessKey": data["accessKey"], "reason": "已自行解决"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["state"] == 5


# ------------------------------------------------------------------ 管理端
def test_admin_filters_search_and_overview() -> None:
    _create(question="我要投诉网络一直断网", session="S-SUP-LIST-1")
    _create(question="请问图书馆开放时间", session="S-SUP-LIST-2")

    overview = client.get("/api/admin/support/overview").json()["data"]
    assert overview["total"] >= 2
    assert overview["states"][0]["label"] == "待接入"
    assert overview["totalSeats"] == 3

    negative = client.get(
        "/api/admin/support/tickets", params={"sentiment": 1}
    ).json()["data"]
    assert negative["total"] >= 1
    assert all(item["sentiment"] == 1 for item in negative["items"])

    found = client.get(
        "/api/admin/support/tickets", params={"q": "图书馆开放时间"}
    ).json()["data"]
    assert found["total"] == 1
    assert found["items"][0]["subject"] == "请问图书馆开放时间"

    filters = client.get("/api/admin/support/filters").json()["data"]
    assert {s["label"] for s in filters["states"]} == {
        "待接入", "接待中", "离线留言", "已回复", "已关闭"
    }
    assert filters["sentiments"] == [
        {"value": 0, "label": "正常"},
        {"value": 1, "label": "负面"},
    ]


def test_admin_assign_and_reply_logs() -> None:
    _create(session="S-SUP-ASSIGN")
    ticket = client.get("/api/admin/support/tickets").json()["data"]["items"][0]
    ticket_id = ticket["ticketId"]
    target = _seats()[1]
    resp = client.post(
        f"/api/admin/support/tickets/{ticket_id}/assign",
        json={"groupId": target["groupId"], "seatId": target["seatId"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["seatId"] == target["seatId"]

    _set_online(int(target["seatId"]), True)
    client.post(
        f"/api/admin/support/tickets/{ticket_id}/reply",
        json={"content": "已为您转交处理。", "seatId": target["seatId"]},
    )
    logs = client.get("/api/admin/support/reply-logs").json()["data"]
    assert any(row["ticketId"] == ticket_id and row["messageCount"] >= 1 for row in logs)


def test_group_and_seat_crud() -> None:
    resp = client.post(
        "/api/admin/support/groups",
        json={"groupName": "测试客服组", "channelIds": "test-ch", "description": "测试"},
    )
    assert resp.status_code == 200, resp.text
    group_id = resp.json()["data"]["groupId"]
    assert client.post(
        "/api/admin/support/groups", json={"groupName": "测试客服组"}
    ).status_code == 409

    resp = client.post(
        "/api/admin/support/seats",
        json={
            "userNo": "seat-test",
            "seatName": "测试坐席",
            "groupId": group_id,
            "maxSessions": 3,
            "online": True,
        },
    )
    assert resp.status_code == 200, resp.text
    seat_id = resp.json()["data"]["seatId"]
    assert client.post(
        "/api/admin/support/seats",
        json={"userNo": "seat-test", "seatName": "重复坐席"},
    ).status_code == 409

    # 组内仍有坐席时不能删除
    assert client.delete(f"/api/admin/support/groups/{group_id}").status_code == 409
    patched = client.patch(
        f"/api/admin/support/seats/{seat_id}", json={"seatName": "测试坐席二号"}
    )
    assert patched.json()["data"]["seatName"] == "测试坐席二号"
    assert client.delete(f"/api/admin/support/seats/{seat_id}").status_code == 200
    assert client.delete(f"/api/admin/support/groups/{group_id}").status_code == 200


def test_seat_offline_requeues_queued_tickets() -> None:
    _offline_all()
    primary = _seat_id(online=None)
    _set_online(primary, True)
    data = _create(session="S-SUP-REQUEUE")
    assert data["ticket"]["seatId"] == primary
    _set_online(primary, False)
    detail = client.get(
        f"/api/admin/support/tickets/{data['ticket']['ticketId']}"
    ).json()["data"]
    assert detail["state"] == 1
    assert detail["seatId"] is None


def test_input_validation() -> None:
    assert client.post("/api/support/tickets", json={"question": "  "}).status_code == 400
    assert client.post(
        "/api/admin/support/groups", json={"groupName": "x"}
    ).status_code == 400
    assert client.post(
        "/api/admin/support/seats", json={"userNo": "seat-x", "seatName": "x"}
    ).status_code == 400
    assert client.post(
        "/api/admin/support/seats",
        json={"userNo": "seat-x", "seatName": "坐席X", "maxSessions": 99},
    ).status_code == 422


# ------------------------------------------------------------------ 鉴权 / 权限
def test_admin_auth_guards_and_permission_mapping(monkeypatch) -> None:
    assert permissions.required_for("GET", "/api/admin/support/tickets") == "support.view"
    assert permissions.required_for("POST", "/api/admin/support/tickets/1/reply") == "support.manage"
    assert permissions.required_for("GET", "/api/admin/support/groups") == "support.view"
    assert permissions.required_for("DELETE", "/api/admin/support/seats/1") == "support.manage"

    monkeypatch.setattr(settings, "auth_enabled", True)
    assert client.get("/api/admin/support/tickets").status_code == 401
    user_token = auth.create_token("20230001", "本科生", user_id=1)
    assert (
        client.get(
            "/api/admin/support/tickets",
            headers={"Authorization": f"Bearer {user_token}"},
        ).status_code
        == 403
    )
    admin_token = auth.create_token("admin", "管理员", user_id=1)
    assert (
        client.get(
            "/api/admin/support/tickets",
            headers={"Authorization": f"Bearer {admin_token}"},
        ).status_code
        == 200
    )


# ------------------------------------------------------------------ 聊天主链路
def test_handoff_hint_and_sentiment_unit() -> None:
    assert support.handoff_hint("我要转人工")["suggested"] is True
    assert support.handoff_hint("我要投诉食堂")["suggested"] is True
    assert support.handoff_hint("图书馆几点开门")["suggested"] is False
    assert sentiment.tag("服务太差了，我要投诉") == sentiment.NEGATIVE
    assert sentiment.tag("请问校车几点发车") == sentiment.NORMAL
    assert sentiment.label(1) == "负面"


def test_chat_returns_handoff_fields() -> None:
    body = client.post(
        "/api/chat", json={"question": "我要转人工", "role": "本科生"}
    ).json()
    assert body["ok"] is True
    assert body["data"]["handoffSuggested"] is True
    assert "转人工" in body["data"]["handoffKeywords"]
    assert body["data"]["supportEnabled"] is True
    other = client.post(
        "/api/chat", json={"question": "校园网怎么开通", "role": "本科生"}
    ).json()["data"]
    assert other["handoffSuggested"] is False


def test_chat_stream_carries_handoff_fields() -> None:
    with client.stream(
        "POST", "/api/chat/stream", json={"question": "人工客服在吗", "role": "本科生"}
    ) as resp:
        assert resp.status_code == 200
        done = None
        for line in resp.iter_lines():
            if line.startswith("data: "):
                import json

                event = json.loads(line[6:])
                if event.get("type") == "done":
                    done = event
        assert done is not None
        assert done["handoffSuggested"] is True
        assert done["supportEnabled"] is True
