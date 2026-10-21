"""门禁通行流水本地同步库（任务 4.4 / P-DQ-04、I-04）。

两种后端，对外接口一致：

* **MySQL**：``access_event``（见仓库根 ``database/schema.sql``）；
* **JSON**：``data/access_events.json``（进程内锁 + 原子写）。

同步采用**整窗替换**：先完整拉取上游时间窗，成功后再替换本地库，避免上游
半途失败导致数据缺口。同步与分析都在本地完成，外部平台只承担数据供给。
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, insert, or_, select

from app.config import settings
from app.db import models as M
from app.db.session import db_session, db_transaction

logger = logging.getLogger(__name__)

ACCESS_EVENTS_PATH: Path = settings.base_dir / "data" / "access_events.json"
_lock = threading.RLock()

_FIELDS = (
    "event_no",
    "event_time",
    "direction",
    "campus_id",
    "campus_name",
    "college_name",
    "gate_id",
    "gate_name",
    "user_no",
    "user_name",
    "role",
    "abnormal",
    "abnormal_reason",
)

_COLUMNS = (
    M.AccessEvent.event_id,
    M.AccessEvent.event_no,
    M.AccessEvent.event_time,
    M.AccessEvent.direction,
    M.AccessEvent.campus_id,
    M.AccessEvent.campus_name,
    M.AccessEvent.college_name,
    M.AccessEvent.gate_id,
    M.AccessEvent.gate_name,
    M.AccessEvent.user_no,
    M.AccessEvent.user_name,
    M.AccessEvent.role,
    M.AccessEvent.abnormal,
    M.AccessEvent.abnormal_reason,
)


def _mysql() -> bool:
    return settings.store_label == "mysql"


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat(timespec="seconds")


def _row_event(row) -> dict:
    return {
        "eventId": int(row[0]),
        "eventNo": row[1] or "",
        "eventTime": _iso(row[2]),
        "direction": "in" if int(row[3] or 0) == 1 else "out",
        "campusId": int(row[4] or 0),
        "campusName": row[5] or "",
        "collegeName": row[6] or "",
        "gateId": int(row[7] or 0),
        "gateName": row[8] or "",
        "userNo": row[9] or "",
        "userName": row[10] or "",
        "role": row[11] or "",
        "abnormal": bool(int(row[12] or 0)),
        "abnormalReason": row[13] or "",
    }


def _json_record(raw: dict) -> dict:
    return {
        "eventId": int(raw.get("eventId") or 0),
        "eventNo": raw.get("eventNo") or "",
        "eventTime": _iso(raw.get("eventTime")) or "",
        "direction": "in" if raw.get("direction") in ("in", 1, "1", True) else "out",
        "campusId": int(raw.get("campusId") or 0),
        "campusName": raw.get("campusName") or "",
        "collegeName": raw.get("collegeName") or "",
        "gateId": int(raw.get("gateId") or 0),
        "gateName": raw.get("gateName") or "",
        "userNo": raw.get("userNo") or "",
        "userName": raw.get("userName") or "",
        "role": raw.get("role") or "",
        "abnormal": bool(raw.get("abnormal")),
        "abnormalReason": raw.get("abnormalReason") or "",
    }


def _read_json() -> dict:
    if not ACCESS_EVENTS_PATH.is_file():
        return {"events": [], "provider": "", "syncedAt": None, "days": 0}
    try:
        data = json.loads(ACCESS_EVENTS_PATH.read_text(encoding="utf-8"))
        if not isinstance(data.get("events"), list):
            data["events"] = []
        return data
    except Exception:  # noqa: BLE001 - 文件损坏时重建，避免通行分析整体不可用
        logger.warning("门禁流水文件损坏，已重建：%s", ACCESS_EVENTS_PATH)
        return {"events": [], "provider": "", "syncedAt": None, "days": 0}


def _write_json(data: dict) -> None:
    ACCESS_EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = ACCESS_EVENTS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(ACCESS_EVENTS_PATH)


def _event_values(raw: dict) -> dict:
    return {
        "event_no": str(raw.get("eventNo") or "")[:64],
        "event_time": _as_dt(raw.get("eventTime")),
        "direction": 1 if raw.get("direction") == "in" else 0,
        "campus_id": int(raw.get("campusId") or 0),
        "campus_name": str(raw.get("campusName") or "")[:64],
        "college_name": str(raw.get("collegeName") or "")[:128],
        "gate_id": int(raw.get("gateId") or 0),
        "gate_name": str(raw.get("gateName") or "")[:64],
        "user_no": str(raw.get("userNo") or "")[:64],
        "user_name": str(raw.get("userName") or "")[:64],
        "role": str(raw.get("role") or "")[:32],
        "abnormal": 1 if raw.get("abnormal") else 0,
        "abnormal_reason": str(raw.get("abnormalReason") or "")[:64],
    }


# ================================================================== 同步写入
def replace_events(events: list[dict], *, provider: str = "", days: int = 0) -> dict:
    """整窗替换本地库。调用方须先完成上游拉取与校验。"""
    records = [_json_record(raw) for raw in events]
    for idx, record in enumerate(records, start=1):
        record["eventId"] = idx
    now = datetime.now()
    synced_at = now.isoformat(timespec="seconds")

    if not _mysql():
        with _lock:
            _write_json({
                "events": records,
                "provider": provider,
                "syncedAt": synced_at,
                "days": int(days or 0),
            })
        return {"inserted": len(records), "provider": provider, "syncedAt": synced_at}

    with db_transaction() as conn:
        conn.execute(sa_delete(M.AccessEvent))
        for start in range(0, len(records), 1000):
            chunk = []
            for raw in records[start : start + 1000]:
                values = _event_values(raw)
                values["created_at"] = now
                chunk.append(values)
            if chunk:
                conn.execute(insert(M.AccessEvent), chunk)
    return {"inserted": len(records), "provider": provider, "syncedAt": synced_at}


# ================================================================== 查询
def _in_window(moment: datetime, start: datetime | None, end: datetime | None) -> bool:
    return (start is None or moment >= start) and (end is None or moment <= end)


def _match(raw: dict, filters: dict) -> bool:
    moment = _as_dt(raw.get("eventTime"))
    if moment is None:
        return False
    if not _in_window(moment, filters.get("date_from"), filters.get("date_to")):
        return False
    if filters.get("campus_id") is not None and int(raw.get("campusId") or 0) != int(filters["campus_id"]):
        return False
    if filters.get("college") and (raw.get("collegeName") or "") != filters["college"]:
        return False
    if filters.get("gate_id") is not None and int(raw.get("gateId") or 0) != int(filters["gate_id"]):
        return False
    if filters.get("role") and (raw.get("role") or "") != filters["role"]:
        return False
    if filters.get("abnormal") is not None and bool(raw.get("abnormal")) != bool(filters["abnormal"]):
        return False
    if filters.get("direction") and (raw.get("direction") or "") != filters["direction"]:
        return False
    keyword = str(filters.get("q") or "").strip().lower()
    if keyword:
        haystack = " ".join(
            str(raw.get(key) or "")
            for key in ("userNo", "userName", "campusName", "collegeName", "gateName", "abnormalReason")
        ).lower()
        if keyword not in haystack:
            return False
    return True


def _as_dt(value) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def list_events(filters: dict | None = None) -> list[dict]:
    """按筛选条件返回流水（升序），分析层再做聚合。"""
    filters = filters or {}
    if not _mysql():
        with _lock:
            events = [_json_record(raw) for raw in _read_json().get("events", [])]
        return [event for event in events if _match(event, filters)]

    conditions = []
    if filters.get("date_from") is not None:
        conditions.append(M.AccessEvent.event_time >= filters["date_from"])
    if filters.get("date_to") is not None:
        conditions.append(M.AccessEvent.event_time <= filters["date_to"])
    if filters.get("campus_id") is not None:
        conditions.append(M.AccessEvent.campus_id == int(filters["campus_id"]))
    if filters.get("college"):
        conditions.append(M.AccessEvent.college_name == filters["college"])
    if filters.get("gate_id") is not None:
        conditions.append(M.AccessEvent.gate_id == int(filters["gate_id"]))
    if filters.get("role"):
        conditions.append(M.AccessEvent.role == filters["role"])
    if filters.get("abnormal") is not None:
        conditions.append(M.AccessEvent.abnormal == (1 if filters["abnormal"] else 0))
    if filters.get("direction"):
        conditions.append(M.AccessEvent.direction == (1 if filters["direction"] == "in" else 0))
    keyword = str(filters.get("q") or "").strip()
    if keyword:
        like = f"%{keyword}%"
        conditions.append(
            or_(
                M.AccessEvent.user_no.like(like),
                M.AccessEvent.user_name.like(like),
                M.AccessEvent.campus_name.like(like),
                M.AccessEvent.college_name.like(like),
                M.AccessEvent.gate_name.like(like),
                M.AccessEvent.abnormal_reason.like(like),
            )
        )
    with db_session() as conn:
        stmt = select(*_COLUMNS)
        if conditions:
            stmt = stmt.where(*conditions)
        rows = conn.execute(stmt.order_by(M.AccessEvent.event_time, M.AccessEvent.event_id)).all()
    return [_row_event(row) for row in rows]


def query_events(
    filters: dict | None = None,
    *,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    events = list_events(filters)
    events.sort(key=lambda item: (item.get("eventTime") or "", item.get("eventId") or 0), reverse=True)
    total = len(events)
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 20), 200))
    start = (page - 1) * page_size
    return {
        "items": events[start : start + page_size],
        "total": total,
        "page": page,
        "pageSize": page_size,
    }


def sync_meta() -> dict:
    if not _mysql():
        data = _read_json()
        events = data.get("events") or []
        last_event = max((str(raw.get("eventTime") or "") for raw in events), default="")
        return {
            "provider": data.get("provider") or "",
            "syncedAt": data.get("syncedAt"),
            "days": int(data.get("days") or 0),
            "totalEvents": len(events),
            "lastEventAt": last_event or None,
        }
    with db_session() as conn:
        row = conn.execute(
            select(
                func.count(M.AccessEvent.event_id),
                func.max(M.AccessEvent.created_at),
                func.max(M.AccessEvent.event_time),
            )
        ).first()
    return {
        "provider": "",
        "syncedAt": _iso(row[1]) if row else None,
        "days": 0,
        "totalEvents": int(row[0] or 0) if row else 0,
        "lastEventAt": _iso(row[2]) if row else None,
    }


def event_days() -> list[date]:
    """本地库覆盖的日期（用于同步提示，不参与筛选）。"""
    events = list_events({})
    days = {_as_dt(event.get("eventTime")).date() for event in events if _as_dt(event.get("eventTime"))}
    return sorted(days)
