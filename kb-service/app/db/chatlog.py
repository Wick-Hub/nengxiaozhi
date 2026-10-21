"""会话与消息存储（任务 1.8 / 1.14）。

两种后端，对外接口完全一致：

* **JSON 模式**（``KB_STORE=json``）：``kb-service/data/sessions.json``，
  进程内加锁 + 原子写（``.tmp`` → ``replace``）。
* **MySQL 模式**（``KB_STORE=mysql``）：写入 ``chat_session`` / ``chat_message`` /
  ``answer_citation`` / ``token_usage`` / ``unrecognized_question``，
  严格对齐原型 ``kb-engine/src/store/chatlog.js``：
  - 会话与消息表不存在时按需 seed 出 ``agent(campus-default)`` 与
    ``channel(portal-web)``（``chat_session.channel_id`` 是 NOT NULL）；
  - 一次问答全链路落库：会话 + 用户消息 + AI 消息 + 引用 + token 计量；
  - 拒答 / 低置信度时把问题写入 ``unrecognized_question`` 未识别问题池
    （任务 3.11，空召回的「弱召回」由管线单独入池）。

消息角色沿用原型的 ``user`` / ``ai``（DB 侧 ``msg_role`` 1=用户 2=AI），
读取历史时 ``ai`` 映射为 ``assistant`` 供大模型使用。

与原型的一处**有意差异**：原型用 ``INSERT IGNORE`` 建会话，会话角色在首条消息后
不再更新；本实现额外在传入 ``role`` 时刷新 ``role_snapshot``，与 JSON 模式行为一致。
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, insert, select, text, update

from app.config import settings
from app.db import kb_repo, unrecognized_store
from app.db import models as M
from app.db.session import db_session, db_transaction
from app.services import sentiment

logger = logging.getLogger(__name__)

SESSIONS_PATH = settings.base_dir / "data" / "sessions.json"
_lock = threading.RLock()

_B36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# 原型 chatlog.js 的 seed 常量
DEFAULT_AGENT_CODE = "campus-default"
DEFAULT_AGENT_NAME = "能小智·校园百事通"
DEFAULT_CHANNEL_CODE = "portal-web"
DEFAULT_CHANNEL_NAME = "门户Web"

ROLE_USER = 1
ROLE_AI = 2

_seed_cache: dict | None = None


def _b36(n: int) -> str:
    if n == 0:
        return "0"
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = _B36[r] + out
    return out


def new_session_id() -> str:
    """会话 ID：``S`` + base36 毫秒 + 8 位随机 hex。

    ⚠ 随机部分必须是 **``token_hex(4)``（32 bit）**。早先是 ``token_hex(2)``
    （16 bit，仅 65536 种），同一毫秒内并发建会话会有**实测可复现的碰撞**：
    同一毫秒 50 个会话约 1.3%，500 个时约 85%，1000 个时几乎必然。
    会话 ID 碰撞意味着**两个用户共用同一段多轮上下文**（串话），
    属于数据正确性问题，不是理论风险（C-01 的 30000 RPM 目标下尤其真实）。
    """
    return "S" + _b36(int(time.time() * 1000)).upper() + secrets.token_hex(4).upper()


def new_trace_id() -> str:
    """链路 ID：``T`` + base36 毫秒 + 8 位随机 hex（理由同 :func:`new_session_id`）。

    碰撞会让 ``token_usage`` / ``answer_citation`` 归错记录，进而导致用量对账失真。
    """
    return "T" + _b36(int(time.time() * 1000)).upper() + secrets.token_hex(4).upper()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _iso(dt) -> str | None:
    """DB datetime → ISO 字符串（与 JSON 模式的 createdAt 格式一致）。"""
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    return dt.isoformat(timespec="seconds")


def _mysql() -> bool:
    return settings.store_label == "mysql"


# ================================================================== JSON 后端
def _read_all() -> dict:
    if not SESSIONS_PATH.is_file():
        return {"sessions": {}}
    try:
        data = json.loads(SESSIONS_PATH.read_text(encoding="utf-8"))
        if not isinstance(data.get("sessions"), dict):
            data["sessions"] = {}
        return data
    except Exception:  # noqa: BLE001 - 文件损坏时重建，避免影响问答
        logger.warning("会话文件损坏，已重建：%s", SESSIONS_PATH)
        return {"sessions": {}}


def _write_all(data: dict) -> None:
    SESSIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SESSIONS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SESSIONS_PATH)


def _ensure_session(
    data: dict,
    session_id: str,
    role: str | None,
    *,
    agent_id=None,
    channel_code: str | None = None,
    dept: str | None = None,
) -> dict:
    sess = data["sessions"].get(session_id)
    if sess is None:
        sess = {
            "sessionId": session_id,
            "role": role or "访客",
            "createdAt": _now(),
            "messages": [],
            "agentId": str(agent_id) if agent_id is not None else "",
            "channelCode": channel_code or "",
            "dept": dept or "",
        }
        data["sessions"][session_id] = sess
    else:
        if role:
            sess["role"] = role
        # 归因只补空值、不覆盖：同一会话后续换渠道/助手访问仍按首次口径统计
        if agent_id is not None and not sess.get("agentId"):
            sess["agentId"] = str(agent_id)
        if channel_code and not sess.get("channelCode"):
            sess["channelCode"] = channel_code
        if dept and not sess.get("dept"):
            sess["dept"] = dept
    return sess


def _next_msg_id(sess: dict) -> int:
    return (max((m.get("msgId", 0) for m in sess["messages"]), default=0)) + 1


def _save_qa_json(
    question, role, gen, session_id, trace_id, *, agent_id=None, channel_code=None, dept=None
) -> dict:
    sid = session_id or new_session_id()
    with _lock:
        data = _read_all()
        sess = _ensure_session(
            data, sid, role, agent_id=agent_id, channel_code=channel_code, dept=dept
        )

        uid = _next_msg_id(sess)
        sess["messages"].append(
            {
                "msgId": uid,
                "role": "user",
                "content": question,
                "sentiment": sentiment.tag(question),
                "createdAt": _now(),
            }
        )
        aid = uid + 1
        sess["messages"].append(
            {
                "msgId": aid,
                "role": "ai",
                "content": gen.answer,
                "confidence": gen.confidence,
                "refused": bool(gen.refused),
                "citations": list(gen.citations or []),
                "traceId": trace_id or new_trace_id(),
                "createdAt": _now(),
            }
        )
        _write_all(data)
    return {"sessionId": sid, "msgId": aid}


def _append_message_json(session_id: str, role: str, content: str, **extra) -> dict:
    with _lock:
        data = _read_all()
        sess = _ensure_session(data, session_id, extra.pop("user_role", None))
        msg = {
            "msgId": _next_msg_id(sess),
            "role": role,
            "content": content,
            "createdAt": _now(),
            **extra,
        }
        sess["messages"].append(msg)
        _write_all(data)
    return msg


def _load_session_json(session_id: str) -> dict | None:
    with _lock:
        sess = _read_all()["sessions"].get(session_id)
    return sess or None


def _list_sessions_json(limit: int) -> list[dict]:
    with _lock:
        sessions = list(_read_all()["sessions"].values())
    sessions.sort(key=lambda s: s.get("createdAt", ""), reverse=True)
    out: list[dict] = []
    for s in sessions[:limit]:
        msgs = s.get("messages", [])
        last_user = next((m["content"] for m in reversed(msgs) if m.get("role") == "user"), "")
        out.append(
            {
                "sessionId": s.get("sessionId"),
                "role": s.get("role"),
                "createdAt": s.get("createdAt"),
                "messageCount": len(msgs),
                "lastQuestion": last_user,
            }
        )
    return out


# ================================================================== MySQL 后端
def ensure_seed(conn) -> dict:
    """确保默认助手与渠道存在，返回 ``{agent_id, channel_id}``。

    对齐原型 ``ensureSeed``：``agent`` 与 ``channel`` 表为空时按需创建，
    否则 ``chat_session.channel_id``（NOT NULL）无处可填。
    """
    global _seed_cache
    if _seed_cache is not None:
        return _seed_cache

    agent_id = conn.execute(
        select(M.Agent.agent_id).where(M.Agent.agent_code == DEFAULT_AGENT_CODE)
    ).scalar()
    if not agent_id:
        res = conn.execute(
            insert(M.Agent).values(
                agent_code=DEFAULT_AGENT_CODE,
                agent_name=DEFAULT_AGENT_NAME,
                model_code="default",
                temperature=0.30,
                visible_roles="全部",
            )
        )
        agent_id = int(res.inserted_primary_key[0])

    channel_id = conn.execute(
        select(M.Channel.channel_id).where(M.Channel.channel_code == DEFAULT_CHANNEL_CODE)
    ).scalar()
    if not channel_id:
        res = conn.execute(
            insert(M.Channel).values(
                channel_code=DEFAULT_CHANNEL_CODE,
                channel_name=DEFAULT_CHANNEL_NAME,
                default_agent_id=agent_id,
                need_auth=0,
                status=1,
            )
        )
        channel_id = int(res.inserted_primary_key[0])

    _seed_cache = {"agent_id": int(agent_id), "channel_id": int(channel_id)}
    logger.info("会话 seed 就绪：agent_id=%s / channel_id=%s", agent_id, channel_id)
    return _seed_cache


def reset_seed_cache() -> None:
    """清空 seed 缓存（测试或库重建后调用）。"""
    global _seed_cache
    _seed_cache = None


def _session_scope(conn, agent_id=None, channel_code: str | None = None) -> dict:
    """会话归因：显式助手/渠道优先，缺省回落到 seed 的默认值。

    渠道按编码解析成 ``channel_id``（``chat_session.channel_id`` 是 BIGINT）；
    编码不存在时静默回落，避免第三方页面传错渠道编码导致问答整体失败。
    """
    seed = ensure_seed(conn)
    aid = seed["agent_id"]
    if agent_id is not None and str(agent_id).strip().isdigit():
        aid = int(str(agent_id).strip())
    cid = seed["channel_id"]
    code = (channel_code or "").strip()
    if code:
        found = conn.execute(
            select(M.Channel.channel_id).where(M.Channel.channel_code == code)
        ).scalar()
        if found:
            cid = int(found)
    return {"agent_id": aid, "channel_id": cid}


def _save_qa_mysql(
    question, role, gen, session_id, trace_id, *, agent_id=None, channel_code=None, dept=None
) -> dict:
    sid = session_id or new_session_id()
    trace = trace_id or new_trace_id()
    usage = gen.usage or {}

    with db_transaction() as conn:
        scope = _session_scope(conn, agent_id, channel_code)
        conn.execute(
            text(
                "INSERT IGNORE INTO chat_session "
                "(session_id, channel_id, agent_id, role_snapshot) VALUES (:sid, :cid, :aid, :role)"
            ),
            {
                "sid": sid,
                "cid": scope["channel_id"],
                "aid": scope["agent_id"],
                "role": role or "访客",
            },
        )
        if role:
            # 原型用 INSERT IGNORE 建会话后不再更新角色；这里补一次刷新（见模块说明）
            conn.execute(
                text(
                    "UPDATE chat_session SET role_snapshot = :role, updated_at = NOW() "
                    "WHERE session_id = :sid"
                ),
                {"role": role, "sid": sid},
            )

        conn.execute(
            insert(M.ChatMessage).values(
                session_id=sid,
                msg_role=ROLE_USER,
                content=question,
                trace_id=trace,
                sentiment=sentiment.tag(question),
            )
        )
        res = conn.execute(
            insert(M.ChatMessage).values(
                session_id=sid,
                msg_role=ROLE_AI,
                content=gen.answer,
                trace_id=trace,
                confidence=round(float(gen.confidence or 0.0), 4),
                refused=1 if gen.refused else 0,
            )
        )
        msg_id = int(res.inserted_primary_key[0])

        for c in gen.citations or []:
            conn.execute(
                insert(M.AnswerCitation).values(
                    msg_id=msg_id,
                    chunk_id=str(c.get("chunkId") or ""),
                    doc_id=int(c.get("docId") or 0),
                    similarity=round(float(c.get("similarity") or 0.0), 4),
                )
            )

        conn.execute(
            insert(M.TokenUsage).values(
                trace_id=trace,
                channel_id=scope["channel_id"],
                agent_id=scope["agent_id"],
                model_code=gen.model or "",
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                total_tokens=int(usage.get("total_tokens") or 0),
                latency_ms=int(gen.latency_ms or 0),
                first_token_ms=gen.first_token_ms,
                call_state=1,
            )
        )

    return {"sessionId": sid, "msgId": msg_id}


def _append_message_mysql(session_id: str, role: str, content: str, **extra) -> dict:
    msg_role = ROLE_AI if role == "ai" else ROLE_USER
    with db_transaction() as conn:
        seed = ensure_seed(conn)
        conn.execute(
            text(
                "INSERT IGNORE INTO chat_session "
                "(session_id, channel_id, agent_id, role_snapshot) VALUES (:sid, :cid, :aid, :role)"
            ),
            {
                "sid": session_id,
                "cid": seed["channel_id"],
                "aid": seed["agent_id"],
                "role": extra.pop("user_role", None) or "访客",
            },
        )
        res = conn.execute(
            insert(M.ChatMessage).values(
                session_id=session_id,
                msg_role=msg_role,
                content=content,
                trace_id=extra.get("traceId") or "",
                confidence=(
                    round(float(extra["confidence"]), 4) if extra.get("confidence") is not None else None
                ),
                refused=1 if extra.get("refused") else 0,
            )
        )
        msg_id = int(res.inserted_primary_key[0])
    return {
        "msgId": msg_id,
        "role": role,
        "content": content,
        "createdAt": _now(),
        **extra,
    }


def _load_session_mysql(session_id: str) -> dict | None:
    with db_session() as conn:
        head = conn.execute(
            select(M.ChatSession.role_snapshot, M.ChatSession.created_at).where(
                M.ChatSession.session_id == session_id
            )
        ).first()
        rows = conn.execute(
            select(
                M.ChatMessage.msg_id,
                M.ChatMessage.msg_role,
                M.ChatMessage.content,
                M.ChatMessage.confidence,
                M.ChatMessage.refused,
                M.ChatMessage.created_at,
            )
            .where(M.ChatMessage.session_id == session_id)
            .order_by(M.ChatMessage.msg_id)
        ).all()
        if not rows:
            return None

        msg_ids = [int(r.msg_id) for r in rows]
        cites = conn.execute(
            select(
                M.AnswerCitation.msg_id,
                M.AnswerCitation.chunk_id,
                M.AnswerCitation.similarity,
                M.KbChunk.title_path,
                M.KbChunk.block_type,
                M.KbDoc.doc_title,
                M.KbDoc.ktype,
                M.SysOrg.org_name,
            )
            .join(M.KbChunk, M.KbChunk.chunk_id == M.AnswerCitation.chunk_id)
            .join(M.KbDoc, M.KbDoc.doc_id == M.KbChunk.doc_id)
            .outerjoin(M.SysOrg, M.SysOrg.org_id == M.KbDoc.dept_id)
            .where(M.AnswerCitation.msg_id.in_(msg_ids))
            .order_by(M.AnswerCitation.msg_id, M.AnswerCitation.id)
        ).all()

    cite_map: dict[int, list[dict]] = {}
    for c in cites:
        cite_map.setdefault(int(c.msg_id), []).append(
            {
                "chunkId": c.chunk_id,
                "similarity": float(c.similarity) if c.similarity is not None else None,
                "titlePath": c.title_path,
                "blockType": c.block_type,
                "docTitle": c.doc_title,
                # 库里存的是枚举（guide/faq/...），统一转中文名，与实时链路一致
                # （原型 loadSession 直接返回枚举，前端会显示成 "guide"）
                "ktype": kb_repo.ktype_label(c.ktype),
                "dept": c.org_name,
            }
        )

    messages = []
    for r in rows:
        cites_for_msg = cite_map.get(int(r.msg_id), [])
        # answer_citation 无「正文编号 n」列，按插入顺序回填（前端 n || startN+i 的等价形式）
        for i, c in enumerate(cites_for_msg):
            c["n"] = i + 1
        messages.append(
            {
                "msgId": int(r.msg_id),
                "role": "user" if int(r.msg_role) == ROLE_USER else "ai",
                "content": r.content,
                "confidence": float(r.confidence) if r.confidence is not None else None,
                "refused": bool(r.refused),
                "citations": cites_for_msg,
                "createdAt": _iso(r.created_at),
            }
        )

    return {
        "sessionId": session_id,
        "role": head.role_snapshot if head else "访客",
        "createdAt": _iso(head.created_at) if head else None,
        "messages": messages,
    }


def _list_sessions_mysql(limit: int) -> list[dict]:
    # 关联子查询需显式 correlate，否则 SQLAlchemy 会把 chat_message 从子查询里
    # 自动消除，导致 "returned no FROM clauses due to auto-correlation"。
    last_q = (
        select(M.ChatMessage.content)
        .where(
            M.ChatMessage.session_id == M.ChatSession.session_id,
            M.ChatMessage.msg_role == ROLE_USER,
        )
        .order_by(M.ChatMessage.msg_id.desc())
        .limit(1)
        .correlate(M.ChatSession)
        .scalar_subquery()
    )
    with db_session() as conn:
        rows = conn.execute(
            select(
                M.ChatSession.session_id,
                M.ChatSession.role_snapshot,
                M.ChatSession.created_at,
                func.count(M.ChatMessage.msg_id),
                last_q.label("last_question"),
            )
            .outerjoin(M.ChatMessage, M.ChatMessage.session_id == M.ChatSession.session_id)
            .group_by(M.ChatSession.session_id, M.ChatSession.role_snapshot, M.ChatSession.created_at)
            .order_by(M.ChatSession.created_at.desc())
            .limit(limit)
        ).all()

    return [
        {
            "sessionId": r[0],
            "role": r[1],
            "createdAt": _iso(r[2]),
            "messageCount": int(r[3] or 0),
            "lastQuestion": r[4] or "",
        }
        for r in rows
    ]


# ================================================================== 对外接口
def _pool_source(gen) -> str | None:
    """回答是否应进入未识别问题池（任务 3.11）。"""
    if not settings.unrecognized_enabled:
        return None
    if getattr(gen, "refused", False):
        return unrecognized_store.SOURCE_REFUSED
    conf = getattr(gen, "confidence", None)
    if conf is not None and float(conf) < settings.unrecognized_confidence_threshold:
        return unrecognized_store.SOURCE_LOW_CONFIDENCE
    return None


def note_unrecognized(
    *,
    session_id: str | None,
    question: str,
    role: str | None = None,
    confidence: float | None = None,
    source: str = "",
    msg_id: int | None = None,
    agent_id=None,
    channel_code: str | None = None,
    dept: str | None = None,
) -> int | None:
    """入池的安全包装：池写入失败只记日志，绝不回滚/阻断问答主链路。"""
    try:
        return unrecognized_store.note_unrecognized(
            session_id=session_id,
            question=question,
            role=role,
            confidence=confidence,
            source=source,
            msg_id=msg_id,
            agent_id=agent_id,
            channel_code=channel_code,
            dept=dept,
        )
    except Exception as exc:  # noqa: BLE001 - 反哺闭环不能影响用户问答
        logger.warning("未识别问题入池失败（session=%s）：%s", session_id, exc)
        return None


def save_qa(
    question: str,
    role: str | None,
    gen,
    session_id: str | None,
    trace_id: str | None = None,
    *,
    agent_id=None,
    channel_code: str | None = None,
    dept: str | None = None,
) -> dict:
    """一次问答落库：用户消息 + AI 消息 + 引用（+ token 计量 / 未识别问题）。

    :param gen: ``GenerationResult``（需含 answer/citations/confidence/refused/usage/model/latency_ms）
    :param agent_id: 本次回答的助手 ID（任务 3.11 归因）
    :param channel_code: 本次请求来源渠道编码（任务 3.11 归因）
    :return: ``{"sessionId": str, "msgId": int}``
    """
    if _mysql():
        saved = _save_qa_mysql(
            question, role, gen, session_id, trace_id,
            agent_id=agent_id, channel_code=channel_code, dept=dept,
        )
    else:
        saved = _save_qa_json(
            question, role, gen, session_id, trace_id,
            agent_id=agent_id, channel_code=channel_code, dept=dept,
        )
    source = _pool_source(gen)
    if source:
        note_unrecognized(
            session_id=saved.get("sessionId"),
            question=question,
            role=role,
            confidence=getattr(gen, "confidence", None),
            source=source,
            msg_id=saved.get("msgId"),
            agent_id=agent_id,
            channel_code=channel_code,
            dept=dept,
        )
    return saved


def append_message(session_id: str, role: str, content: str, **extra) -> dict:
    """追加单条消息（供管理端/测试使用）。"""
    if _mysql():
        return _append_message_mysql(session_id, role, content, **extra)
    return _append_message_json(session_id, role, content, **extra)


def set_session_device(session_id: str, device: str) -> bool:
    """记录会话首次访问设备（任务 3.10 / M-T-03）。

    只补空值，不覆盖已有设备：同一会话后续换设备访问仍归入首次口径。
    失败只记日志，不影响问答主链路。
    """
    if not session_id or device not in {"pc", "mobile", "tablet", "other"}:
        return False
    try:
        if _mysql():
            with db_transaction() as conn:
                res = conn.execute(
                    update(M.ChatSession)
                    .where(
                        M.ChatSession.session_id == session_id,
                        M.ChatSession.device == "",
                    )
                    .values(device=device)
                )
                return bool(res.rowcount)
        with _lock:
            data = _read_all()
            sess = data["sessions"].get(session_id)
            if not sess or sess.get("device"):
                return False
            sess["device"] = device
            _write_all(data)
        return True
    except Exception as exc:  # noqa: BLE001 - 设备统计不能拖垮问答
        logger.warning("记录访问设备失败（session=%s）：%s", session_id, exc)
        return False


def load_session(session_id: str) -> dict | None:
    """读取会话（含每条 AI 消息的引用）。不存在时返回 ``None``。"""
    if _mysql():
        return _load_session_mysql(session_id)
    return _load_session_json(session_id)


def load_history(session_id: str | None, limit: int | None = None) -> list[dict]:
    """读取最近若干条消息作为多轮上下文（ai → assistant）。"""
    if not session_id:
        return []
    limit = limit or settings.history_max_messages
    sess = load_session(session_id)
    if not sess:
        return []
    recent = sess["messages"][-limit:]
    return [
        {"role": "assistant" if m.get("role") == "ai" else "user", "content": m.get("content", "")}
        for m in recent
    ]


def list_sessions(limit: int = 50) -> list[dict]:
    if _mysql():
        return _list_sessions_mysql(limit)
    return _list_sessions_json(limit)


def read_all_sessions() -> list[dict]:
    """列出全部会话（含 ``messages``）。

    供管理端「会话日志」页（任务 2.1）做跨会话配对/检索用。
    **仅 JSON 模式**有意义——MySQL 模式下会话量级大，应由 SQL 侧筛选后分页，
    因此这里在 MySQL 模式返回空列表，调用方须走 ``db_session`` 查询。
    """
    if _mysql():
        return []
    with _lock:
        return list(_read_all()["sessions"].values())
