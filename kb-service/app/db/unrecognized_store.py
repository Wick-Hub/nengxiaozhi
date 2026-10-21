"""未识别问题池存储（任务 3.11）。

两种后端对外一致：

* JSON：``kb-service/data/unrecognized.json``，进程内锁 + 原子写；
* MySQL：``unrecognized_question`` 表（3.11 迁移补齐归因/聚合列）。

入池触发在问答链路完成（见 :mod:`app.core.pipeline` 与
:func:`app.db.chatlog.save_qa`）：空召回、模型拒答、低置信度三种来源。
管理端只读取池表，不重复扫描会话。
"""

from __future__ import annotations

import json
import logging
import threading
import unicodedata
from datetime import datetime
from pathlib import Path

from sqlalchemy import insert, select, update

from app.config import settings
from app.db import models as M
from app.db.session import db_transaction

logger = logging.getLogger(__name__)

POOL_PATH = settings.base_dir / "data" / "unrecognized.json"
_lock = threading.RLock()
MAX_SCAN = 5000

SOURCE_WEAK_RECALL = "weak_recall"
SOURCE_REFUSED = "refused"
SOURCE_LOW_CONFIDENCE = "low_confidence"

STATE_PENDING = 0
STATE_FED_BACK = 1
STATE_IGNORED = 2

STATE_LABELS = {STATE_PENDING: "待处理", STATE_FED_BACK: "已反哺", STATE_IGNORED: "已忽略"}
SOURCE_LABELS = {
    SOURCE_WEAK_RECALL: "弱召回",
    SOURCE_REFUSED: "模型拒答",
    SOURCE_LOW_CONFIDENCE: "低置信度",
}

_PUNCT = set(
    " \t\r\n，。！？；：、,.!?;:'\"“”‘’()（）[]【】<>《》-—_/\\|~`·"
)
_UNSET = object()


def _mysql() -> bool:
    return settings.store_label == "mysql"


def normalize_question(text: str) -> str:
    """问题归一化：全半角统一、小写、去空白与标点，用于聚合与聚类。"""
    raw = unicodedata.normalize("NFKC", str(text or "")).lower()
    kept = "".join(ch for ch in raw if ch not in _PUNCT)
    return (kept or raw.strip())[:255]


def _iso(value) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value)


def _read_all() -> dict:
    if not POOL_PATH.is_file():
        return {"items": []}
    try:
        data = json.loads(POOL_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("未识别问题池读取失败：%s", exc)
        return {"items": []}
    items = data.get("items")
    return {"items": items if isinstance(items, list) else []}


def _write_all(data: dict) -> None:
    POOL_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = POOL_PATH.with_name(POOL_PATH.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(POOL_PATH)


def _item_to_dict(raw: dict) -> dict:
    return {
        "id": int(raw.get("id") or 0),
        "sessionId": raw.get("sessionId"),
        "msgId": raw.get("msgId"),
        "question": raw.get("question") or "",
        "questionKey": raw.get("questionKey") or normalize_question(raw.get("question") or ""),
        "confidence": raw.get("confidence"),
        "source": raw.get("source") or "",
        "role": raw.get("role") or "",
        "channelCode": raw.get("channelCode") or "",
        "agentId": raw.get("agentId") or "",
        "dept": raw.get("dept") or "",
        "clusterId": raw.get("clusterId"),
        "state": int(raw.get("state") or 0),
        "handlerId": raw.get("handlerId"),
        "newDocId": raw.get("newDocId"),
        "newChunkId": raw.get("newChunkId"),
        "createdAt": raw.get("createdAt"),
        "handledAt": raw.get("handledAt"),
    }


def _row_to_dict(row) -> dict:
    return {
        "id": int(row.id),
        "sessionId": row.session_id,
        "msgId": row.msg_id,
        "question": row.question or "",
        "questionKey": row.question_key or normalize_question(row.question or ""),
        "confidence": float(row.confidence) if row.confidence is not None else None,
        "source": row.source or "",
        "role": row.role_snapshot or "",
        "channelCode": row.channel_code or "",
        "agentId": row.agent_id or "",
        "dept": row.dept_name or "",
        "clusterId": row.cluster_id,
        "state": int(row.handle_state or 0),
        "handlerId": row.handler_id,
        "newDocId": row.new_doc_id,
        "newChunkId": row.new_chunk_id,
        "createdAt": _iso(row.created_at),
        "handledAt": _iso(row.handled_at),
    }


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
    """写入一条未识别记录；关闭开关或问题为空时返回 ``None``。

    同一会话同一条 AI 消息（``session_id + msg_id``）重复调用只保留一条，
    避免问答落库与拒答落库两条路径重复入池。
    """
    if not settings.unrecognized_enabled:
        return None
    text = str(question or "").strip()
    if not text:
        return None
    key = normalize_question(text)
    now = datetime.now()
    conf = round(float(confidence), 4) if confidence is not None else None

    if _mysql():
        with db_transaction() as conn:
            if msg_id is not None:
                dup = conn.execute(
                    select(M.UnrecognizedQuestion.id).where(
                        M.UnrecognizedQuestion.session_id == session_id,
                        M.UnrecognizedQuestion.msg_id == int(msg_id),
                    )
                ).first()
                if dup:
                    return int(dup[0])
            res = conn.execute(
                insert(M.UnrecognizedQuestion).values(
                    session_id=session_id,
                    msg_id=int(msg_id) if msg_id is not None else None,
                    question=text[:1024],
                    question_key=key,
                    confidence=conf,
                    source=source[:20],
                    role_snapshot=(role or "")[:32],
                    channel_code=(channel_code or "")[:64],
                    agent_id=(str(agent_id) if agent_id is not None else "")[:64],
                    dept_name=(dept or "")[:64],
                    handle_state=STATE_PENDING,
                    created_at=now,
                )
            )
            return int(res.inserted_primary_key[0])

    with _lock:
        data = _read_all()
        if msg_id is not None:
            for raw in data["items"]:
                if raw.get("sessionId") == session_id and raw.get("msgId") == msg_id:
                    return int(raw.get("id") or 0)
        rid = max((int(x.get("id") or 0) for x in data["items"]), default=0) + 1
        data["items"].append(
            {
                "id": rid,
                "sessionId": session_id,
                "msgId": msg_id,
                "question": text[:1024],
                "questionKey": key,
                "confidence": conf,
                "source": source,
                "role": role or "",
                "channelCode": channel_code or "",
                "agentId": str(agent_id) if agent_id is not None else "",
                "dept": dept or "",
                "clusterId": None,
                "state": STATE_PENDING,
                "handlerId": None,
                "newDocId": None,
                "newChunkId": None,
                "createdAt": now.isoformat(timespec="seconds"),
                "handledAt": None,
            }
        )
        _write_all(data)
        return rid


def list_records(limit: int = MAX_SCAN) -> list[dict]:
    """按时间倒序返回最近记录（聚合与筛选在服务层做）。"""
    if _mysql():
        with db_transaction() as conn:
            rows = conn.execute(
                select(M.UnrecognizedQuestion)
                .order_by(M.UnrecognizedQuestion.id.desc())
                .limit(int(limit))
            ).all()
        return [_row_to_dict(row) for row in rows]
    with _lock:
        items = list(reversed(_read_all()["items"]))[: int(limit)]
    return [_item_to_dict(raw) for raw in items]


def get_record(record_id: int) -> dict | None:
    if _mysql():
        with db_transaction() as conn:
            row = conn.execute(
                select(M.UnrecognizedQuestion).where(
                    M.UnrecognizedQuestion.id == int(record_id)
                )
            ).first()
        return _row_to_dict(row) if row else None
    with _lock:
        for raw in _read_all()["items"]:
            if int(raw.get("id") or 0) == int(record_id):
                return _item_to_dict(raw)
    return None


def update_records(
    record_ids: list[int],
    *,
    state: int | None = None,
    dept: str | None = None,
    handler_id=None,
    new_doc_id=_UNSET,
    new_chunk_id=_UNSET,
) -> int:
    """批量更新状态 / 归属部门 / 反哺产物；返回实际更新的记录数。"""
    ids = sorted({int(i) for i in record_ids if str(i).strip().isdigit()})
    if not ids:
        return 0
    now = datetime.now()

    if _mysql():
        values: dict = {}
        if state is not None:
            values["handle_state"] = int(state)
            values["handled_at"] = None if int(state) == STATE_PENDING else now
        if dept is not None:
            values["dept_name"] = dept[:64]
        if handler_id is not None:
            values["handler_id"] = handler_id
        if new_doc_id is not _UNSET:
            values["new_doc_id"] = new_doc_id
        if new_chunk_id is not _UNSET:
            values["new_chunk_id"] = new_chunk_id
        if not values:
            return 0
        with db_transaction() as conn:
            res = conn.execute(
                update(M.UnrecognizedQuestion)
                .where(M.UnrecognizedQuestion.id.in_(ids))
                .values(**values)
            )
        return int(res.rowcount or 0)

    with _lock:
        data = _read_all()
        changed = 0
        for raw in data["items"]:
            if int(raw.get("id") or 0) not in ids:
                continue
            if state is not None:
                raw["state"] = int(state)
                raw["handledAt"] = (
                    None if int(state) == STATE_PENDING else now.isoformat(timespec="seconds")
                )
            if dept is not None:
                raw["dept"] = dept[:64]
            if handler_id is not None:
                raw["handlerId"] = handler_id
            if new_doc_id is not _UNSET:
                raw["newDocId"] = new_doc_id
            if new_chunk_id is not _UNSET:
                raw["newChunkId"] = new_chunk_id
            changed += 1
        if changed:
            _write_all(data)
        return changed
