"""会话路由端到端测试（任务 3.4 / M-C-07、P-B-08）。

验收口径：**同一问题在不同角色 / 不同渠道下得到不同回答**。
``LLM_MOCK`` 的正文是固定占位，因此以「引用切片集合」作为回答差异的可观测证据：
角色过滤与助手知识范围都会改变检索结果，引用不同即代表回答依据不同。

覆盖：

1. 同一问题「校园网怎么开通」按访客/教职工检索，引用集合不同，且各自不越权；
2. 渠道角色路由把教职工切到「财务处」助手后，同一问题引用来自助手知识范围，
   与访客（信息网络中心）的引用集合不同。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.index_store import get_index
from app.db import agent_store
from app.main import app

client = TestClient(app)


def _chat(question: str, role: str, agent_id: int | None = None, session_id: str = "") -> dict:
    resp = client.post(
        "/api/chat",
        json={
            "question": question,
            "role": role,
            "agentId": agent_id,
            "sessionId": session_id or f"vf-route-{role}-{question}",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True, body
    return body["data"]


def _citation_ids(data: dict) -> set[str]:
    return {str(c["chunkId"]) for c in data["citations"]}


def test_same_question_different_roles_have_different_citations() -> None:
    # 用「差旅费」而非「校园网」：后者的示例切片全部是「全部」可见，
    # 按真实语义检索后不同角色自然拿到同一批公共切片，测不出角色过滤。
    question = "差旅费报销标准和流程"
    guest = _chat(question, "访客", session_id="vf-role-guest")
    staff = _chat(question, "教职工", session_id="vf-role-staff")

    guest_ids = _citation_ids(guest)
    staff_ids = _citation_ids(staff)
    assert guest_ids and staff_ids
    assert guest_ids != staff_ids, "不同身份的引用集合应不同（角色过滤生效）"
    assert all(cid.startswith("D002-") for cid in staff_ids), "教职工应命中财务处差旅费切片"
    assert not any(cid.startswith("D002-") for cid in guest_ids), "访客不应看到教职工报销切片"

    visible = {c.chunk_id: set(c.visible_roles or ["全部"]) for c in get_index().chunks}
    assert all(
        "全部" in visible[cid] or "访客" in visible[cid] for cid in guest_ids
    ), "访客看到了越权切片"
    assert all(
        "全部" in visible[cid] or "教职工" in visible[cid] for cid in staff_ids
    ), "教职工看到了越权切片"


def test_channel_route_switches_agent_and_knowledge_scope() -> None:
    net = agent_store.create_agent(
        {
            "agentCode": "vf-route-net",
            "agentName": "网络中心助手",
            "retrievalConfig": {"depts": ["信息网络中心"]},
        }
    )
    fin = agent_store.create_agent(
        {
            "agentCode": "vf-route-fin",
            "agentName": "财务处助手",
            "retrievalConfig": {"depts": ["财务处"]},
        }
    )
    created = client.post(
        "/api/admin/channels",
        json={
            "channelCode": "vf-route-chat",
            "channelName": "路由对话渠道",
            "defaultAgentId": net["agentId"],
            "agentIds": [net["agentId"], fin["agentId"]],
            "routeConfig": {
                "routes": [
                    {"role": "教职工", "agentId": fin["agentId"]},
                ]
            },
            "needAuth": 0,
            "status": 1,
        },
    )
    assert created.status_code == 200, created.text

    guest_cfg = client.get("/api/channels/vf-route-chat?role=访客").json()["data"]
    staff_cfg = client.get("/api/channels/vf-route-chat?role=教职工").json()["data"]
    assert guest_cfg["defaultAgentId"] == net["agentId"]
    assert staff_cfg["defaultAgentId"] == fin["agentId"]

    guest = _chat(
        "校园网怎么开通",
        "访客",
        guest_cfg["defaultAgentId"],
        session_id="vf-channel-guest",
    )
    staff = _chat(
        "校园网怎么开通",
        "教职工",
        staff_cfg["defaultAgentId"],
        session_id="vf-channel-staff",
    )
    guest_ids = _citation_ids(guest)
    staff_ids = _citation_ids(staff)
    assert guest_ids != staff_ids, "同一问题经不同渠道路由后应给出不同依据"
    assert {c["dept"] for c in guest["citations"]} == {"信息网络中心"}
    assert {c["dept"] for c in staff["citations"]} == {"财务处"}
