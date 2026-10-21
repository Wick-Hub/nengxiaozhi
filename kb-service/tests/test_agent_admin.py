"""智能体 / 助手管理测试（任务 2.2 / M-A-01·02·03）。

覆盖四层：

1. **存储层**（``app/db/agent_store.py``）：seed、增删改查、编码唯一、角色字段归一；
2. **服务层**（``app/services/agent_admin.py``）：参数校验、场景预设搭建、
   基础配置七项、启停、删除前置校验；
3. **接口层**（``/api/admin/agents*``）：状态码契约（400/404/409/422）与响应封装；
4. **契约一致性**：验收要求「不同助手有独立 agent 行」——创建后必须各自成行。

测试全程使用 ``KB_STORE=json``，助手文件由 ``conftest`` 重定向到临时目录。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import agent_store
from app.main import app
from app.services import agent_admin

client = TestClient(app)


# ------------------------------------------------------------------ 夹具
@pytest.fixture()
def base_agent() -> dict:
    """seed 出来的默认助手。"""
    return agent_store.list_agents()[0]


@pytest.fixture()
def custom_agent() -> dict:
    """一个自定义助手（用于更新/启停/删除用例）。"""
    result = agent_admin.create_agent(
        {
            "agentCode": "test-custom",
            "agentName": "测试助手",
            "agentType": "chat",
            "description": "用于测试",
            "visibleRoles": ["全部"],
        }
    )
    assert result["ok"], result["error"]
    return result["data"]


# ------------------------------------------------------------------ 1. 存储层
def test_seed_creates_default_agent() -> None:
    agents = agent_store.list_agents()
    assert len(agents) == 1
    assert agents[0]["agentCode"] == agent_store.DEFAULT_AGENT_CODE
    assert agents[0]["statusName"] == "已上架"
    assert agents[0]["visibleRoles"] == ["全部"]
    assert agents[0]["fallbackReply"]  # 兜底话术有默认值


def test_create_assigns_increasing_ids() -> None:
    a = agent_admin.create_agent({"agentCode": "t-a", "agentName": "A"})["data"]
    b = agent_admin.create_agent({"agentCode": "t-b", "agentName": "B"})["data"]
    assert b["agentId"] > a["agentId"], "不同助手必须各自成行（独立自增 ID）"
    assert agent_store.get_agent(a["agentId"])["agentName"] == "A"


def test_get_by_code_and_missing() -> None:
    created = agent_admin.create_agent({"agentCode": "t-lookup", "agentName": "查找"})["data"]
    assert agent_store.get_by_code("t-lookup")["agentId"] == created["agentId"]
    assert agent_store.get_by_code("no-such-code") is None
    assert agent_store.get_agent(999999) is None


def test_roles_normalized_to_list() -> None:
    created = agent_admin.create_agent(
        {"agentCode": "t-roles", "agentName": "角色", "visibleRoles": "本科生,研究生"}
    )["data"]
    assert created["visibleRoles"] == ["本科生", "研究生"]
    # 重新读盘后仍是列表（不是逗号串）
    assert agent_store.get_agent(created["agentId"])["visibleRoles"] == ["本科生", "研究生"]


def test_delete_missing_returns_false() -> None:
    assert agent_store.delete_agent(424242) is False


# ------------------------------------------------------------------ 2. 服务层校验
@pytest.mark.parametrize(
    ("payload", "fragment"),
    [
        ({"agentName": "无编码"}, "缺少助手编码"),
        ({"agentCode": "t-x"}, "缺少助手名称"),
        ({"agentCode": "Bad Code", "agentName": "X"}, "助手编码只能"),
        ({"agentCode": "t-x", "agentName": "X", "agentType": "unknown"}, "助手类型非法"),
        ({"agentCode": "t-x", "agentName": "X", "temperature": 9}, "temperature 需在"),
        ({"agentCode": "t-x", "agentName": "X", "topP": 0}, "topP 需在"),
        ({"agentCode": "t-x", "agentName": "X", "maxTokens": 0}, "maxTokens 需在"),
        ({"agentCode": "t-x", "agentName": "X", "visibleRoles": []}, "可见角色至少"),
        ({"agentCode": "t-x", "agentName": "X", "streamEnabled": 7}, "流式开关"),
        ({"agentCode": "t-x", "agentName": "X", "status": 5}, "状态只能是"),
        ({"agentCode": "t-x", "agentName": "X" * 200}, "助手名称最长"),
        ({"agentCode": "t-x", "agentName": "X", "description": "d" * 300}, "描述最长"),
        ({"agentCode": "t-x", "agentName": "X", "riskReply": "r" * 600}, "风控话术最长"),
        ({"agentCode": "t-x", "agentName": "X", "fallbackReply": "f" * 600}, "兜底话术最长"),
    ],
)
def test_validation_rejects_bad_payload(payload: dict, fragment: str) -> None:
    result = agent_admin.create_agent(payload)
    assert result["ok"] is False
    assert fragment in result["error"], result["error"]


def test_validation_collects_all_errors() -> None:
    """多个问题应一次报全，而不是只报第一个。"""
    result = agent_admin.create_agent({"agentCode": "BAD", "agentName": ""})
    assert result["ok"] is False
    assert "助手编码只能" in result["error"] and "缺少助手名称" in result["error"]


def test_duplicate_code_conflicts() -> None:
    agent_admin.create_agent({"agentCode": "t-dup", "agentName": "第一次"})
    again = agent_admin.create_agent({"agentCode": "t-dup", "agentName": "第二次"})
    assert again["ok"] is False
    assert again["status"] == 409
    assert "已存在" in again["error"]


# ------------------------------------------------------------------ 3. 场景预设（M-A-01）
def test_six_scene_presets() -> None:
    presets = agent_admin.list_presets()["data"]["presets"]
    assert [p["sceneKey"] for p in presets] == [
        "finance",
        "admission",
        "logistics",
        "soe",
        "network",
        "orientation",
    ]
    assert all(p["agentCode"].startswith("scene-") for p in presets)
    assert all(p["systemPrompt"] for p in presets), "每个场景都应带角色化提示词"
    assert not any(p["created"] for p in presets)


def test_build_from_preset_then_reuse() -> None:
    first = agent_admin.create_from_preset("finance")
    assert first["ok"] and first["data"]["reused"] is False
    assert first["data"]["agentName"] == "财务助手"
    assert first["data"]["systemPrompt"]

    again = agent_admin.create_from_preset("finance")
    assert again["ok"] and again["data"]["reused"] is True
    assert again["data"]["agentId"] == first["data"]["agentId"], "不得建出重复行"

    presets = {p["sceneKey"]: p for p in agent_admin.list_presets()["data"]["presets"]}
    assert presets["finance"]["created"] is True
    assert presets["finance"]["agentId"] == first["data"]["agentId"]
    assert presets["network"]["created"] is False


def test_build_unknown_scene() -> None:
    result = agent_admin.create_from_preset("not-a-scene")
    assert result["ok"] is False and result["status"] == 404


# ------------------------------------------------------------------ 4. 基础配置七项（M-A-03）
def test_basic_config_seven_fields(custom_agent: dict) -> None:
    """名称/描述/头像/流式启停/启禁用/风控话术/兜底话术 全部可配置。"""
    aid = custom_agent["agentId"]
    result = agent_admin.update_agent(
        aid,
        {
            "agentName": "改名后的助手",
            "description": "新的描述",
            "iconUrl": "🦉",
            "streamEnabled": 0,
            "status": 0,
            "riskReply": "涉及敏感内容，请咨询相关部门。",
            "fallbackReply": "没找到，请换个说法。",
        },
    )
    assert result["ok"], result["error"]
    data = result["data"]
    assert data["agentName"] == "改名后的助手"
    assert data["description"] == "新的描述"
    assert data["iconUrl"] == "🦉"
    assert data["streamEnabled"] == 0
    assert data["status"] == 0
    assert data["riskReply"] == "涉及敏感内容，请咨询相关部门。"
    assert data["fallbackReply"] == "没找到，请换个说法。"

    # 落盘后再读一次，确认不是只改了返回值
    assert agent_store.get_agent(aid)["agentName"] == "改名后的助手"


def test_partial_update_keeps_other_fields(custom_agent: dict) -> None:
    aid = custom_agent["agentId"]
    agent_admin.update_agent(aid, {"description": "只改描述"})
    after = agent_store.get_agent(aid)
    assert after["description"] == "只改描述"
    assert after["agentName"] == custom_agent["agentName"]
    assert after["agentCode"] == custom_agent["agentCode"]


def test_update_rejects_unknown_field_only(custom_agent: dict) -> None:
    result = agent_admin.update_agent(custom_agent["agentId"], {"nope": 1})
    assert result["ok"] is False
    assert "没有可更新的字段" in result["error"]


def test_update_missing_agent() -> None:
    result = agent_admin.update_agent(987654, {"agentName": "X"})
    assert result["ok"] is False and result["status"] == 404


def test_update_code_conflict(custom_agent: dict, base_agent: dict) -> None:
    result = agent_admin.update_agent(
        custom_agent["agentId"], {"agentCode": base_agent["agentCode"]}
    )
    assert result["ok"] is False and result["status"] == 409


# ------------------------------------------------------------------ 5. 启停与删除
def test_set_status_toggles(custom_agent: dict) -> None:
    aid = custom_agent["agentId"]
    off = agent_admin.set_status(aid, 0)
    assert off["ok"] and off["data"]["statusName"] == "已下架"
    on = agent_admin.set_status(aid, 1)
    assert on["ok"] and on["data"]["statusName"] == "已上架"
    bad = agent_admin.set_status(aid, 2)
    assert bad["ok"] is False


def test_status_filter_in_list(custom_agent: dict) -> None:
    agent_admin.set_status(custom_agent["agentId"], 0)
    online = agent_admin.list_agents(status=1)["data"]
    offline = agent_admin.list_agents(status=0)["data"]
    assert all(a["status"] == 1 for a in online["items"])
    assert [a["agentId"] for a in offline["items"]] == [custom_agent["agentId"]]


def test_keyword_search(custom_agent: dict) -> None:
    hit = agent_admin.list_agents(keyword="测试助手")["data"]
    assert [a["agentId"] for a in hit["items"]] == [custom_agent["agentId"]]
    assert agent_admin.list_agents(keyword="绝不存在的词")["data"]["total"] == 0


def test_delete_agent(custom_agent: dict) -> None:
    aid = custom_agent["agentId"]
    result = agent_admin.delete_agent(aid)
    assert result["ok"] and result["data"]["deleted"] is True
    assert agent_store.get_agent(aid) is None


def test_delete_blocked_when_channel_bound(custom_agent: dict, monkeypatch) -> None:
    """被渠道绑定为主助手时拒绝删除（JSON 模式无渠道表，这里桩掉计数）。"""
    monkeypatch.setattr(agent_store, "count_channels_using", lambda _aid: 2)
    result = agent_admin.delete_agent(custom_agent["agentId"])
    assert result["ok"] is False and result["status"] == 409
    assert "2 个渠道" in result["error"]
    assert agent_store.get_agent(custom_agent["agentId"]) is not None, "拒绝后不得真的删掉"


# ------------------------------------------------------------------ 6. 接口层契约
def test_api_list_and_presets() -> None:
    resp = client.get("/api/admin/agents")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    data = body["data"]
    assert {"items", "total", "agentTypes", "presets"} <= set(data)
    assert data["agentTypes"] == list(agent_store.AGENT_TYPES)

    presets = client.get("/api/admin/agents/presets").json()["data"]["presets"]
    assert len(presets) == 6


def test_api_crud_roundtrip() -> None:
    created = client.post(
        "/api/admin/agents",
        json={
            "agentCode": "api-roundtrip",
            "agentName": "接口助手",
            "agentType": "guide",
            "description": "接口层创建",
            "visibleRoles": ["本科生"],
            "streamEnabled": 1,
            "fallbackReply": "兜底",
            "riskReply": "风控",
        },
    )
    assert created.status_code == 200, created.text
    aid = created.json()["data"]["agentId"]
    assert created.json()["data"]["agentType"] == "guide"

    detail = client.get(f"/api/admin/agents/{aid}")
    assert detail.status_code == 200 and detail.json()["data"]["agentName"] == "接口助手"

    patched = client.patch(f"/api/admin/agents/{aid}", json={"description": "改过"})
    assert patched.status_code == 200 and patched.json()["data"]["description"] == "改过"

    off = client.post(f"/api/admin/agents/{aid}/status", json={"status": 0})
    assert off.status_code == 200 and off.json()["data"]["status"] == 0

    deleted = client.delete(f"/api/admin/agents/{aid}")
    assert deleted.status_code == 200 and deleted.json()["data"]["deleted"] is True

    assert client.get(f"/api/admin/agents/{aid}").status_code == 404


def test_api_build_preset_endpoint() -> None:
    resp = client.post("/api/admin/agents/presets/orientation")
    assert resp.status_code == 200
    assert resp.json()["data"]["agentName"] == "迎新助手"
    # 再搭一次：复用，不报错
    again = client.post("/api/admin/agents/presets/orientation")
    assert again.status_code == 200 and again.json()["data"]["reused"] is True


def test_api_error_status_codes() -> None:
    assert client.get("/api/admin/agents/999999").status_code == 404
    assert client.post("/api/admin/agents/presets/nope").status_code == 404
    assert client.patch("/api/admin/agents/999999", json={"agentName": "X"}).status_code == 404
    assert client.post("/api/admin/agents", json={"agentCode": "x", "agentName": "y"}).status_code == 400
    # 请求体类型错误 → FastAPI 自身 422
    assert client.post("/api/admin/agents/1/status", json={"status": 9}).status_code == 422
    assert client.get("/api/admin/agents?status=7").status_code == 422


def test_api_presets_route_not_shadowed() -> None:
    """``/agents/presets`` 不能被 ``/agents/{agent_id}`` 吞掉（否则 422）。"""
    resp = client.get("/api/admin/agents/presets")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_operation_log_actions_declared() -> None:
    """任务 2.2 的动作码必须存在，便于 2.10 操作日志页筛选。"""
    from app.security import oplog

    for name in ("ACTION_AGENT_CREATE", "ACTION_AGENT_UPDATE", "ACTION_AGENT_STATUS", "ACTION_AGENT_DELETE"):
        assert getattr(oplog, name, None), f"缺少 {name}"


def test_operation_logs_endpoint_still_works_in_json_mode() -> None:
    """JSON 模式没有 operation_log 表，接口应返回空列表而不是 500。"""
    resp = client.get("/api/admin/operation-logs")
    assert resp.status_code == 200
    assert resp.json()["data"]["items"] == []
