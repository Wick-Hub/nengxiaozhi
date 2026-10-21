"""AI 生成内容标识（任务 3.7 / P-B-17）测试。

两层标识都要可验证：

* **显式**：问答界面由前端渲染「AI 生成」徽标（DOM 守卫在 ``test_frontend.py``
  之外单列断言，见文件末尾）；
* **隐式**：``/api/chat`` 与 ``/api/chat/stream`` 的响应体 ``aiLabel``、
  历史消息字段，以及 HTTP 响应头。
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.core import ai_label
from app.main import app

client = TestClient(app)
STATIC = Path(__file__).resolve().parent.parent / "static"


def _done_event(resp) -> dict:
    events = []
    for line in resp.text.splitlines():
        if line.startswith("data:"):
            events.append(json.loads(line[5:].strip()))
    done = [e for e in events if e.get("type") == "done"]
    assert done, f"没有 done 事件：{events[:3]}"
    return done[0]


# ------------------------------------------------------------------ 元数据构造
def test_build_label_contains_standard_fields() -> None:
    label = ai_label.build_label(session_id="SABC", msg_id=7, model="deepseek-chat")
    assert label["aigc"] is True
    assert label["label"] == "AI 生成"
    assert label["text"] == "内容由 AI 生成，仅供参考"
    assert label["producer"] == "能小智校园 AI 助手"
    assert label["produceId"] == "SABC:7"
    assert label["model"] == "deepseek-chat"
    assert "标识办法" in label["standard"]


def test_produce_id_degrades_to_session_before_persist() -> None:
    assert ai_label.produce_id("SABC") == "SABC"
    assert ai_label.produce_id("SABC", None) == "SABC"
    assert ai_label.produce_id(None) == ""
    assert ai_label.produce_id(None, 3) == ""


def test_headers_are_ascii() -> None:
    headers = ai_label.headers("SABC")
    assert headers["X-AI-Generated"] == "true"
    assert headers["X-AI-Produce-Id"] == "SABC"
    for value in headers.values():
        value.encode("ascii")  # 不抛即通过


def test_headers_percent_encode_non_ascii_session_id() -> None:
    """客户端会话 ID 可含中文；响应头必须仍是 ASCII，且可逆。"""
    from urllib.parse import unquote

    headers = ai_label.headers("会话-01:平板")
    value = headers["X-AI-Produce-Id"]
    value.encode("ascii")
    assert unquote(value) == "会话-01:平板"


# ------------------------------------------------------------------ 非流式
def test_chat_response_has_explicit_and_implicit_label() -> None:
    resp = client.post("/api/chat", json={"question": "校园网怎么开通", "role": "本科生"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["aiGenerated"] is True
    label = data["aiLabel"]
    assert label["label"] == "AI 生成"
    assert label["produceId"] == f"{data['sessionId']}:{data['msgId']}"

    assert resp.headers["x-ai-generated"] == "true"
    assert resp.headers["x-ai-label"] == "AI-generated"
    assert resp.headers["x-ai-produce-id"] == data["sessionId"]


# ------------------------------------------------------------------ 流式
def test_stream_done_event_has_label() -> None:
    resp = client.post(
        "/api/chat/stream", json={"question": "校园网怎么开通", "role": "本科生"}
    )
    assert resp.status_code == 200
    assert resp.headers["x-ai-generated"] == "true"
    done = _done_event(resp)
    assert done["aiGenerated"] is True
    assert done["aiLabel"]["produceId"] == f"{done['sessionId']}:{done['msgId']}"
    assert done["aiLabel"]["text"] == "内容由 AI 生成，仅供参考"


# ------------------------------------------------------------------ 历史消息
def test_history_marks_ai_messages_only() -> None:
    first = client.post(
        "/api/chat", json={"question": "校园网怎么开通", "role": "本科生"}
    ).json()["data"]
    sid = first["sessionId"]

    items = client.get(f"/api/sessions/{sid}/messages").json()["data"]
    users = [m for m in items if m["role"] == "user"]
    ais = [m for m in items if m["role"] == "ai"]
    assert users and ais
    assert all(m["aiGenerated"] is False and m["aiLabel"] is None for m in users)
    assert all(m["aiGenerated"] is True for m in ais)
    assert all(m["aiLabel"]["produceId"].startswith(f"{sid}:") for m in ais)


# ------------------------------------------------------------------ 前端显式标识
def test_frontend_renders_ai_label() -> None:
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "function addAiLabel" in js
    assert "AI 生成" in js
    assert "内容由 AI 生成，仅供参考" in js
    # 实时回答、拒答、历史三条路径都要挂标识
    assert js.count("addAiLabel(") >= 4  # 定义 1 + 调用 3
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert ".ai-label" in css and ".ai-badge" in css
