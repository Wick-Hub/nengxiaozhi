"""问答分享与用户反馈存储（任务 4.9 / P-QA-07·08）。

两种后端，对外接口一致：

* **JSON**：``data/shares.json`` / ``data/feedback.json``（进程内锁 + 原子写）；
* **MySQL**：``qa_share`` / ``qa_feedback``（见 ``database/schema.sql``）。

分享是**创建时冻结的快照**：原会话后续变化（重新生成、清理）不影响已发出的
链接。同一条 AI 消息重复分享复用同一个 ``share_id``，避免同一答案产生多条链接。
反馈按 ``session_id + msg_id`` 归一：用户改主意（赞→踩）时更新原记录而不是新增，
管理端看到的是「一条消息一条反馈」。
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from datetime import datetime, time
from pathlib import Path

from sqlalchemy import func, insert, select, update

from app.config import settings
from app.db import models as M
from app.db.session import db_session, db_transaction

logger = logging.getLogger(__name__)

SHARES_PATH: Path = settings.base_dir / "data" / "shares.json"
FEEDBACK_PATH: Path = settings.base_dir / "data" / "feedback.json"
_lock = threading.RLock()

RATING_LIKE = 1
RATING_DISLIKE = -1
RATING_LABELS = {RATING_LIKE: "点赞", RATING_DISLIKE: "点踩"}

REASON_CODES: dict[str, str] = {
    "inaccurate": "内容不准确",
    "incomplete": "回答不完整",
    "irrelevant": "答非所问",
    "other": "其他原因",
}

EXPORT_LIMIT = 5000
_B36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _mysql() -> bool:
    return settings.store_label == "mysql"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value)


def new_share_id() -> str:
    """公开分享 ID：``Q`` + base36 毫秒 + 6 位随机 hex（URL 安全且不可枚举）。"""
    ms = int(datetime.now().timestamp() * 1000)
    out = ""
    while ms:
        ms, rem = divmod(ms, 36)
        out = _B36[rem] + out
    return ("Q" + out + secrets.token_hex(3)).upper()


# ================================================================== JSON 后端
def _read_json(path: Path, root: str, next_key: str) -> dict:
    if not path.is_file():
        return {root: {}, next_key: 1}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data.get(root), dict):
            data[root] = {}
        data[next_key] = int(data.get(next_key) or len(data[root]) + 1)
        return data
    except Exception:  # noqa: BLE001 - 单个文件损坏不应拖垮分享/反馈
        logger.warning("分享/反馈文件损坏，已重建：%s", path)
        return {root: {}, next_key: 1}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_shares() -> dict:
    return _read_json(SHARES_PATH, "shares", "nextId")


def _write_shares(data: dict) -> None:
    _write_json(SHARES_PATH, data)


def _read_feedback() -> dict:
    return _read_json(FEEDBACK_PATH, "items", "nextId")


def _write_feedback(data: dict) -> None:
    _write_json(FEEDBACK_PATH, data)


def _share_dict(raw: dict) -> dict:
    return {
        "shareId": raw.get("shareId") or "",
        "sessionId": raw.get("sessionId") or "",
        "msgId": raw.get("msgId"),
        "question": raw.get("question") or "",
        "answer": raw.get("answer") or "",
        "citations": raw.get("citations") or [],
        "agentName": raw.get("agentName") or "",
        "channelCode": raw.get("channelCode") or "",
        "role": raw.get("role") or "",
        "views": int(raw.get("views") or 0),
        "createdAt": raw.get("createdAt"),
    }


def _feedback_dict(raw: dict) -> dict:
    rating = int(raw.get("rating") or 0)
    reason_code = raw.get("reasonCode") or raw.get("reason_code") or ""
    return {
        "feedbackId": int(raw.get("feedbackId") or raw.get("feedback_id") or 0),
        "shareId": raw.get("shareId") or raw.get("share_id") or "",
        "sessionId": raw.get("sessionId") or raw.get("session_id") or "",
        "msgId": raw.get("msgId") if raw.get("msgId") is not None else raw.get("msg_id"),
        "rating": rating,
        "ratingLabel": RATING_LABELS.get(rating, "未知"),
        "reasonCode": reason_code,
        "reasonLabel": REASON_CODES.get(reason_code, ""),
        "reason": raw.get("reason") or "",
        "question": raw.get("question") or "",
        "answerExcerpt": raw.get("answerExcerpt") or raw.get("answer_excerpt") or "",
        "role": raw.get("role") or raw.get("role_snapshot") or "",
        "channelCode": raw.get("channelCode") or raw.get("channel_code") or "",
        "handled": 1 if int(raw.get("handled") or 0) else 0,
        "handleNote": raw.get("handleNote") or raw.get("handle_note") or "",
        "createdAt": raw.get("createdAt") or raw.get("created_at"),
        "handledAt": raw.get("handledAt") or raw.get("handled_at"),
    }


def _share_row(row) -> dict:
    return _share_dict(
        {
            "shareId": row.share_id,
            "sessionId": row.session_id,
            "msgId": row.msg_id,
            "question": row.question,
            "answer": row.answer,
            "citations": row.citations,
            "agentName": row.agent_name,
            "channelCode": row.channel_code,
            "role": row.role_snapshot,
            "views": row.views,
            "createdAt": _iso(row.created_at),
        }
    )


def _feedback_row(row) -> dict:
    return _feedback_dict(
        {
            "feedbackId": row.feedback_id,
            "shareId": row.share_id,
            "sessionId": row.session_id,
            "msgId": row.msg_id,
            "rating": row.rating,
            "reasonCode": row.reason_code,
            "reason": row.reason,
            "question": row.question,
            "answerExcerpt": row.answer_excerpt,
            "role": row.role_snapshot,
            "channelCode": row.channel_code,
            "handled": row.handled,
            "handleNote": row.handle_note,
            "createdAt": _iso(row.created_at),
            "handledAt": _iso(row.handled_at),
        }
    )


# ================================================================== 分享
def find_share_by_message(session_id: str, msg_id: int) -> dict | None:
    if _mysql():
        with db_session() as conn:
            row = conn.execute(
                select(M.QaShare).where(
                    M.QaShare.session_id == session_id,
                    M.QaShare.msg_id == int(msg_id),
                )
            ).first()
        return _share_row(row) if row else None
    with _lock:
        for raw in _read_shares()["shares"].values():
            if not isinstance(raw, dict):
                continue
            if raw.get("sessionId") == session_id and raw.get("msgId") == int(msg_id):
                return _share_dict(raw)
    return None


def create_share(
    *,
    session_id: str,
    msg_id: int | None,
    question: str,
    answer: str,
    citations: list | None = None,
    agent_name: str = "",
    channel_code: str = "",
    role: str = "",
) -> dict:
    """创建分享快照；同一 ``session_id + msg_id`` 已分享时直接复用原记录。"""
    if msg_id is not None:
        existing = find_share_by_message(session_id, int(msg_id))
        if existing:
            existing["reused"] = True
            return existing

    share_id = new_share_id()
    record = {
        "shareId": share_id,
        "sessionId": session_id,
        "msgId": int(msg_id) if msg_id is not None else None,
        "question": str(question or "")[:1024],
        "answer": str(answer or ""),
        "citations": list(citations or []),
        "agentName": str(agent_name or "")[:128],
        "channelCode": str(channel_code or "")[:64],
        "role": str(role or "")[:64],
        "views": 0,
        "createdAt": _now(),
    }

    if _mysql():
        with db_transaction() as conn:
            conn.execute(
                insert(M.QaShare).values(
                    share_id=share_id,
                    session_id=session_id,
                    msg_id=record["msgId"],
                    question=record["question"],
                    answer=record["answer"],
                    citations=record["citations"],
                    agent_name=record["agentName"],
                    channel_code=record["channelCode"],
                    role_snapshot=record["role"],
                    views=0,
                )
            )
        record["reused"] = False
        return record

    with _lock:
        data = _read_shares()
        data["shares"][share_id] = record
        _write_shares(data)
    record["reused"] = False
    return dict(record)


def get_share(share_id: str) -> dict | None:
    if _mysql():
        with db_session() as conn:
            row = conn.execute(
                select(M.QaShare).where(M.QaShare.share_id == share_id)
            ).first()
        return _share_row(row) if row else None
    with _lock:
        raw = _read_shares()["shares"].get(share_id)
        return _share_dict(raw) if isinstance(raw, dict) else None


def increment_views(share_id: str) -> None:
    """分享页打开计数；失败只记日志，不影响页面展示。"""
    try:
        if _mysql():
            with db_transaction() as conn:
                conn.execute(
                    update(M.QaShare)
                    .where(M.QaShare.share_id == share_id)
                    .values(views=M.QaShare.views + 1)
                )
            return
        with _lock:
            data = _read_shares()
            raw = data["shares"].get(share_id)
            if not isinstance(raw, dict):
                return
            raw["views"] = int(raw.get("views") or 0) + 1
            _write_shares(data)
    except Exception as exc:  # noqa: BLE001 - 计数失败不能阻断分享页
        logger.warning("分享浏览量更新失败（%s）：%s", share_id, exc)


# ================================================================== 反馈
def find_feedback_by_message(session_id: str, msg_id: int) -> dict | None:
    if _mysql():
        with db_session() as conn:
            row = conn.execute(
                select(M.QaFeedback).where(
                    M.QaFeedback.session_id == session_id,
                    M.QaFeedback.msg_id == int(msg_id),
                )
            ).first()
        return _feedback_row(row) if row else None
    with _lock:
        for raw in _read_feedback()["items"].values():
            if not isinstance(raw, dict):
                continue
            if raw.get("sessionId") == session_id and raw.get("msgId") == int(msg_id):
                return _feedback_dict(raw)
    return None


def upsert_feedback(
    *,
    rating: int,
    share_id: str = "",
    session_id: str = "",
    msg_id: int | None = None,
    reason_code: str = "",
    reason: str = "",
    question: str = "",
    answer_excerpt: str = "",
    role: str = "",
    channel_code: str = "",
) -> dict:
    """写入反馈；同一消息再次反馈时覆盖评价与原因，并重置处理状态。"""
    rating = RATING_LIKE if int(rating) > 0 else RATING_DISLIKE
    values = {
        "shareId": share_id[:24],
        "sessionId": (session_id or "")[:64],
        "msgId": int(msg_id) if msg_id is not None else None,
        "rating": rating,
        "reasonCode": (reason_code or "")[:32] if rating == RATING_DISLIKE else "",
        "reason": (reason or "")[:512],
        "question": (question or "")[:1024],
        "answerExcerpt": (answer_excerpt or "")[:512],
        "role": (role or "")[:64],
        "channelCode": (channel_code or "")[:64],
    }
    now = datetime.now()

    if _mysql():
        with db_transaction() as conn:
            existing = None
            if session_id and msg_id is not None:
                existing = conn.execute(
                    select(M.QaFeedback.feedback_id).where(
                        M.QaFeedback.session_id == session_id,
                        M.QaFeedback.msg_id == int(msg_id),
                    )
                ).first()
            if existing:
                feedback_id = int(existing[0])
                conn.execute(
                    update(M.QaFeedback)
                    .where(M.QaFeedback.feedback_id == feedback_id)
                    .values(
                        share_id=values["shareId"],
                        rating=values["rating"],
                        reason_code=values["reasonCode"],
                        reason=values["reason"],
                        question=values["question"],
                        answer_excerpt=values["answerExcerpt"],
                        role_snapshot=values["role"],
                        channel_code=values["channelCode"],
                        handled=0,
                        handle_note="",
                        handled_at=None,
                        created_at=now,
                    )
                )
            else:
                res = conn.execute(
                    insert(M.QaFeedback).values(
                        share_id=values["shareId"],
                        session_id=values["sessionId"],
                        msg_id=values["msgId"],
                        rating=values["rating"],
                        reason_code=values["reasonCode"],
                        reason=values["reason"],
                        question=values["question"],
                        answer_excerpt=values["answerExcerpt"],
                        role_snapshot=values["role"],
                        channel_code=values["channelCode"],
                        handled=0,
                        created_at=now,
                    )
                )
                feedback_id = int(res.inserted_primary_key[0])
        return get_feedback(feedback_id) or {}

    with _lock:
        data = _read_feedback()
        existing = None
        if session_id and msg_id is not None:
            for raw in data["items"].values():
                if not isinstance(raw, dict):
                    continue
                if raw.get("sessionId") == session_id and raw.get("msgId") == int(msg_id):
                    existing = raw
                    break
        if existing is None:
            feedback_id = int(data.get("nextId") or 1)
            data["nextId"] = feedback_id + 1
            existing = {"feedbackId": feedback_id, "createdAt": _now()}
            data["items"][str(feedback_id)] = existing
        else:
            feedback_id = int(existing.get("feedbackId") or 0)
        existing.update(values)
        existing["handled"] = 0
        existing["handleNote"] = ""
        existing["handledAt"] = None
        existing["createdAt"] = _now()
        _write_feedback(data)
        return _feedback_dict(existing)


def get_feedback(feedback_id: int) -> dict | None:
    if _mysql():
        with db_session() as conn:
            row = conn.execute(
                select(M.QaFeedback).where(M.QaFeedback.feedback_id == int(feedback_id))
            ).first()
        return _feedback_row(row) if row else None
    with _lock:
        raw = _read_feedback()["items"].get(str(int(feedback_id)))
        return _feedback_dict(raw) if isinstance(raw, dict) else None


def update_feedback(
    feedback_id: int, *, handled: bool, note: str = ""
) -> dict | None:
    now = datetime.now()
    if _mysql():
        with db_transaction() as conn:
            row = conn.execute(
                select(M.QaFeedback.feedback_id).where(
                    M.QaFeedback.feedback_id == int(feedback_id)
                )
            ).first()
            if row is None:
                return None
            conn.execute(
                update(M.QaFeedback)
                .where(M.QaFeedback.feedback_id == int(feedback_id))
                .values(
                    handled=1 if handled else 0,
                    handle_note=(note or "")[:512],
                    handled_at=now if handled else None,
                )
            )
        return get_feedback(feedback_id)

    with _lock:
        data = _read_feedback()
        raw = data["items"].get(str(int(feedback_id)))
        if not isinstance(raw, dict):
            return None
        raw["handled"] = 1 if handled else 0
        raw["handleNote"] = (note or "")[:512]
        raw["handledAt"] = now.isoformat(timespec="seconds") if handled else None
        _write_feedback(data)
        return _feedback_dict(raw)


def _parse_day(raw: str | None, *, end: bool = False) -> datetime | None:
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
        logger.warning("反馈筛选：忽略无法解析的时间参数 %r", raw)
        return None


def _feedback_where(
    rating: int | None,
    handled: int | None,
    keyword: str | None,
    date_from: str | None,
    date_to: str | None,
) -> list:
    conds = []
    if rating in (RATING_LIKE, RATING_DISLIKE):
        conds.append(M.QaFeedback.rating == rating)
    if handled in (0, 1):
        conds.append(M.QaFeedback.handled == handled)
    df = _parse_day(date_from)
    dt = _parse_day(date_to, end=True)
    if df is not None:
        conds.append(M.QaFeedback.created_at >= df)
    if dt is not None:
        conds.append(M.QaFeedback.created_at <= dt)
    if keyword and keyword.strip():
        needle = f"%{keyword.strip()}%"
        conds.append(
            M.QaFeedback.question.like(needle)
            | M.QaFeedback.reason.like(needle)
            | M.QaFeedback.answer_excerpt.like(needle)
        )
    return conds


def list_feedback(
    *,
    rating: int | None = None,
    handled: int | None = None,
    keyword: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    page = max(1, int(page or 1))
    page_size = max(1, min(EXPORT_LIMIT, int(page_size or 20)))

    if _mysql():
        conds = _feedback_where(rating, handled, keyword, date_from, date_to)
        with db_session() as conn:
            base = select(func.count()).select_from(M.QaFeedback)
            if conds:
                base = base.where(*conds)
            total = int(conn.execute(base).scalar() or 0)
            rows = conn.execute(
                select(M.QaFeedback)
                .where(*conds)
                .order_by(M.QaFeedback.feedback_id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
            likes = int(
                conn.execute(
                    select(func.count())
                    .select_from(M.QaFeedback)
                    .where(*conds, M.QaFeedback.rating == RATING_LIKE)
                ).scalar()
                or 0
            )
            dislikes = int(
                conn.execute(
                    select(func.count())
                    .select_from(M.QaFeedback)
                    .where(*conds, M.QaFeedback.rating == RATING_DISLIKE)
                ).scalar()
                or 0
            )
            pending = int(
                conn.execute(
                    select(func.count())
                    .select_from(M.QaFeedback)
                    .where(*conds, M.QaFeedback.handled == 0)
                ).scalar()
                or 0
            )
        items = [_feedback_row(row) for row in rows]
    else:
        df = _parse_day(date_from)
        dt = _parse_day(date_to, end=True)
        needle = (keyword or "").strip().lower()
        with _lock:
            rows = [
                _feedback_dict(raw)
                for raw in _read_feedback()["items"].values()
                if isinstance(raw, dict)
            ]
        filtered = []
        for item in rows:
            if rating in (RATING_LIKE, RATING_DISLIKE) and item["rating"] != rating:
                continue
            if handled in (0, 1) and item["handled"] != handled:
                continue
            if needle:
                haystack = f"{item['question']} {item['reason']} {item['answerExcerpt']}".lower()
                if needle not in haystack:
                    continue
            moment = _parse_day(item.get("createdAt"))
            if df is not None and (moment is None or moment < df):
                continue
            if dt is not None and (moment is None or moment > dt):
                continue
            filtered.append(item)
        filtered.sort(key=lambda x: x["feedbackId"], reverse=True)
        total = len(filtered)
        likes = sum(1 for x in filtered if x["rating"] == RATING_LIKE)
        dislikes = sum(1 for x in filtered if x["rating"] == RATING_DISLIKE)
        pending = sum(1 for x in filtered if x["handled"] == 0)
        start = (page - 1) * page_size
        items = filtered[start:start + page_size]

    return {
        "items": items,
        "total": total,
        "page": page,
        "pageSize": page_size,
        "pages": (total + page_size - 1) // page_size if total else 0,
        "summary": {
            "total": total,
            "likes": likes,
            "dislikes": dislikes,
            "pending": pending,
            "likeRate": round(likes / total, 4) if total else None,
        },
    }


def share_feedback_counts(share_id: str) -> dict:
    if _mysql():
        with db_session() as conn:
            rows = conn.execute(
                select(M.QaFeedback.rating, func.count())
                .where(M.QaFeedback.share_id == share_id)
                .group_by(M.QaFeedback.rating)
            ).all()
        counts = {int(r[0]): int(r[1]) for r in rows}
    else:
        with _lock:
            items = [
                _feedback_dict(raw)
                for raw in _read_feedback()["items"].values()
                if isinstance(raw, dict) and raw.get("shareId") == share_id
            ]
        counts = {RATING_LIKE: 0, RATING_DISLIKE: 0}
        for item in items:
            counts[item["rating"]] = counts.get(item["rating"], 0) + 1
    return {
        "likes": counts.get(RATING_LIKE, 0),
        "dislikes": counts.get(RATING_DISLIKE, 0),
    }


def filter_options() -> dict:
    """管理端筛选项：渠道 + 点踩原因（从已有数据收集，避免无意义空列表）。"""
    if _mysql():
        with db_session() as conn:
            channels = [
                str(r[0])
                for r in conn.execute(
                    select(M.QaFeedback.channel_code)
                    .where(M.QaFeedback.channel_code != "")
                    .distinct()
                    .order_by(M.QaFeedback.channel_code)
                ).all()
            ]
    else:
        with _lock:
            channels = sorted(
                {
                    str(raw.get("channelCode") or "")
                    for raw in _read_feedback()["items"].values()
                    if isinstance(raw, dict) and raw.get("channelCode")
                }
            )
    return {
        "channels": channels,
        "reasons": [{"code": code, "label": label} for code, label in REASON_CODES.items()],
        "ratings": [
            {"value": RATING_LIKE, "label": RATING_LABELS[RATING_LIKE]},
            {"value": RATING_DISLIKE, "label": RATING_LABELS[RATING_DISLIKE]},
        ],
    }
