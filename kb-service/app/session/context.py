"""会话上下文组装（任务 1.8：多轮对话 ≥10 轮）。

把「读取会话消息 → 规范化 → 组装为 LLM 上下文」从 ``pipeline`` / ``chatlog`` 中抽离，
集中处理三件事：

1. **窗口大小**：默认取最近 ``settings.history_max_messages`` 条（默认 20 条 ≈ 10 轮）。
   原型 ``qa.js`` 的 ``getHistory(limit = 8)`` 只有 4 轮，任务 1.8 要求提升到
   「至少 20 条消息」。
2. **配对完整**：截断后若首条是 ``assistant``，直接丢弃。OpenAI 兼容接口
   （含 DeepSeek）要求 ``system`` 之后的首条消息必须是 ``user``，否则部分厂商直接 400。
3. **角色归一**：存储层用 ``ai``，LLM 协议用 ``assistant``。

跨会话隔离由 ``session_id`` 保证：不同会话各自读取，互不可见。

指代消解 / 查询改写为后续任务预留，见 :func:`rewrite_query`（当前恒等返回）。
"""

from __future__ import annotations

from app.config import settings
from app.db import chatlog

#: 任务 1.8 的下限：≥10 轮（每轮 user + assistant 共 2 条）
MIN_TURNS = 10


def normalize_history(
    messages: list[dict],
    limit: int | None = None,
    ensure_user_first: bool = True,
) -> list[dict]:
    """把存储层消息规范化为 LLM 上下文（纯函数，便于单测）。

    - ``ai → assistant``，其余（含 ``user`` / ``system``）→ ``user``
    - 丢弃内容为空的条目
    - ``limit`` 只保留尾部 N 条
    - ``ensure_user_first`` 时丢弃开头的 ``assistant``，保证配对从 ``user`` 起
    """
    out: list[dict] = []
    for m in messages:
        content = (m.get("content") or "").strip()
        if not content:
            continue
        role = "assistant" if m.get("role") == "ai" else "user"
        out.append({"role": role, "content": content})

    if limit and limit > 0:
        out = out[-limit:]

    if ensure_user_first:
        while out and out[0]["role"] != "user":
            out.pop(0)

    return out


def load_context(session_id: str | None, limit: int | None = None) -> list[dict]:
    """读取会话最近若干条消息，规范化为可喂给大模型的多轮上下文。

    会话不存在或 ``session_id`` 为空时返回 ``[]``（首轮对话无历史）。
    """
    if not session_id:
        return []
    limit = limit or settings.history_max_messages
    sess = chatlog.load_session(session_id)
    if not sess:
        return []
    return normalize_history(sess.get("messages") or [], limit=limit)


def history_stats(context: list[dict]) -> dict:
    """上下文统计（诊断 / 验收用）：消息条数与轮次数。"""
    turns = sum(1 for m in context if m["role"] == "user")
    return {"messages": len(context), "turns": turns}


def rewrite_query(question: str, context: list[dict] | None = None) -> str:
    """查询改写 / 指代消解钩子（任务 1.8 预留，当前恒等）。

    后续可接入轻量改写：把「它 / 这个 / 那怎么办」等指代补全为完整问句，
    以提升多轮场景下的召回率。此处保持恒等，不改变现有行为。
    """
    return question
