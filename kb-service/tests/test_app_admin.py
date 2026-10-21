"""智能体广场应用管理与角色授权测试（任务 3.3 / M-C-05·06）。

覆盖：

1. **CRUD**：新建智能体应用 / 外链应用、清单排序、详情、更新、删除；
2. **角色授权**：按当前身份过滤广场，未知角色回落「访客」，token 角色优先；
3. **来源联动**：助手上下架/删除后，智能体应用自动从广场隐藏；
4. **参数校验**：名称/分类/简介长度、来源、链接协议、图标、排序、状态、角色；
5. **存储口径**：「全部」收敛、角色去重排序、JSON 文件隔离；
6. **MySQL 分支（桩）**：行 → camelCase 映射与逗号角色拆分。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db import agent_store, app_store
from app.main import app
from app.security import auth
from app.services import app_admin

client = TestClient(app)


@pytest.fixture()
def plaza_agent() -> dict:
    return agent_store.create_agent(
        {
            "agentCode": "plaza-agent-a",
            "agentName": "广场助手A",
            "agentType": "chat",
        }
    )


def _payload(agent: dict, **overrides) -> dict:
    payload = {
        "appName": "财务助手",
        "sourceType": "agent",
        "agentId": agent["agentId"],
        "category": "事务办理",
        "description": "报销与经费查询",
        "iconUrl": "💰",
        "sortNo": 10,
        "visibleRoles": ["全部"],
        "status": 1,
    }
    payload.update(overrides)
    return payload


def _create(agent: dict, **overrides) -> dict:
    resp = client.post("/api/admin/apps", json=_payload(agent, **overrides))
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


# ------------------------------------------------------------------ 创建 / 清单 / 详情
def test_create_agent_app_and_list(plaza_agent: dict) -> None:
    item = _create(
        plaza_agent,
        appName="财务助手",
        visibleRoles=["教职工", "本科生"],
        sortNo=5,
    )
    assert item["appName"] == "财务助手"
    assert item["agentId"] == plaza_agent["agentId"]
    assert item["agentName"] == "广场助手A"
    assert item["agentStatus"] == 1
    assert item["agentMissing"] is False
    # 角色按身份字典顺序归一，不按提交顺序
    assert item["visibleRoles"] == ["本科生", "教职工"]

    link = _create(
        plaza_agent,
        appName="办事大厅",
        sourceType="link",
        agentId=None,
        linkUrl="https://ehall.example.edu.cn",
        category="信息服务",
        visibleRoles=["全部"],
        sortNo=1,
    )
    assert link["sourceType"] == "link"
    assert link["agentId"] is None
    assert link["linkUrl"] == "https://ehall.example.edu.cn"
    assert link["visibleRoles"] == ["全部"]

    data = client.get("/api/admin/apps").json()["data"]
    assert data["total"] == 2
    assert data["enabled"] == 2
    # sortNo 小的在前
    assert [x["appName"] for x in data["items"]] == ["办事大厅", "财务助手"]
    assert data["roleOptions"][0] == "全部"
    assert set(app_store.VISIBLE_ROLES).issubset(set(data["roleOptions"]))
    assert "事务办理" in data["categoryOptions"]
    assert any(a["agentId"] == plaza_agent["agentId"] for a in data["agentOptions"])

    detail = client.get(f"/api/admin/apps/{item['appId']}").json()["data"]
    assert detail["appId"] == item["appId"]
    assert detail["agentName"] == "广场助手A"


# ------------------------------------------------------------------ 公开广场 / 角色授权
def test_public_apps_filter_by_role(plaza_agent: dict) -> None:
    _create(plaza_agent, appName="所有人应用", visibleRoles=["全部"], sortNo=1)
    _create(plaza_agent, appName="教职工应用", visibleRoles=["教职工"], sortNo=2)
    _create(plaza_agent, appName="学生应用", visibleRoles=["本科生", "研究生"], sortNo=3)

    undergrad = client.get("/api/apps", params={"role": "本科生"}).json()["data"]
    assert undergrad["role"] == "本科生"
    assert [x["appName"] for x in undergrad["apps"]] == ["所有人应用", "学生应用"]
    assert undergrad["total"] == 2
    # 公开端不泄露授权配置
    assert "visibleRoles" not in undergrad["apps"][0]

    staff = client.get("/api/apps", params={"role": "教职工"}).json()["data"]
    assert [x["appName"] for x in staff["apps"]] == ["所有人应用", "教职工应用"]

    guest = client.get("/api/apps").json()["data"]
    assert guest["role"] == "访客"
    assert [x["appName"] for x in guest["apps"]] == ["所有人应用"]

    alien = client.get("/api/apps", params={"role": "外星人"}).json()["data"]
    assert alien["role"] == "访客"
    assert alien["total"] == 1


def test_token_role_overrides_query_param(plaza_agent: dict) -> None:
    _create(plaza_agent, appName="教职工专属", visibleRoles=["教职工"])

    token = auth.create_token("T10001", "教职工")
    resp = client.get(
        "/api/apps",
        params={"role": "访客"},
        headers={"Authorization": f"Bearer {token}"},
    )
    data = resp.json()["data"]
    assert data["role"] == "教职工"
    assert [x["appName"] for x in data["apps"]] == ["教职工专属"]

    token = auth.create_token("20230001", "本科生")
    data = client.get(
        "/api/apps",
        params={"role": "教职工"},
        headers={"Authorization": f"Bearer {token}"},
    ).json()["data"]
    assert data["role"] == "本科生"
    assert data["apps"] == []


# ------------------------------------------------------------------ 助手联动
def test_app_follows_agent_status_and_deletion(plaza_agent: dict) -> None:
    _create(plaza_agent, appName="随助手下线")
    agent_id = plaza_agent["agentId"]

    agent_store.update_agent(agent_id, {"status": 0})
    admin_item = client.get("/api/admin/apps").json()["data"]["items"][0]
    assert admin_item["agentStatus"] == 0
    assert admin_item["agentMissing"] is False
    assert client.get("/api/apps?role=访客").json()["data"]["apps"] == []

    agent_store.update_agent(agent_id, {"status": 1})
    assert len(client.get("/api/apps?role=访客").json()["data"]["apps"]) == 1

    assert agent_store.delete_agent(agent_id) is True
    admin_item = client.get("/api/admin/apps").json()["data"]["items"][0]
    assert admin_item["agentMissing"] is True
    assert admin_item["agentName"] == ""
    assert client.get("/api/apps?role=访客").json()["data"]["apps"] == []


def test_link_app_not_affected_by_agent_state(plaza_agent: dict) -> None:
    _create(
        plaza_agent,
        appName="外链应用",
        sourceType="link",
        agentId=None,
        linkUrl="/guide",
        visibleRoles=["全部"],
    )
    agent_store.delete_agent(plaza_agent["agentId"])
    apps = client.get("/api/apps?role=访客").json()["data"]["apps"]
    assert [x["appName"] for x in apps] == ["外链应用"]


# ------------------------------------------------------------------ 更新 / 启停 / 删除
def test_update_roles_status_and_partial_merge(plaza_agent: dict) -> None:
    item = _create(plaza_agent, appName="可编辑", visibleRoles=["全部"], status=1)

    resp = client.patch(
        f"/api/admin/apps/{item['appId']}",
        json={"visibleRoles": ["研究生"], "status": 0, "category": "学习学术"},
    )
    assert resp.status_code == 200, resp.text
    updated = resp.json()["data"]
    assert updated["visibleRoles"] == ["研究生"]
    assert updated["status"] == 0
    assert updated["category"] == "学习学术"
    # 未提交字段沿用旧值
    assert updated["appName"] == "可编辑"
    assert updated["sourceType"] == "agent"
    assert updated["agentId"] == plaza_agent["agentId"]
    assert client.get("/api/apps?role=研究生").json()["data"]["apps"] == []

    client.patch(f"/api/admin/apps/{item['appId']}", json={"status": 1})
    apps = client.get("/api/apps?role=研究生").json()["data"]["apps"]
    assert [x["appName"] for x in apps] == ["可编辑"]
    assert client.get("/api/apps?role=本科生").json()["data"]["apps"] == []


def test_delete_app(plaza_agent: dict) -> None:
    item = _create(plaza_agent)
    assert client.delete(f"/api/admin/apps/{item['appId']}").status_code == 200
    assert client.get(f"/api/admin/apps/{item['appId']}").status_code == 404
    assert client.delete(f"/api/admin/apps/{item['appId']}").status_code == 404
    assert client.patch(f"/api/admin/apps/{item['appId']}", json={"status": 1}).status_code == 404


# ------------------------------------------------------------------ 校验
def test_validation_cases(plaza_agent: dict) -> None:
    cases = [
        ({"appName": ""}, "应用名称不能为空"),
        ({"appName": "名" * 129}, "应用名称不能超过"),
        ({"category": ""}, "应用分类不能为空"),
        ({"category": "类" * 33}, "应用分类不能超过"),
        ({"description": "简" * 256}, "应用简介不能超过"),
        ({"sourceType": "iframe"}, "应用来源"),
        ({"agentId": None}, "必须选择绑定助手"),
        ({"agentId": 999999}, "绑定的智能体不存在"),
        ({"sourceType": "link", "linkUrl": ""}, "必须填写链接地址"),
        ({"sourceType": "link", "linkUrl": "javascript:alert(1)"}, "链接协议不安全"),
        ({"sourceType": "link", "linkUrl": "ftp://example.com/a"}, "链接需以 http(s)"),
        ({"sourceType": "link", "linkUrl": "https://a.example/b c"}, "空白"),
        ({"iconUrl": "javascript:alert(1)"}, "图标链接协议不安全"),
        ({"iconUrl": "ftp://example.com/i.png"}, "图标链接需以 http(s)"),
        ({"sortNo": 10000}, "排序需在"),
        ({"visibleRoles": ["外星人"]}, "未知角色"),
    ]
    for idx, (override, keyword) in enumerate(cases):
        payload = _payload(plaza_agent)
        payload.update(override)
        if "appName" not in override:
            payload["appName"] = f"校验-{idx}"
        resp = client.post("/api/admin/apps", json=payload)
        assert resp.status_code == 400, (override, resp.text)
        assert keyword in resp.json()["detail"], (override, resp.json()["detail"])


def test_schema_level_rejects_type_errors(plaza_agent: dict) -> None:
    for override in ({"sortNo": "abc"}, {"status": 2}):
        resp = client.post("/api/admin/apps", json=_payload(plaza_agent, **override))
        assert resp.status_code == 422, (override, resp.text)


def test_update_revalidates_merged_payload(plaza_agent: dict) -> None:
    item = _create(plaza_agent)
    resp = client.patch(
        f"/api/admin/apps/{item['appId']}",
        json={"sourceType": "link"},
    )
    assert resp.status_code == 400
    assert "必须填写链接地址" in resp.json()["detail"]

    resp = client.patch(
        f"/api/admin/apps/{item['appId']}",
        json={"visibleRoles": ["不存在的角色"]},
    )
    assert resp.status_code == 400
    assert "未知角色" in resp.json()["detail"]


def test_role_normalization_helpers() -> None:
    assert app_admin._clean_roles(None) == (["全部"], None)
    assert app_admin._clean_roles([]) == (["全部"], None)
    assert app_admin._clean_roles(["全部", "教职工"]) == (["全部"], None)
    assert app_admin._clean_roles(["教职工", "本科生", "教职工"]) == (
        ["本科生", "教职工"],
        None,
    )
    assert app_admin._clean_roles("研究生、访客") == (["访客", "研究生"], None)
    assert app_store.split_roles("教职工,本科生") == ["本科生", "教职工"]
    assert app_store.join_roles(["全部", "本科生"]) == "全部"


# ------------------------------------------------------------------ MySQL 分支（桩）
class _Result:
    def __init__(self, rows=()) -> None:
        self._rows = [tuple(r) for r in rows]

    def all(self) -> list[tuple]:
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._rows[0][0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class _Conn:
    def __init__(self, results) -> None:
        self._queue = list(results)
        self.statements: list = []

    def execute(self, stmt, *args, **kwargs) -> _Result:
        self.statements.append(stmt)
        if not self._queue:
            raise AssertionError("MySQL 桩：查询次数多于预期")
        return self._queue.pop(0)


class _Session:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    def __enter__(self) -> _Conn:
        return self._conn

    def __exit__(self, *exc) -> bool:
        return False


def test_mysql_list_maps_rows_and_splits_roles(monkeypatch) -> None:
    now = datetime(2026, 10, 20, 12, 0, 0)
    row = (
        3,
        "MySQL 应用",
        "agent",
        7,
        "",
        "事务办理",
        "简介",
        "🧩",
        2,
        "教职工,本科生",
        1,
        now,
        now,
    )
    conn = _Conn([_Result([row])])
    monkeypatch.setattr(settings, "kb_store", "mysql")
    monkeypatch.setattr(app_store, "db_session", lambda: _Session(conn))

    items = app_store.list_apps()
    assert len(items) == 1
    assert items[0]["appName"] == "MySQL 应用"
    assert items[0]["visibleRoles"] == ["本科生", "教职工"]
    assert items[0]["updatedAt"] == "2026-10-20T12:00:00"
    assert items[0]["status"] == 1
