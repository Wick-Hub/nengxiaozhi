"""API 契约测试（任务 1.15 接线）。

覆盖：健康检查计数、文档列表/切片、会话列表与消息、写操作的存储模式门禁、
问答成功/异常降级、SSE 事件序列。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.index_store import IndexSnapshot
from app.db import chatlog
from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _isolate_sessions(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(chatlog, "SESSIONS_PATH", tmp_path / "sessions.json")
    yield


# ------------------------------------------------------------------ 健康
def test_health_reports_index_counts() -> None:
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["store"] == "json"
    assert body["llm"] == "mock"
    assert body["docs"] == 7
    assert body["chunks"] == 35
    assert body["embed_backend"] in {"onnx", "hash", "not_loaded"}
    assert isinstance(body["embed_is_fallback"], bool)


# ------------------------------------------------------------------ 文档
def test_docs_list() -> None:
    body = client.get("/api/docs").json()
    assert body["ok"] is True
    docs = body["data"]
    assert len(docs) == 7
    assert all("docId" in d for d in docs)


def test_docs_list_filter_by_dept() -> None:
    all_docs = client.get("/api/docs").json()["data"]
    dept = all_docs[0].get("dept")
    if not dept:
        pytest.skip("索引中无 dept 字段")
    filtered = client.get("/api/docs", params={"dept": dept}).json()["data"]
    assert filtered
    assert all(d.get("dept") == dept for d in filtered)


def test_doc_chunks() -> None:
    docs = client.get("/api/docs").json()["data"]
    doc_id = docs[0]["docId"]
    body = client.get(f"/api/docs/{doc_id}/chunks").json()
    assert body["ok"] is True
    chunks = body["data"]
    assert chunks
    assert all(c["docId"] == doc_id for c in chunks)
    assert all(c["chunkId"] for c in chunks)


def test_doc_upload_requires_mysql_in_json_mode() -> None:
    """JSON 模式下写操作要给出可执行的提示，而不是静默失败或 501。"""
    resp = client.post("/api/docs", json={"filename": "a.md", "content_base64": "eA=="})
    assert resp.status_code == 400
    assert "KB_STORE=mysql" in resp.json()["detail"]


# ------------------------------------------------------------------ 问答
def test_chat_success_shape() -> None:
    """非流式问答：{ok, data} 信封 + camelCase，字段与原型一致。"""
    body = client.post(
        "/api/chat", json={"question": "校园网怎么开通", "role": "本科生"}
    ).json()
    assert body["ok"] is True
    data = body["data"]
    assert data["answer"]
    assert data["sessionId"].startswith("S")
    assert data["msgId"] >= 1
    assert data["refused"] is False
    assert data["retrieved"] > 0
    assert data["citations"]
    c = data["citations"][0]
    # 引用字段为 camelCase，且含原始编号 n
    assert set(["n", "chunkId"]).issubset(c.keys())
    assert "title_path" not in c


def test_chat_accepts_session_id_alias() -> None:
    """sessionId（原型前端写法）与 session_id 两种入参都应被接受。"""
    r1 = client.post("/api/chat", json={"question": "校园网怎么开通", "role": "本科生"}).json()
    sid = r1["data"]["sessionId"]
    r2 = client.post(
        "/api/chat", json={"question": "需要多少钱", "role": "本科生", "session_id": sid}
    ).json()
    assert r2["data"]["sessionId"] == sid


def test_chat_validates_empty_question() -> None:
    resp = client.post("/api/chat", json={"question": ""})
    assert resp.status_code == 422


def test_chat_rejects_overlong_question() -> None:
    resp = client.post("/api/chat", json={"question": "问" * 501})
    assert resp.status_code == 422


def test_chat_multi_turn_via_api() -> None:
    r1 = client.post("/api/chat", json={"question": "校园网怎么开通", "role": "本科生"}).json()
    sid = r1["data"]["sessionId"]
    client.post("/api/chat", json={"question": "需要多少钱", "role": "本科生", "sessionId": sid})

    body = client.get(f"/api/sessions/{sid}/messages").json()
    assert body["ok"] is True
    # data 为消息数组（原型契约）
    assert isinstance(body["data"], list)
    assert len(body["data"]) == 4


def test_chat_returns_503_without_llm(monkeypatch) -> None:
    """LLM 未配置时应 503（原型 llmAvailable 行为），而非 200/500。"""
    import app.api.chat as chat_mod

    monkeypatch.setattr(chat_mod, "llm_ready", lambda: False)
    resp = client.post("/api/chat", json={"question": "校园网怎么开通"})
    assert resp.status_code == 503
    body = resp.json()
    assert body["ok"] is False
    assert "LLM_API_KEY" in body["error"]


# ------------------------------------------------------------------ SSE
def test_chat_stream_event_sequence() -> None:
    with client.stream(
        "POST", "/api/chat/stream", json={"question": "校园网怎么开通", "role": "本科生"}
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        raw = "".join(resp.iter_text())

    events = [
        json.loads(line[len("data: ") :])
        for line in raw.splitlines()
        if line.startswith("data: ")
    ]
    assert events, "SSE 应至少有一个事件"
    assert events[0]["type"] == "delta"
    # 增量字段名 text（原型契约，前端 app.js 直接消费）
    assert events[0]["text"]
    assert "content" not in events[0]
    done = events[-1]
    assert done["type"] == "done"
    assert done["refused"] is False
    assert done["answer"]
    assert done["citations"]
    assert done["sessionId"].startswith("S")
    assert done["msgId"] >= 1
    assert "latencyMs" in done
    assert all(e["session_id"] == events[0]["session_id"] for e in events)


def test_chat_stream_503_without_llm(monkeypatch) -> None:
    """未配置 LLM 时 SSE 接口应在进入流之前返回 503 JSON。"""
    import app.api.chat as chat_mod

    monkeypatch.setattr(chat_mod, "llm_ready", lambda: False)
    resp = client.post("/api/chat/stream", json={"question": "校园网怎么开通"})
    assert resp.status_code == 503
    assert resp.json()["ok"] is False


def test_chat_stream_error_event(monkeypatch) -> None:
    import app.api.chat as chat_mod

    def boom(*a, **kw):
        raise RuntimeError("模拟生成失败")

    monkeypatch.setattr(chat_mod, "answer_question_stream", boom)
    with client.stream("POST", "/api/chat/stream", json={"question": "x"}) as resp:
        raw = "".join(resp.iter_text())
    # 错误事件字段名为 error（原型契约）
    assert '"type": "error"' in raw
    assert '"error"' in raw


# ------------------------------------------------------------------ 会话 & 同步
def test_sessions_list_after_chat() -> None:
    client.post("/api/chat", json={"question": "校园网怎么开通", "role": "本科生"})
    body = client.get("/api/sessions").json()
    assert body["ok"] is True
    assert isinstance(body["data"], list)
    assert body["data"], "问答后应至少有一个会话"
    assert "lastQuestion" in body["data"][0]


def test_session_messages_unknown_id() -> None:
    """会话不存在时返回 404 + ok=false（原型契约）。"""
    resp = client.get("/api/sessions/S_NOT_EXIST/messages")
    assert resp.status_code == 404
    body = resp.json()
    assert body["ok"] is False
    assert body["error"] == "会话不存在"


def test_session_messages_shape() -> None:
    r1 = client.post("/api/chat", json={"question": "校园网怎么开通", "role": "本科生"}).json()
    sid = r1["data"]["sessionId"]
    msgs = client.get(f"/api/sessions/{sid}/messages").json()["data"]
    assert [m["role"] for m in msgs] == ["user", "ai"]
    assert all("createdTime" in m for m in msgs)
    assert msgs[1]["citations"]


def test_sync_requires_mysql_in_json_mode() -> None:
    """JSON 模式下增量同步要给出可执行的提示。"""
    resp = client.post("/api/sync")
    assert resp.status_code == 400
    assert "KB_STORE=mysql" in resp.json()["detail"]


# ------------------------------------------------------------------ 索引不可用
def test_chat_503_when_index_unavailable(monkeypatch) -> None:
    """索引不可用（如 MySQL 未启动）必须 503 明示，而不是伪装成拒答。"""
    from app.api import chat as chat_mod

    monkeypatch.setattr(
        chat_mod, "get_index", lambda: IndexSnapshot(error="MySQL 不可用：连接被拒绝")
    )

    r = client.post("/api/chat", json={"question": "校园网怎么开户", "role": "本科生"})
    assert r.status_code == 503
    body = r.json()
    assert body["ok"] is False
    assert "知识库不可用" in body["error"]
    assert "MySQL 不可用" in body["error"]

    r = client.post("/api/chat/stream", json={"question": "校园网怎么开户"})
    assert r.status_code == 503
    assert "知识库不可用" in r.json()["error"]
