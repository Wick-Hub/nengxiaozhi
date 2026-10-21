"""多智能体切换测试（任务 3.8 / P-QA-06）。

覆盖四层：

1. **存储层**：助手场景引导问题的归一（去空白/去重/截断）与持久化；
2. **服务层**：校验口径（≤6 条、单条 ≤120 字）、场景预设携带引导问题；
3. **公开助手矩阵**：按身份过滤、下架隐藏、默认助手置顶、不泄漏配置字段；
4. **渠道/应用联动**：渠道公开卡片按身份过滤并携带引导问题；应用广场
   的智能体应用下发同一组引导问题，供切换时同步首屏推荐。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import agent_store, app_store
from app.main import app
from app.services import agent_admin

client = TestClient(app)


def _create(code: str, name: str, roles=None, guides=None) -> dict:
    payload = {
        "agentCode": code,
        "agentName": name,
        "agentType": "chat",
        "description": f"{name}的描述",
        "visibleRoles": roles or ["全部"],
    }
    if guides is not None:
        payload["guideQuestions"] = guides
    result = agent_admin.create_agent(payload)
    assert result["ok"], result["error"]
    return result["data"]


# ------------------------------------------------------------------ 1. 存储层
def test_guide_questions_normalized_and_persisted() -> None:
    created = _create(
        "g-normalize",
        "引导问题助手",
        guides=["  学费怎么交？  ", "学费怎么交？", "", "报销要什么材料？"],
    )
    assert created["guideQuestions"] == ["学费怎么交？", "报销要什么材料？"]
    # 重新读盘后仍是数组，且已去重
    reloaded = agent_store.get_agent(created["agentId"])
    assert reloaded["guideQuestions"] == ["学费怎么交？", "报销要什么材料？"]


def test_guide_questions_default_empty() -> None:
    base = agent_store.list_agents()[0]
    assert base["guideQuestions"] == []
    created = _create("g-empty", "无引导")
    assert created["guideQuestions"] == []


def test_guide_questions_truncated_on_read() -> None:
    # 直接写坏数据（模拟历史脏记录），读路径应截断而不是让门户报错
    created = _create("g-dirty", "脏数据")
    agent_store.update_agent(
        created["agentId"],
        {"guideQuestions": ["长" * 130] + [f"问题{i}" for i in range(9)]},
    )
    got = agent_store.get_agent(created["agentId"])
    assert len(got["guideQuestions"]) == agent_store.MAX_GUIDE_QUESTIONS
    assert all(len(q) <= agent_store.MAX_GUIDE_LENGTH for q in got["guideQuestions"])


# ------------------------------------------------------------------ 2. 服务层校验
@pytest.mark.parametrize(
    ("guides", "fragment"),
    [
        ([f"问题{i}" for i in range(7)], "最多 6 条"),
        (["长" * 121], "单条最长 120 字"),
    ],
)
def test_guide_questions_validation_errors(guides: list[str], fragment: str) -> None:
    result = agent_admin.create_agent(
        {"agentCode": "g-bad", "agentName": "错误", "guideQuestions": guides}
    )
    assert result["ok"] is False
    assert fragment in result["error"]


def test_guide_questions_non_list_rejected_by_service() -> None:
    errors = agent_store.validate_guide_questions({"q": "x"})
    assert errors and "字符串数组" in errors[0]


def test_update_can_replace_and_clear_guide_questions() -> None:
    created = _create("g-update", "待更新", guides=["旧问题"])
    updated = agent_admin.update_agent(
        created["agentId"], {"guideQuestions": ["新问题一", "新问题二"]}
    )
    assert updated["ok"] is True
    assert updated["data"]["guideQuestions"] == ["新问题一", "新问题二"]

    cleared = agent_admin.update_agent(created["agentId"], {"guideQuestions": []})
    assert cleared["ok"] is True
    assert cleared["data"]["guideQuestions"] == []


def test_scene_preset_carries_guide_questions() -> None:
    result = agent_admin.create_from_preset("finance")
    assert result["ok"], result["error"]
    agent = result["data"]
    # 预设已带场景引导问题；重复搭建复用同一行
    assert len(agent["guideQuestions"]) >= 3
    again = agent_admin.create_from_preset("finance")
    assert again["data"]["agentId"] == agent["agentId"]
    assert again["data"]["guideQuestions"] == agent["guideQuestions"]


def test_admin_api_roundtrip_guide_questions() -> None:
    resp = client.post(
        "/api/admin/agents",
        json={
            "agentCode": "g-api",
            "agentName": "接口助手",
            "guideQuestions": ["问题一", "问题二"],
        },
    )
    assert resp.status_code == 200, resp.text
    item = resp.json()["data"]
    assert item["guideQuestions"] == ["问题一", "问题二"]

    patched = client.patch(
        f"/api/admin/agents/{item['agentId']}", json={"guideQuestions": ["问题三"]}
    )
    assert patched.status_code == 200
    assert patched.json()["data"]["guideQuestions"] == ["问题三"]

    detail = client.get(f"/api/admin/agents/{item['agentId']}").json()["data"]
    assert detail["guideQuestions"] == ["问题三"]


# ------------------------------------------------------------------ 3. 公开助手矩阵
def test_public_agents_filter_by_role_and_visibility() -> None:
    _create("pub-teacher", "教职工专属", roles=["教职工"], guides=["资产报废怎么办？"])
    _create("pub-all", "全员助手", guides=["校园网怎么开通？"])

    data = client.get("/api/agents?role=教职工").json()["data"]
    codes = {a["agentCode"] for a in data["agents"]}
    assert {"pub-teacher", "pub-all"} <= codes
    assert data["role"] == "教职工"

    data = client.get("/api/agents?role=访客").json()["data"]
    codes = {a["agentCode"] for a in data["agents"]}
    assert "pub-all" in codes
    assert "pub-teacher" not in codes


def test_public_agents_unknown_role_falls_back_to_guest() -> None:
    _create("pub-only-teacher", "仅教职工", roles=["教职工"])
    data = client.get("/api/agents?role=外星人").json()["data"]
    assert data["role"] == "访客"
    assert "pub-only-teacher" not in {a["agentCode"] for a in data["agents"]}


def test_public_agents_hide_off_shelf_and_default_first() -> None:
    created = _create("pub-off", "下架助手", guides=["不应出现"])
    agent_admin.set_status(created["agentId"], 0)
    _create("pub-on", "上架助手", guides=["应该出现"])

    data = client.get("/api/agents?role=访客").json()["data"]
    names = [a["agentCode"] for a in data["agents"]]
    assert "pub-off" not in names
    assert names[0] == agent_store.DEFAULT_AGENT_CODE
    assert data["defaultAgentId"] == data["agents"][0]["agentId"]


def test_public_agents_do_not_leak_config_fields() -> None:
    _create("pub-secret", "配置助手", guides=["问题"])
    item = next(
        a
        for a in client.get("/api/agents?role=访客").json()["data"]["agents"]
        if a["agentCode"] == "pub-secret"
    )
    assert item["guideQuestions"] == ["问题"]
    assert "systemPrompt" not in item
    assert "retrievalConfig" not in item
    assert "modelCode" not in item


# ------------------------------------------------------------------ 4. 渠道 / 应用联动
@pytest.fixture()
def channel_with_two_agents() -> dict:
    base = agent_store.list_agents()[0]
    teacher = _create("ch-teacher", "渠道教职工助手", roles=["教职工"], guides=["采购限额是多少？"])
    resp = client.post(
        "/api/admin/channels",
        json={
            "channelCode": "vf-switch",
            "channelName": "切换验收渠道",
            "defaultAgentId": base["agentId"],
            "agentIds": [base["agentId"], teacher["agentId"]],
            "needAuth": 0,
            "status": 1,
        },
    )
    assert resp.status_code == 200, resp.text
    return {"base": base, "teacher": teacher, "channel": resp.json()["data"]}


def test_channel_public_cards_role_filtered_and_carry_guides(
    channel_with_two_agents: dict,
) -> None:
    data = client.get("/api/channels/vf-switch?role=教职工").json()["data"]
    assert {a["agentCode"] for a in data["agents"]} == {
        agent_store.DEFAULT_AGENT_CODE,
        "ch-teacher",
    }
    teacher = next(a for a in data["agents"] if a["agentCode"] == "ch-teacher")
    assert teacher["guideQuestions"] == ["采购限额是多少？"]

    guest = client.get("/api/channels/vf-switch?role=访客").json()["data"]
    assert {a["agentCode"] for a in guest["agents"]} == {agent_store.DEFAULT_AGENT_CODE}


def test_channel_hidden_route_agent_falls_back() -> None:
    teacher = _create("ch-hidden", "教职工路由助手", roles=["教职工"], guides=["流程是什么？"])
    base = agent_store.list_agents()[0]
    created = client.post(
        "/api/admin/channels",
        json={
            "channelCode": "vf-hidden",
            "channelName": "隐藏路由渠道",
            "defaultAgentId": base["agentId"],
            "agentIds": [base["agentId"], teacher["agentId"]],
            "needAuth": 0,
            "status": 1,
            "routeConfig": {
                "routes": [
                    {"role": "教职工", "agentId": teacher["agentId"]},
                ]
            },
        },
    )
    assert created.status_code == 200, created.text

    # 访客看不到教职工专属路由助手，公开端回退到默认助手
    data = client.get("/api/channels/vf-hidden?role=访客").json()["data"]
    assert data["defaultAgentId"] == base["agentId"]
    assert all(a["agentId"] != teacher["agentId"] for a in data["agents"])

    data = client.get("/api/channels/vf-hidden?role=教职工").json()["data"]
    assert data["defaultAgentId"] == teacher["agentId"]


def test_public_app_agent_carries_guide_questions() -> None:
    agent = _create("app-guides", "广场助手", guides=["广场问题一", "广场问题二"])
    app_store.create_app(
        app_name="广场入口",
        source_type="agent",
        agent_id=agent["agentId"],
        visible_roles=["全部"],
        status=1,
    )
    apps = client.get("/api/apps?role=访客").json()["data"]["apps"]
    hit = next(a for a in apps if a["agentId"] == agent["agentId"])
    assert hit["guideQuestions"] == ["广场问题一", "广场问题二"]
