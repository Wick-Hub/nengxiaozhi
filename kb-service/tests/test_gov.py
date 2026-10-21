"""导办事项与免登录票据测试（任务 4.5 / P-DB-01·02、M-G-01、I-03）。

覆盖：

1. **事项同步**：mock 幂等导入、按名称+部门去重、不覆盖本地启停与生效时段；
   HTTP 源四种返回形态与错误映射。
2. **管理端**：新建/编辑/删除、URL/部门/时段校验、清空生效时段、字典与汇总。
3. **公开检索与推荐**：按角色与生效窗口过滤、关键词打分、无意图不推荐、
   浏览意图回退常用事项。
4. **票据**：签发、一次性兑换、过期/停用/越权/格式错误、无需票据直达、
   鉴权开启后未登录 401 与身份取自 token。
5. **对话集成**：非流式与 SSE `done` 均携带 `guideItems`。
6. **页面与权限**：`/gov` `/hall` 可访问，票据页 no-referrer；RBAC 映射。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db import gov_store
from app.main import app
from app.security import auth, permissions
from app.services import gov_admin

client = TestClient(app)


def _sync() -> dict:
    resp = client.post("/api/admin/gov/sync")
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _admin_items(**params) -> list[dict]:
    resp = client.get("/api/admin/gov/items", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["items"]


def _item_by_name(name: str) -> dict:
    for item in _admin_items(q=name):
        if item["itemName"] == name:
            return item
    raise AssertionError(f"未找到事项：{name}")


def _create_item(**overrides) -> dict:
    payload = {
        "itemName": "测试事项",
        "deptId": 1,
        "hallUrl": "/hall",
        "needTicket": True,
        "conditions": "在校师生且状态正常。",
        "materials": "身份证原件。",
        "keywordTags": ["测试", "办理"],
        "visibleRoles": ["全部"],
        "status": 1,
    }
    payload.update(overrides)
    resp = client.post("/api/admin/gov/items", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _seed_item(**overrides) -> int:
    fields = {
        "item_name": "票据测试事项",
        "dept_id": 1,
        "hall_url": "/hall",
        "need_ticket": True,
        "conditions": "",
        "materials": "",
        "keyword_tags": ["票据"],
        "visible_roles": ["全部"],
        "status": 1,
        "effective_from": None,
        "effective_to": None,
    }
    fields.update(overrides)
    return gov_store.create_item(**fields)


def _sse_events(text: str) -> list[dict]:
    events: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        try:
            events.append(json.loads(line[5:].strip()))
        except ValueError:
            continue
    return events


# ------------------------------------------------------------------ 页面
def test_pages_served_and_hall_referrer_policy() -> None:
    resp = client.get("/gov")
    assert resp.status_code == 200
    assert "导办事项管理" in resp.text
    assert "gov.js" in resp.text

    resp = client.get("/hall")
    assert resp.status_code == 200
    assert 'name="referrer" content="no-referrer"' in resp.text
    assert "hall.js" in resp.text

    assert client.get("/hall/").status_code == 200


# ------------------------------------------------------------------ 同步
def test_sync_is_idempotent_and_preserves_local_status() -> None:
    first = _sync()
    assert first["provider"] == "mock"
    assert first["total"] == 9
    assert first["created"] == 9
    assert first["updated"] == 0
    assert first["skipped"] == 0

    second = _sync()
    assert second["created"] == 0
    assert second["updated"] == 9
    assert len(_admin_items()) == 9

    item = _item_by_name("校园卡补办")
    future_from = (datetime.now() + timedelta(days=1)).isoformat(timespec="minutes")
    future_to = (datetime.now() + timedelta(days=30)).isoformat(timespec="minutes")
    resp = client.patch(
        f"/api/admin/gov/items/{item['itemId']}",
        json={"status": 0, "effectiveFrom": future_from, "effectiveTo": future_to},
    )
    assert resp.status_code == 200, resp.text

    third = _sync()
    assert third["created"] == 0 and third["updated"] == 9
    kept = _item_by_name("校园卡补办")
    assert kept["status"] == 0
    assert kept["effectiveFrom"].startswith(future_from)
    assert kept["effectiveTo"].startswith(future_to)
    assert kept["effectiveState"] == "disabled"


def test_admin_crud_validation_and_clear_window() -> None:
    resp = client.post(
        "/api/admin/gov/items",
        json={"itemName": "   ", "deptId": 1, "hallUrl": "/hall"},
    )
    assert resp.status_code == 400
    assert "事项名称" in resp.json()["detail"]

    resp = client.post(
        "/api/admin/gov/items",
        json={"itemName": "坏链接", "deptId": 1, "hallUrl": "javascript:alert(1)"},
    )
    assert resp.status_code == 400
    assert "协议不安全" in resp.json()["detail"]

    resp = client.post(
        "/api/admin/gov/items",
        json={"itemName": "坏部门", "deptId": 99, "hallUrl": "/hall"},
    )
    assert resp.status_code == 400
    assert "主管部门" in resp.json()["detail"]

    invalid_window = {
        "itemName": "倒置时段",
        "deptId": 1,
        "hallUrl": "/hall",
        "effectiveFrom": "2026-10-10T10:00",
        "effectiveTo": "2026-10-01T10:00",
    }
    resp = client.post("/api/admin/gov/items", json=invalid_window)
    assert resp.status_code == 400
    assert "晚于" in resp.json()["detail"]

    window = {
        "effectiveFrom": "2026-10-01T08:00",
        "effectiveTo": "2026-12-31T20:00",
    }
    item = _create_item(**window)
    assert item["deptName"] == "信网中心"
    assert item["effectiveState"] == "active"
    assert item["summary"]

    resp = client.post(
        "/api/admin/gov/items",
        json={
            "itemName": "测试事项",
            "deptId": 1,
            "hallUrl": "/hall",
        },
    )
    assert resp.status_code == 400
    assert "已存在" in resp.json()["detail"]

    resp = client.patch(
        f"/api/admin/gov/items/{item['itemId']}",
        json={
            "itemName": "测试事项（已改）",
            "visibleRoles": ["研究生"],
            "keywordTags": "标签甲，标签乙",
        },
    )
    assert resp.status_code == 200, resp.text
    changed = resp.json()["data"]
    assert changed["itemName"] == "测试事项（已改）"
    assert changed["visibleRoles"] == ["研究生"]
    assert changed["keywordTags"] == ["标签甲", "标签乙"]

    # 显式传 null 清空生效时段
    resp = client.patch(
        f"/api/admin/gov/items/{item['itemId']}",
        json={"effectiveFrom": None, "effectiveTo": None},
    )
    assert resp.status_code == 200
    cleared = resp.json()["data"]
    assert cleared["effectiveFrom"] is None
    assert cleared["effectiveTo"] is None
    assert cleared["effectiveState"] == "active"

    resp = client.get(f"/api/admin/gov/items/{item['itemId']}")
    assert resp.status_code == 200
    assert resp.json()["data"]["itemName"] == "测试事项（已改）"

    assert client.get("/api/admin/gov/items/999999").status_code == 404
    assert (
        client.patch("/api/admin/gov/items/999999", json={"status": 0}).status_code
        == 404
    )

    assert (
        client.delete(f"/api/admin/gov/items/{item['itemId']}").status_code == 200
    )
    assert (
        client.delete(f"/api/admin/gov/items/{item['itemId']}").status_code == 404
    )


def test_admin_meta() -> None:
    data = client.get("/api/admin/gov/meta").json()["data"]
    assert len(data["deptOptions"]) == 9
    assert data["roleOptions"][0] == "全部"
    assert data["ticketTtlSeconds"] == 120
    assert data["sync"]["provider"] == "mock"
    assert data["sync"]["mockCount"] == 9


# ------------------------------------------------------------------ 公开检索
def test_public_items_filter_by_role_and_window() -> None:
    _sync()

    data = client.get("/api/gov/items", params={"role": "本科生", "limit": 50}).json()["data"]
    names = [item["itemName"] for item in data["items"]]
    assert "校园卡补办" in names
    assert "教职工收入证明" not in names
    assert "学生证补办" in names

    data = client.get("/api/gov/items", params={"role": "教职工", "limit": 50}).json()["data"]
    names = [item["itemName"] for item in data["items"]]
    assert "教职工收入证明" in names
    assert "学生证补办" not in names

    data = client.get(
        "/api/gov/items", params={"role": "本科生", "q": "成绩单"}
    ).json()["data"]
    assert data["items"]
    assert data["items"][0]["itemName"] == "中文成绩单打印"
    assert "成绩单" in data["items"][0]["matchReason"]

    future = _seed_item(
        item_name="未来开放事项",
        keyword_tags=["未来"],
        effective_from=datetime.now() + timedelta(days=2),
    )
    public_names = [
        item["itemName"]
        for item in client.get(
            "/api/gov/items", params={"role": "本科生", "limit": 50}
        ).json()["data"]["items"]
    ]
    assert "未来开放事项" not in public_names
    scheduled = client.get(
        "/api/admin/gov/items", params={"status": "scheduled"}
    ).json()["data"]["items"]
    assert [row["itemId"] for row in scheduled] == [future]

    # 无效角色按访客处理（仍能拿到「全部」事项）
    data = client.get("/api/gov/items", params={"role": "外星人"}).json()["data"]
    assert data["role"] == "访客"
    assert data["items"]


def test_recommend_gating_and_role_visibility(monkeypatch) -> None:
    _sync()

    cards = gov_admin.recommend("校园卡丢了怎么补办？", "本科生")
    assert cards
    assert cards[0]["itemName"] == "校园卡补办"
    assert cards[0]["needTicket"] is True
    assert cards[0]["deptName"] == "信网中心"
    assert cards[0]["matchReason"]

    assert gov_admin.recommend("高等数学怎么复习？", "本科生") == []

    browse = gov_admin.recommend("学校有哪些办事事项可以办理？", "本科生")
    assert browse
    assert all(card["matchReason"] == "常用事项" for card in browse)

    _seed_item(
        item_name="研究生评审申请",
        keyword_tags=["研究生", "评审"],
        visible_roles=["研究生"],
    )
    assert not any(
        card["itemName"] == "研究生评审申请"
        for card in gov_admin.recommend("研究生评审怎么申请", "本科生")
    )
    assert any(
        card["itemName"] == "研究生评审申请"
        for card in gov_admin.recommend("研究生评审怎么申请", "研究生")
    )

    monkeypatch.setattr(settings, "gov_enabled", False)
    assert gov_admin.recommend("校园卡丢了怎么补办？", "本科生") == []
    assert client.get("/api/gov/items").json()["data"]["total"] == 0


# ------------------------------------------------------------------ 票据
def test_ticket_end_to_end_and_direct_item() -> None:
    _sync()
    item = _item_by_name("校园卡补办")

    resp = client.post(
        f"/api/gov/items/{item['itemId']}/ticket",
        json={"itemId": item["itemId"], "role": "本科生"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["needTicket"] is True
    assert data["ticket"].startswith("GT")
    assert data["expiresIn"] == 120
    assert data["expiresAt"]

    parts = urlsplit(data["jumpUrl"])
    assert parts.path == "/hall"
    assert parse_qs(parts.query)["ticket"] == [data["ticket"]]

    resp = client.post("/api/gov/tickets/verify", json={"ticket": data["ticket"]})
    assert resp.status_code == 200, resp.text
    verified = resp.json()["data"]
    assert verified["valid"] is True
    assert verified["identity"]["userNo"] == "demo"
    assert verified["identity"]["userName"] == "演示用户"
    assert verified["identity"]["role"] == "本科生"
    assert verified["item"]["itemName"] == "校园卡补办"
    assert verified["item"]["deptName"] == "信网中心"
    assert verified["item"]["materials"]

    resp = client.post("/api/gov/tickets/verify", json={"ticket": data["ticket"]})
    assert resp.status_code == 409
    assert "已使用" in resp.json()["detail"]

    resp = client.post(
        f"/api/gov/items/{item['itemId']}/ticket",
        json={"itemId": item["itemId"] + 1},
    )
    assert resp.status_code == 400

    free = _item_by_name("图书馆座位预约")
    resp = client.post(
        f"/api/gov/items/{free['itemId']}/ticket",
        json={"itemId": free["itemId"], "role": "本科生"},
    )
    assert resp.status_code == 200
    direct = resp.json()["data"]
    assert direct["needTicket"] is False
    assert direct["ticket"] is None
    assert direct["jumpUrl"] == "/hall"


def test_ticket_errors_expired_inactive_and_invalid(monkeypatch) -> None:
    inactive = _seed_item(item_name="已停用事项", status=0)
    resp = client.post(
        f"/api/gov/items/{inactive}/ticket", json={"itemId": inactive}
    )
    assert resp.status_code == 409
    assert "停用" in resp.json()["detail"]

    future = _seed_item(
        item_name="未生效事项",
        effective_from=datetime.now() + timedelta(days=1),
    )
    resp = client.post(f"/api/gov/items/{future}/ticket", json={"itemId": future})
    assert resp.status_code == 409

    restricted = _seed_item(item_name="研究生专属事项", visible_roles=["研究生"])
    resp = client.post(
        f"/api/gov/items/{restricted}/ticket",
        json={"itemId": restricted, "role": "本科生"},
    )
    assert resp.status_code == 403

    active = _seed_item(item_name="过期票据事项")
    resp = client.post(
        f"/api/gov/items/{active}/ticket",
        json={"itemId": active, "role": "本科生"},
    )
    ticket = resp.json()["data"]["ticket"]

    later = datetime.now() + timedelta(hours=2)
    monkeypatch.setattr(gov_admin, "_now", lambda: later)
    resp = client.post("/api/gov/tickets/verify", json={"ticket": ticket})
    assert resp.status_code == 410
    assert "过期" in resp.json()["detail"]

    assert (
        client.post("/api/gov/tickets/verify", json={"ticket": "bad ticket!"}).status_code
        == 400
    )
    assert (
        client.post("/api/gov/tickets/verify", json={"ticket": "GTnotexist"}).status_code
        == 404
    )
    assert client.post(f"/api/gov/items/{active}/ticket", json={"itemId": active}).status_code == 200


def test_ticket_requires_login_and_admin_guards(monkeypatch) -> None:
    item_id = _seed_item(item_name="登录后办理事项")
    monkeypatch.setattr(settings, "auth_enabled", True)

    resp = client.post(f"/api/gov/items/{item_id}/ticket", json={"itemId": item_id})
    assert resp.status_code == 401

    user_token = auth.create_token("20230001", "本科生", user_id=1)
    headers = {"Authorization": f"Bearer {user_token}"}
    resp = client.post(
        f"/api/gov/items/{item_id}/ticket",
        json={"itemId": item_id},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    ticket = resp.json()["data"]["ticket"]

    resp = client.post("/api/gov/tickets/verify", json={"ticket": ticket})
    assert resp.status_code == 200
    identity = resp.json()["data"]["identity"]
    assert identity["userNo"] == "20230001"
    assert identity["userName"] == "20230001"
    assert identity["role"] == "本科生"

    assert client.get("/api/admin/gov/items").status_code == 401
    assert client.get("/api/admin/gov/items", headers=headers).status_code == 403

    admin_token = auth.create_token("admin", "管理员", user_id=1)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    assert client.get("/api/admin/gov/items", headers=admin_headers).status_code == 200
    assert (
        client.post("/api/admin/gov/sync", headers=admin_headers).status_code == 200
    )
    assert (
        client.post("/api/admin/gov/sync", headers=headers).status_code == 403
    )


def test_ticket_store_is_atomic_and_expiry_aware() -> None:
    now = datetime.now()
    gov_store.create_ticket(
        {
            "ticket_id": "GTunitone",
            "item_id": 1,
            "user_no": "u1",
            "user_name": "用户一",
            "role": "本科生",
            "issued_at": now,
            "expires_at": now + timedelta(seconds=60),
        }
    )
    assert gov_store.consume_ticket("GTunitone", now) is True
    assert gov_store.consume_ticket("GTunitone", now) is False
    assert gov_store.get_ticket("GTunitone")["usedAt"] is not None

    gov_store.create_ticket(
        {
            "ticket_id": "GTunitexpired",
            "item_id": 1,
            "user_no": "u2",
            "user_name": "用户二",
            "role": "本科生",
            "issued_at": now - timedelta(minutes=5),
            "expires_at": now - timedelta(minutes=1),
        }
    )
    assert gov_store.consume_ticket("GTunitexpired", now) is False
    assert gov_store.consume_ticket("GTmissing", now) is False


# ------------------------------------------------------------------ 对话集成
def test_chat_includes_guide_items(monkeypatch) -> None:
    _sync()

    resp = client.post(
        "/api/chat",
        json={"question": "校园卡丢了怎么补办？", "role": "本科生"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert [card["itemName"] for card in data["guideItems"]][0] == "校园卡补办"
    assert data["guideItems"][0]["itemId"] > 0

    resp = client.post(
        "/api/chat/stream",
        json={"question": "校园卡丢了怎么补办？", "role": "本科生"},
    )
    assert resp.status_code == 200
    events = _sse_events(resp.text)
    done = [event for event in events if event.get("type") == "done"]
    assert done, events
    assert done[-1]["guideItems"][0]["itemName"] == "校园卡补办"

    resp = client.post(
        "/api/chat",
        json={"question": "高等数学怎么复习？", "role": "本科生"},
    )
    assert resp.json()["data"]["guideItems"] == []

    monkeypatch.setattr(settings, "gov_enabled", False)
    resp = client.post(
        "/api/chat",
        json={"question": "校园卡丢了怎么补办？", "role": "本科生"},
    )
    assert resp.json()["data"]["guideItems"] == []


# ------------------------------------------------------------------ HTTP 同步
class _FakeResponse:
    def __init__(self, body, status_code: int = 200) -> None:
        self._body = body
        self.status_code = status_code

    def json(self):
        return self._body


def test_sync_http_contract(monkeypatch) -> None:
    seen: dict = {}

    def fake_get(url, **kwargs):
        seen["url"] = url
        seen.update(kwargs)
        return _FakeResponse(
            {
                "data": {
                    "items": [
                        {
                            "itemName": "线上报销事项",
                            "deptName": "财务处",
                            "needTicket": False,
                            "keywordTags": "报销,差旅",
                            "visibleRoles": ["教职工"],
                        },
                        {"itemName": ""},
                    ]
                }
            }
        )

    monkeypatch.setattr(settings, "gov_sync_provider", "http")
    monkeypatch.setattr(settings, "gov_sync_url", "https://gov.example.edu/api/items")
    monkeypatch.setattr(settings, "gov_sync_token", "test-token")
    monkeypatch.setattr(gov_admin.httpx, "get", fake_get)

    data = _sync()
    assert data["provider"] == "http"
    assert data["total"] == 2
    assert data["created"] == 1
    assert data["updated"] == 0
    assert data["skipped"] == 1
    assert seen["url"] == "https://gov.example.edu/api/items"
    assert seen["headers"]["Authorization"] == "Bearer test-token"

    items = _admin_items(q="线上报销")
    assert items and items[0]["deptName"] == "财务处"
    assert items[0]["needTicket"] is False

    monkeypatch.setattr(
        gov_admin.httpx,
        "get",
        lambda *args, **kwargs: _FakeResponse({}, status_code=503),
    )
    resp = client.post("/api/admin/gov/sync")
    assert resp.status_code == 502
    assert "503" in resp.json()["detail"]


def test_extract_items_supports_common_shapes() -> None:
    rows = [{"itemName": "A"}]
    assert gov_admin._extract_items(rows)[0] == rows
    assert gov_admin._extract_items({"items": rows})[0] == rows
    assert gov_admin._extract_items({"data": rows})[0] == rows
    assert gov_admin._extract_items({"data": {"items": rows}})[0] == rows
    assert gov_admin._extract_items({"unexpected": 1})[1]


# ------------------------------------------------------------------ 权限
def test_gov_permission_mapping() -> None:
    assert permissions.required_for("GET", "/api/admin/gov/items") == "gov.view"
    assert permissions.required_for("GET", "/api/admin/gov/meta") == "gov.view"
    assert permissions.required_for("POST", "/api/admin/gov/items") == "gov.manage"
    assert permissions.required_for("PATCH", "/api/admin/gov/items/1") == "gov.manage"
    assert permissions.required_for("POST", "/api/admin/gov/sync") == "gov.manage"
    assert "gov.view" in permissions.all_codes()
    assert "gov.manage" in permissions.all_codes()
    assert permissions.required_for("GET", "/api/gov/items") is None
