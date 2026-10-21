"""会话日志查询与导出（任务 2.1）。

管理端「会话日志」页的后端：两种视图 + 筛选 + 检索 + CSV 导出。

- **问答视图**：一行 = 一问一答（问题/回答/角色/时间/置信度/引用数/模型/token）。
- **咨询用户视图**：一行 = 一个会话（问数/拒答数/最后提问/末次时间/token 合计）。

双后端语义一致：``KB_STORE=json`` 读 ``data/sessions.json``；
``KB_STORE=mysql`` 查 ``chat_session`` / ``chat_message`` / ``answer_citation`` /
``token_usage``。两条分支最终都归一到同一份「消息行」结构，再共用同一套
配对 / 筛选 / 排序 / 分页逻辑——**分页与导出走同一条 ``_collect`` 路径**，
保证「导出结果 = 页面所见」的语义不漂移。

**已知边界（如实声明，勿当已完成）**

1. 两种后端都只在**最近 ``SCAN_LIMIT`` 条消息**内配对、检索与分页。
   管理端日志是「最近记录」视图；需要更早记录时用时间范围把窗口收窄
   （MySQL 分支的会话级筛选 role/agent/channel 与时间范围**已下推到 SQL**，
   窗口只覆盖相关消息）。响应里的 ``scanned`` / ``scanLimit`` 把这条边界
   显式暴露给前端。
2. JSON 模式**没有** agent / channel / model / token 落库（原型 ``chatlog.js``
   的 JSON 分支不写这些字段），对应字段返回 ``None``，前端显示「—」；
   **不用假数据填充**。
3. 问答配对规则：AI 消息向前找**同会话中 ``msg_id`` 最大的用户消息**。
   与 :func:`app.db.chatlog.save_qa` 的「用户消息 + AI 消息」连续写入一致。
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, time

from sqlalchemy import desc, func, select

from app.config import settings
from app.db import models as M
from app.db.session import db_session

logger = logging.getLogger(__name__)

ROLE_USER = 1
ROLE_AI = 2

#: 单次查询最多扫描的消息条数（两种后端一致）。
SCAN_LIMIT = 2000
#: CSV 导出最多输出的行数（避免一次导出把内存打满）。
EXPORT_LIMIT = 5000
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 200

#: 问答视图导出列
QA_CSV_COLUMNS = [
    ("会话ID", lambda r: r.get("sessionId")),
    ("提问时间", lambda r: r.get("askedAt")),
    ("回答时间", lambda r: r.get("answeredAt")),
    ("咨询用户", lambda r: r.get("role")),
    ("智能体", lambda r: r.get("agentName")),
    ("渠道", lambda r: r.get("channelName")),
    ("问题", lambda r: r.get("question")),
    ("回答", lambda r: r.get("answer")),
    ("置信度", lambda r: r.get("confidence")),
    ("是否拒答", lambda r: "是" if r.get("refused") else "否"),
    ("引用数", lambda r: r.get("citationCount")),
    ("模型", lambda r: r.get("model")),
    ("耗时ms", lambda r: r.get("latencyMs")),
    ("提示token", lambda r: r.get("promptTokens")),
    ("补全token", lambda r: r.get("completionTokens")),
    ("总token", lambda r: r.get("totalTokens")),
]

#: 咨询用户视图导出列
USER_CSV_COLUMNS = [
    ("会话ID", lambda r: r.get("sessionId")),
    ("咨询用户", lambda r: r.get("role")),
    ("智能体", lambda r: r.get("agentName")),
    ("渠道", lambda r: r.get("channelName")),
    ("首次时间", lambda r: r.get("createdAt")),
    ("末次时间", lambda r: r.get("lastAt")),
    ("问数", lambda r: r.get("questionCount")),
    ("拒答数", lambda r: r.get("refusedCount")),
    ("总token", lambda r: r.get("totalTokens")),
    ("最后提问", lambda r: r.get("lastQuestion")),
]


# ------------------------------------------------------------------ 工具
def _mysql() -> bool:
    return settings.store_label == "mysql"


def _iso(value) -> str | None:
    """datetime → ``YYYY-MM-DDTHH:MM:SS``（与 chatlog.py 的 createdAt 口径一致）。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat(timespec="seconds")


def _as_float(value) -> float | None:
    return float(value) if value is not None else None


def _as_int(value) -> int | None:
    return int(value) if value is not None else None


def _parse_day(raw: str | None, *, end: bool = False) -> datetime | None:
    """``YYYY-MM-DD`` → 当天 00:00（end=False）或 23:59:59（end=True）。

    同时接受完整 ISO 时间串；无法解析时返回 ``None``（视为不筛选）。
    """
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        if len(text) == 10:
            day = datetime.strptime(text, "%Y-%m-%d").date()
            return datetime.combine(day, time(23, 59, 59) if end else time(0, 0, 0))
        return datetime.fromisoformat(text)
    except ValueError:
        logger.warning("会话日志：忽略无法解析的时间参数 %r", raw)
        return None


def _page_args(page: int | None, page_size: int | None) -> tuple[int, int]:
    p = max(1, int(page or 1))
    size = int(page_size or DEFAULT_PAGE_SIZE)
    size = max(1, min(MAX_PAGE_SIZE, size))
    return p, size


def _in_range(value: str | None, df: datetime | None, dt: datetime | None) -> bool:
    """ISO 字符串是否落在 [df, dt] 内；无法解析时按「不在范围内」处理。"""
    if not value:
        return False
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return False
    if df is not None and moment < df:
        return False
    if dt is not None and moment > dt:
        return False
    return True


def _match(record: dict, keyword: str | None) -> bool:
    """关键词命中：问题或回答包含（大小写不敏感）。"""
    if not keyword:
        return True
    needle = keyword.strip().lower()
    if not needle:
        return True
    return needle in (record.get("question") or "").lower() or needle in (
        record.get("answer") or ""
    ).lower()


# ------------------------------------------------------------------ 取数：JSON
def _load_json(*, role: str | None = None, limit: int = SCAN_LIMIT) -> tuple[dict, list[dict]]:
    """读 ``data/sessions.json``，归一为 ``(会话元信息, 消息行)``。

    与 MySQL 分支保持同一口径：按会话 ``createdAt`` 倒序遍历，累积到 ``limit``
    条消息为止（**不截断正在进行中的会话**，避免把一问一答从中间切开）。
    ``role`` 为会话级筛选，在此下推。
    """
    from app.db import chatlog as chatlog_store

    sessions = chatlog_store.read_all_sessions()
    sessions.sort(key=lambda s: s.get("createdAt") or "", reverse=True)

    meta: dict[str, dict] = {}
    messages: list[dict] = []

    for sess in sessions:
        sid = sess.get("sessionId")
        if not sid:
            continue
        sess_role = sess.get("role") or "访客"
        if role and sess_role != role:
            continue
        if len(messages) >= limit:
            break
        meta[sid] = {
            "role": sess_role,
            "agentId": None,
            "agentName": None,
            "channelId": None,
            "channelName": None,
            "createdAt": sess.get("createdAt"),
        }
        for msg in sess.get("messages") or []:
            raw_role = msg.get("role")
            messages.append(
                {
                    "sessionId": sid,
                    "msgId": int(msg.get("msgId") or 0),
                    "msgRole": ROLE_AI if raw_role == "ai" else ROLE_USER,
                    "content": msg.get("content") or "",
                    "confidence": _as_float(msg.get("confidence")),
                    "refused": bool(msg.get("refused")),
                    "traceId": msg.get("traceId") or "",
                    "createdAt": msg.get("createdAt"),
                    "citationCount": len(msg.get("citations") or []),
                    "citations": list(msg.get("citations") or []),
                    # JSON 分支不落这些字段（见模块说明第 2 条）
                    "model": None,
                    "latencyMs": None,
                    "promptTokens": None,
                    "completionTokens": None,
                    "totalTokens": None,
                }
            )
    return meta, messages


# ------------------------------------------------------------------ 取数：MySQL
def _load_mysql(
    *,
    agent_id: int | None,
    channel_id: int | None,
    role: str | None,
    date_from: datetime | None,
    date_to: datetime | None,
) -> tuple[dict, list[dict]]:
    """查库，归一为 ``(会话元信息, 消息行)``。

    会话级筛选（role / agent / channel）与时间范围**下推到 SQL**，
    使 ``SCAN_LIMIT`` 窗口只覆盖相关消息，而不是全库最近消息。
    """
    with db_session() as conn:
        stmt = (
            select(
                M.ChatMessage.msg_id,
                M.ChatMessage.session_id,
                M.ChatMessage.msg_role,
                M.ChatMessage.content,
                M.ChatMessage.confidence,
                M.ChatMessage.refused,
                M.ChatMessage.trace_id,
                M.ChatMessage.created_at,
                M.ChatSession.role_snapshot,
                M.ChatSession.agent_id,
                M.ChatSession.channel_id,
                M.ChatSession.created_at,
            )
            .join(M.ChatSession, M.ChatSession.session_id == M.ChatMessage.session_id)
            .order_by(desc(M.ChatMessage.msg_id))
            .limit(SCAN_LIMIT)
        )
        if role:
            stmt = stmt.where(M.ChatSession.role_snapshot == role)
        if agent_id is not None:
            stmt = stmt.where(M.ChatSession.agent_id == agent_id)
        if channel_id is not None:
            stmt = stmt.where(M.ChatSession.channel_id == channel_id)
        if date_from is not None:
            stmt = stmt.where(M.ChatMessage.created_at >= date_from)
        if date_to is not None:
            stmt = stmt.where(M.ChatMessage.created_at <= date_to)

        rows = conn.execute(stmt).all()

        agent_names = {
            int(r[0]): r[1]
            for r in conn.execute(select(M.Agent.agent_id, M.Agent.agent_name)).all()
        }
        channel_names = {
            int(r[0]): r[1]
            for r in conn.execute(select(M.Channel.channel_id, M.Channel.channel_name)).all()
        }

        ai_msg_ids = [int(r[0]) for r in rows if int(r[2]) == ROLE_AI]
        cite_counts: dict[int, int] = {}
        if ai_msg_ids:
            for c in conn.execute(
                select(M.AnswerCitation.msg_id, func.count())
                .where(M.AnswerCitation.msg_id.in_(ai_msg_ids))
                .group_by(M.AnswerCitation.msg_id)
            ).all():
                cite_counts[int(c[0])] = int(c[1])

        traces = sorted({str(r[6]) for r in rows if r[6]})
        usage: dict[str, tuple] = {}
        if traces:
            for u in conn.execute(
                select(
                    M.TokenUsage.trace_id,
                    M.TokenUsage.model_code,
                    M.TokenUsage.prompt_tokens,
                    M.TokenUsage.completion_tokens,
                    M.TokenUsage.total_tokens,
                    M.TokenUsage.latency_ms,
                ).where(M.TokenUsage.trace_id.in_(traces))
            ).all():
                usage.setdefault(str(u[0]), u)

    meta: dict[str, dict] = {}
    messages: list[dict] = []
    for r in rows:
        sid = str(r[1])
        aid = _as_int(r[9])
        cid = _as_int(r[10])
        if sid not in meta:
            meta[sid] = {
                "role": r[8] or "访客",
                "agentId": aid,
                "agentName": agent_names.get(aid) if aid is not None else None,
                "channelId": cid,
                "channelName": channel_names.get(cid) if cid is not None else None,
                "createdAt": _iso(r[11]),
            }
        msg_role = int(r[2])
        u = usage.get(str(r[6])) if msg_role == ROLE_AI and r[6] else None
        messages.append(
            {
                "sessionId": sid,
                "msgId": int(r[0]),
                "msgRole": msg_role,
                "content": r[3] or "",
                "confidence": _as_float(r[4]),
                "refused": bool(r[5]),
                "traceId": str(r[6] or ""),
                "createdAt": _iso(r[7]),
                "citationCount": cite_counts.get(int(r[0]), 0) if msg_role == ROLE_AI else 0,
                "model": u[1] if u else None,
                "promptTokens": _as_int(u[2]) if u else None,
                "completionTokens": _as_int(u[3]) if u else None,
                "totalTokens": _as_int(u[4]) if u else None,
                "latencyMs": _as_int(u[5]) if u else None,
            }
        )
    return meta, messages


# ------------------------------------------------------------------ 配对
def _pair_qa(meta: dict, messages: list[dict]) -> list[dict]:
    """把消息行配对成一问一答。

    AI 消息向前找同会话 ``msg_id`` 最大的用户消息；找不到配对的 AI 消息
    仍然保留（``question`` 为空），避免管理端「看不到异常回答」。
    """
    by_session: dict[str, list[dict]] = {}
    for msg in messages:
        by_session.setdefault(msg["sessionId"], []).append(msg)

    pairs: list[dict] = []
    for sid, msgs in by_session.items():
        msgs.sort(key=lambda m: m["msgId"])
        last_user: dict | None = None
        info = meta.get(sid, {})
        for msg in msgs:
            if msg["msgRole"] == ROLE_USER:
                last_user = msg
                continue
            pairs.append(
                {
                    "msgId": msg["msgId"],
                    "sessionId": sid,
                    "question": last_user["content"] if last_user else "",
                    "answer": msg["content"],
                    "role": info.get("role") or "访客",
                    "agentId": info.get("agentId"),
                    "agentName": info.get("agentName"),
                    "channelId": info.get("channelId"),
                    "channelName": info.get("channelName"),
                    "askedAt": last_user["createdAt"] if last_user else None,
                    "answeredAt": msg["createdAt"],
                    "confidence": msg["confidence"],
                    "refused": bool(msg["refused"]),
                    "citationCount": int(msg["citationCount"] or 0),
                    "citations": list(msg.get("citations") or []),
                    "model": msg["model"],
                    "latencyMs": msg["latencyMs"],
                    "promptTokens": msg["promptTokens"],
                    "completionTokens": msg["completionTokens"],
                    "totalTokens": msg["totalTokens"],
                    "traceId": msg["traceId"],
                }
            )
    # 时间倒序；同刻按 msgId 倒序，末位用 sessionId 兜底。
    # **必须全序**：msgId 只在会话内自增、并不全局唯一，缺了末位 tiebreaker
    # 会让同刻记录顺序不稳定，进而导致翻页重复/漏项。
    pairs.sort(
        key=lambda r: (r["answeredAt"] or "", r["msgId"], r["sessionId"]), reverse=True
    )
    return pairs


def _user_view(pairs: list[dict], meta: dict) -> list[dict]:
    """按会话聚合出「咨询用户」视图。"""
    grouped: dict[str, list[dict]] = {}
    for p in pairs:
        grouped.setdefault(p["sessionId"], []).append(p)

    out: list[dict] = []
    for sid, items in grouped.items():
        info = meta.get(sid, {})
        latest = items[0]  # pairs 已按时间倒序
        tokens = [p["totalTokens"] for p in items if p.get("totalTokens") is not None]
        out.append(
            {
                "sessionId": sid,
                "role": info.get("role") or latest.get("role") or "访客",
                "agentId": info.get("agentId"),
                "agentName": info.get("agentName"),
                "channelId": info.get("channelId"),
                "channelName": info.get("channelName"),
                "createdAt": info.get("createdAt")
                or min((p.get("askedAt") or "" for p in items), default=""),
                "lastAt": latest.get("answeredAt"),
                "questionCount": len(items),
                "refusedCount": sum(1 for p in items if p.get("refused")),
                "totalTokens": sum(tokens) if tokens else None,
                "lastQuestion": latest.get("question") or "",
            }
        )
    out.sort(key=lambda r: (r.get("lastAt") or "", r["sessionId"]), reverse=True)
    return out

# ------------------------------------------------------------------ 共用筛选
def _collect(
    *,
    agent_id: int | None = None,
    channel_id: int | None = None,
    role: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    keyword: str | None = None,
) -> tuple[dict, list[dict], int]:
    """取数 + 筛选 + 配对。返回 ``(会话元信息, 问答列表, 扫描消息数)``。

    分页与导出**共用本函数**，保证「导出的就是筛选后的全集」。
    """
    df = _parse_day(date_from)
    dt = _parse_day(date_to, end=True)
    if _mysql():
        meta, messages = _load_mysql(
            agent_id=agent_id, channel_id=channel_id, role=role, date_from=df, date_to=dt
        )
    else:
        meta, messages = _load_json(role=role)

    pairs = _pair_qa(meta, messages)
    if role:
        pairs = [p for p in pairs if p.get("role") == role]
    if agent_id is not None:
        pairs = [p for p in pairs if p.get("agentId") == agent_id]
    if channel_id is not None:
        pairs = [p for p in pairs if p.get("channelId") == channel_id]
    if df is not None or dt is not None:
        pairs = [p for p in pairs if _in_range(p.get("answeredAt"), df, dt)]
    if keyword:
        pairs = [p for p in pairs if _match(p, keyword)]
    return meta, pairs, len(messages)


def _envelope(items: list[dict], total: int, page: int, size: int, scanned: int) -> dict:
    return {
        "items": items,
        "total": total,
        "page": page,
        "pageSize": size,
        "totalPages": max(1, (total + size - 1) // size),
        "scanned": scanned,
        "scanLimit": SCAN_LIMIT,
        "store": settings.store_label,
    }


# ------------------------------------------------------------------ 对外接口
def query_qa(
    *,
    agent_id: int | None = None,
    channel_id: int | None = None,
    role: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    keyword: str | None = None,
    page: int | None = 1,
    page_size: int | None = DEFAULT_PAGE_SIZE,
) -> dict:
    """问答视图：筛选 + 检索 + 分页。"""
    _, pairs, scanned = _collect(
        agent_id=agent_id,
        channel_id=channel_id,
        role=role,
        date_from=date_from,
        date_to=date_to,
        keyword=keyword,
    )
    p, size = _page_args(page, page_size)
    total = len(pairs)
    start = (p - 1) * size
    return _envelope(pairs[start : start + size], total, p, size, scanned)


def query_users(
    *,
    agent_id: int | None = None,
    channel_id: int | None = None,
    role: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    keyword: str | None = None,
    page: int | None = 1,
    page_size: int | None = DEFAULT_PAGE_SIZE,
) -> dict:
    """咨询用户视图：按会话聚合 + 筛选 + 分页。"""
    meta, pairs, scanned = _collect(
        agent_id=agent_id,
        channel_id=channel_id,
        role=role,
        date_from=date_from,
        date_to=date_to,
        keyword=keyword,
    )
    users = _user_view(pairs, meta)
    p, size = _page_args(page, page_size)
    total = len(users)
    start = (p - 1) * size
    return _envelope(users[start : start + size], total, p, size, scanned)


def export_rows(
    view: str,
    *,
    agent_id: int | None = None,
    channel_id: int | None = None,
    role: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    keyword: str | None = None,
) -> tuple[list[dict], bool]:
    """导出用：返回筛选后的**全集**行。

    :return: ``(行列表, 是否被 EXPORT_LIMIT 截断)``
    """
    meta, pairs, _ = _collect(
        agent_id=agent_id,
        channel_id=channel_id,
        role=role,
        date_from=date_from,
        date_to=date_to,
        keyword=keyword,
    )
    rows = _user_view(pairs, meta) if view == "users" else pairs
    truncated = len(rows) > EXPORT_LIMIT
    return rows[:EXPORT_LIMIT], truncated


def filter_options() -> dict:
    """筛选项：智能体 / 渠道 / 角色 下拉数据。"""
    roles: list[str] = []
    agents: list[dict] = []
    channels: list[dict] = []

    if _mysql():
        with db_session() as conn:
            agents = [
                {"agentId": int(r[0]), "agentName": r[1], "status": int(r[2] or 0)}
                for r in conn.execute(
                    select(M.Agent.agent_id, M.Agent.agent_name, M.Agent.status)
                ).all()
            ]
            channels = [
                {"channelId": int(r[0]), "channelName": r[1], "status": int(r[2] or 0)}
                for r in conn.execute(
                    select(M.Channel.channel_id, M.Channel.channel_name, M.Channel.status)
                ).all()
            ]
            roles = sorted(
                {
                    str(r[0])
                    for r in conn.execute(select(M.ChatSession.role_snapshot).distinct()).all()
                    if r[0]
                }
            )
    else:
        from app.db import chatlog as chatlog_store

        sessions = chatlog_store.read_all_sessions()
        roles = sorted({str(s.get("role")) for s in sessions if s.get("role")})
        # JSON 分支无 agent/channel 落库；给出去重后的「默认」占位，
        # 与 chatlog.py 的 seed 常量一致（不是假数据，是当前唯一实际在用的取值）
        agents = [{"agentId": None, "agentName": "能小智·校园百事通（默认）", "status": 1}]
        channels = [{"channelId": None, "channelName": "门户Web（默认）", "status": 1}]

    return {"agents": agents, "channels": channels, "roles": roles, "store": settings.store_label}


def to_csv(view: str, rows: list[dict]) -> str:
    """把查询结果导出为 CSV 文本（带 UTF-8 BOM，Excel 直接打开不乱码）。"""
    columns = USER_CSV_COLUMNS if view == "users" else QA_CSV_COLUMNS
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([name for name, _ in columns])
    for row in rows:
        writer.writerow(["" if (v := getter(row)) is None else v for _, getter in columns])
    return "\ufeff" + buf.getvalue()
