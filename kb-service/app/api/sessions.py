"""会话历史接口。

契约与原型 ``server.js`` 的 ``handleHistory`` 对齐（前端 ``app.js`` 直接消费）：

    GET /api/sessions/{id}/messages
        200 -> {"ok": true, "data": [ {msgId, role:'user'|'ai', content,
                                      confidence, createdTime, citations}, ... ]}
        404 -> {"ok": false, "data": null, "error": "会话不存在"}

注意 ``data`` 是**消息数组**（而非分页对象），前端直接 ``for (const m of json.data)``。
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.core import ai_label
from app.core.richtext import parse_blocks, render_html
from app.db import chatlog
from app.models.schemas import ApiResult

router = APIRouter(prefix="/sessions")


@router.get("", response_model=ApiResult, summary="会话列表")
def list_sessions(limit: int = 50) -> ApiResult:
    try:
        return ApiResult(ok=True, data=chatlog.list_sessions(limit))
    except NotImplementedError as exc:
        return ApiResult(ok=False, data=[], error=str(exc))


def _to_message(m: dict, session_id: str) -> dict:
    """存储消息 → 前端消息契约（createdAt → createdTime）。

    ``ai`` 消息附带 ``blocks`` / ``answerHtml``（任务 1.9），使历史会话恢复时
    与实时回答走**同一套渲染结果**，不会出现刷新前后样式不一致。
    ``ai`` 消息同时携带 AI 标识元数据（任务 3.7），历史记录与实时回答口径一致。
    """
    content = m.get("content", "") or ""
    is_ai = m.get("role") == "ai"
    return {
        "msgId": m.get("msgId"),
        "role": m.get("role"),
        "content": content,
        "confidence": m.get("confidence"),
        "refused": bool(m.get("refused", False)),
        "createdTime": m.get("createdAt"),
        "citations": m.get("citations") or [],
        "blocks": parse_blocks(content) if is_ai else [],
        "answerHtml": render_html(content) if is_ai else None,
        "aiGenerated": is_ai,
        "aiLabel": (
            ai_label.build_label(
                session_id=session_id, msg_id=m.get("msgId"), model=m.get("model")
            )
            if is_ai
            else None
        ),
    }


@router.get("/{session_id}/messages", summary="历史会话消息")
def list_messages(session_id: str):
    """读取会话全部消息（含每条 AI 消息的引用），供前端恢复会话。"""
    try:
        sess = chatlog.load_session(session_id)
    except NotImplementedError as exc:
        return JSONResponse(status_code=200, content={"ok": False, "data": None, "error": str(exc)})

    if not sess:
        return JSONResponse(
            status_code=404, content={"ok": False, "data": None, "error": "会话不存在"}
        )

    return ApiResult(
        ok=True, data=[_to_message(m, session_id) for m in sess.get("messages", [])]
    )
