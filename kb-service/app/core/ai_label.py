"""AI 生成内容标识（任务 3.7 / P-B-17）。

按《人工智能生成合成内容标识办法》做两层标识：

* **显式标识**：问答界面在每条 AI 回答下方展示「AI 生成」徽标与提示语；
* **隐式标识**：接口在响应体 ``data.aiLabel`` 与 HTTP 响应头里写入元数据。

```json
{"aigc": true, "label": "AI 生成", "text": "内容由 AI 生成，仅供参考",
 "producer": "能小智校园 AI 助手", "produceId": "S...:123",
 "standard": "《人工智能生成合成内容标识办法》", "version": 1, "model": "..."}
```

``produceId`` 由「会话 ID + 消息 ID」组成，可回溯到 ``chat_message`` 原始记录；
消息尚未落库时退化为会话 ID。响应头只放 ASCII，中文提示语只进响应体。
"""

from __future__ import annotations

from urllib.parse import quote

LABEL_SHORT = "AI 生成"
LABEL_TEXT = "内容由 AI 生成，仅供参考"
PRODUCER = "能小智校园 AI 助手"
STANDARD = "《人工智能生成合成内容标识办法》"
VERSION = 1


def produce_id(session_id: str | None, msg_id: int | None = None) -> str:
    if not session_id:
        return ""
    return f"{session_id}:{msg_id}" if msg_id is not None else session_id


def build_label(
    *, session_id: str | None, msg_id: int | None = None, model: str | None = None
) -> dict:
    """隐式标识元数据（camelCase，随响应体下发）。"""
    return {
        "aigc": True,
        "label": LABEL_SHORT,
        "text": LABEL_TEXT,
        "producer": PRODUCER,
        "produceId": produce_id(session_id, msg_id),
        "standard": STANDARD,
        "version": VERSION,
        "model": model or "",
    }


def _ascii(value: str) -> str:
    """Header 值必须是 latin-1；会话 ID 来自客户端，非 ASCII 需百分号编码。"""
    return quote(value, safe=":._-")


def headers(session_id: str | None) -> dict[str, str]:
    """响应头隐式标识（仅 ASCII，避免 header 编码问题）。"""
    return {
        "X-AI-Generated": "true",
        "X-AI-Label": "AI-generated",
        "X-AI-Label-Version": str(VERSION),
        "X-AI-Produce-Id": _ascii(produce_id(session_id)),
    }
