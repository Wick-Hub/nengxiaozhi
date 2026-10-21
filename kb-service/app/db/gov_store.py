"""导办事项与免登录票据存储（任务 4.5 / P-DB-01·02、M-G-01）。

两种后端，对外接口一致：

* **MySQL**：``gov_item`` / ``gov_ticket``（见 ``database/schema.sql``）；
* **JSON**：``data/gov_items.json`` / ``data/gov_tickets.json``（进程内锁 + 原子写）。

同步使用 ``upsert_items``：先按 ``item_id``、再按「事项名称 + 部门」匹配，
重复导入只更新不新增。同步只写传入的字段，因此本地启停与生效时段不会被
上游数据覆盖（这两项是本地运营决策）。

票据只存服务端：对外是一个高熵随机串，身份随票据记录一起落库；消费用
``UPDATE ... WHERE used_at IS NULL``（JSON 侧加锁）保证**一次性**，并发重复
兑换只有一个请求成功。
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import delete as sa_delete
from sqlalchemy import insert, or_, select
from sqlalchemy import update as sa_update

from app.config import settings
from app.db import models as M
from app.db.session import db_session, db_transaction

logger = logging.getLogger(__name__)

GOV_ITEMS_PATH: Path = settings.base_dir / "data" / "gov_items.json"
GOV_TICKETS_PATH: Path = settings.base_dir / "data" / "gov_tickets.json"
_lock = threading.RLock()

ALL_ROLE = "全部"
VISIBLE_ROLES = ("访客", "本科生", "研究生", "教职工", "离退休人员")

_ITEM_COLUMNS = (
    M.GovItem.item_id,
    M.GovItem.item_name,
    M.GovItem.dept_id,
    M.GovItem.hall_url,
    M.GovItem.need_ticket,
    M.GovItem.conditions,
    M.GovItem.materials,
    M.GovItem.keyword_tags,
    M.GovItem.visible_roles,
    M.GovItem.status,
    M.GovItem.effective_from,
    M.GovItem.effective_to,
    M.GovItem.updated_at,
)

_ITEM_FIELD_MAP = {
    "item_name": "itemName",
    "dept_id": "deptId",
    "hall_url": "hallUrl",
    "need_ticket": "needTicket",
    "conditions": "conditions",
    "materials": "materials",
    "keyword_tags": "keywordTags",
    "visible_roles": "visibleRoles",
    "status": "status",
    "effective_from": "effectiveFrom",
    "effective_to": "effectiveTo",
}

_TICKET_FIELD_MAP = {
    "item_id": "itemId",
    "user_no": "userNo",
    "user_name": "userName",
    "role": "role",
    "issued_at": "issuedAt",
    "expires_at": "expiresAt",
    "used_at": "usedAt",
}


def _mysql() -> bool:
    return settings.store_label == "mysql"


def _now() -> datetime:
    return datetime.now()


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat(timespec="seconds")


def _as_dt(value) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def split_list(raw, *, all_value: str | None = None) -> list[str]:
    if isinstance(raw, list):
        values = raw
    else:
        values = str(raw or "").replace("、", ",").split(",")
    out: list[str] = []
    for value in values:
        name = str(value).strip()
        if name and name not in out:
            out.append(name)
    if all_value and (not out or all_value in out):
        return [all_value]
    return out


def join_list(raw) -> str:
    return ",".join(split_list(raw))


def split_roles(raw) -> list[str]:
    return split_list(raw, all_value=ALL_ROLE)


def join_roles(raw) -> str:
    return ",".join(split_roles(raw))


# ================================================================== JSON 后端
def _read_items() -> dict:
    if not GOV_ITEMS_PATH.is_file():
        return {"items": {}, "nextId": 1}
    try:
        data = json.loads(GOV_ITEMS_PATH.read_text(encoding="utf-8"))
        if not isinstance(data.get("items"), dict):
            data["items"] = {}
        data["nextId"] = int(data.get("nextId") or len(data["items"]) + 1)
        return data
    except Exception:  # noqa: BLE001 - 文件损坏时重建，避免导办整体不可用
        logger.warning("导办事项文件损坏，已重建：%s", GOV_ITEMS_PATH)
        return {"items": {}, "nextId": 1}


def _write_items(data: dict) -> None:
    GOV_ITEMS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = GOV_ITEMS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(GOV_ITEMS_PATH)


def _read_tickets() -> dict:
    if not GOV_TICKETS_PATH.is_file():
        return {"tickets": {}}
    try:
        data = json.loads(GOV_TICKETS_PATH.read_text(encoding="utf-8"))
        if not isinstance(data.get("tickets"), dict):
            data["tickets"] = {}
        return data
    except Exception:  # noqa: BLE001
        logger.warning("导办票据文件损坏，已重建：%s", GOV_TICKETS_PATH)
        return {"tickets": {}}


def _write_tickets(data: dict) -> None:
    GOV_TICKETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = GOV_TICKETS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(GOV_TICKETS_PATH)


def _json_item(raw: dict) -> dict:
    return {
        "itemId": int(raw.get("itemId") or 0),
        "itemName": raw.get("itemName") or "",
        "deptId": int(raw.get("deptId") or 0),
        "hallUrl": raw.get("hallUrl") or "",
        "needTicket": bool(int(raw.get("needTicket", 1))),
        "conditions": raw.get("conditions") or "",
        "materials": raw.get("materials") or "",
        "keywordTags": split_list(raw.get("keywordTags")),
        "visibleRoles": split_roles(raw.get("visibleRoles")),
        "status": 1 if int(raw.get("status", 1)) else 0,
        "effectiveFrom": raw.get("effectiveFrom"),
        "effectiveTo": raw.get("effectiveTo"),
        "updatedAt": raw.get("updatedAt"),
    }


def _row_item(row) -> dict:
    return {
        "itemId": int(row[0]),
        "itemName": row[1] or "",
        "deptId": int(row[2] or 0),
        "hallUrl": row[3] or "",
        "needTicket": bool(int(row[4] or 0)),
        "conditions": row[5] or "",
        "materials": row[6] or "",
        "keywordTags": split_list(row[7]),
        "visibleRoles": split_roles(row[8]),
        "status": int(row[9] or 0),
        "effectiveFrom": _iso(row[10]),
        "effectiveTo": _iso(row[11]),
        "updatedAt": _iso(row[12]),
    }


def _item_values(fields: dict) -> dict:
    values: dict = {}
    for key, value in (fields or {}).items():
        if key not in _ITEM_FIELD_MAP:
            continue
        if key == "visible_roles":
            value = join_roles(value)
        elif key == "keyword_tags":
            value = join_list(value)
        elif key == "need_ticket":
            value = 1 if value else 0
        elif key == "status":
            value = 1 if value else 0
        elif key in ("effective_from", "effective_to"):
            value = _as_dt(value)
        elif key == "dept_id":
            value = int(value or 0)
        elif key in ("item_name", "hall_url"):
            value = str(value or "").strip()
        elif key in ("conditions", "materials"):
            value = str(value or "")
        values[key] = value
    return values


def _json_record(item_id: int, values: dict) -> dict:
    record: dict = {"itemId": item_id}
    for key, camel in _ITEM_FIELD_MAP.items():
        value = values.get(key)
        if key == "visible_roles":
            value = join_roles(value)
        elif key == "keyword_tags":
            value = join_list(value)
        elif key == "need_ticket":
            value = 1 if value else 0
        elif key == "status":
            value = 1 if value else 0
        elif key in ("effective_from", "effective_to"):
            value = _iso(value)
        record[camel] = value
    return record


def _json_patch(values: dict) -> dict:
    """只转换上游实际提供的字段；用于同步更新，避免默认值覆盖本地运营配置。"""
    patch: dict = {}
    for key, camel in _ITEM_FIELD_MAP.items():
        if key not in values:
            continue
        value = values[key]
        if key == "visible_roles":
            value = join_roles(value)
        elif key == "keyword_tags":
            value = join_list(value)
        elif key == "need_ticket":
            value = 1 if value else 0
        elif key == "status":
            value = 1 if value else 0
        elif key in ("effective_from", "effective_to"):
            value = _iso(value)
        patch[camel] = value
    return patch


def _defaults(values: dict) -> dict:
    values.setdefault("item_name", "")
    values.setdefault("dept_id", 0)
    values.setdefault("hall_url", "")
    values.setdefault("need_ticket", 1)
    values.setdefault("conditions", "")
    values.setdefault("materials", "")
    values.setdefault("keyword_tags", "")
    values.setdefault("visible_roles", ALL_ROLE)
    values.setdefault("status", 1)
    values.setdefault("effective_from", None)
    values.setdefault("effective_to", None)
    return values


# ================================================================== 事项查询
def list_items() -> list[dict]:
    if not _mysql():
        raw = _read_items()["items"]
        items = [_json_item(rec) for rec in raw.values() if isinstance(rec, dict)]
        items.sort(key=lambda item: item["itemId"])
        return items
    with db_session() as conn:
        rows = conn.execute(
            select(*_ITEM_COLUMNS).order_by(M.GovItem.item_id)
        ).all()
        return [_row_item(row) for row in rows]


def get_item(item_id: int) -> dict | None:
    if not _mysql():
        record = _read_items()["items"].get(str(int(item_id)))
        return _json_item(record) if isinstance(record, dict) else None
    with db_session() as conn:
        row = conn.execute(
            select(*_ITEM_COLUMNS).where(M.GovItem.item_id == int(item_id))
        ).first()
        return _row_item(row) if row is not None else None


def find_duplicate(item_name: str, dept_id: int, *, exclude_id: int | None = None) -> dict | None:
    for item in list_items():
        if item["itemName"] == item_name and int(item["deptId"]) == int(dept_id):
            if exclude_id is not None and int(item["itemId"]) == int(exclude_id):
                continue
            return item
    return None


# ================================================================== 事项写入
def create_item(**fields) -> int:
    values = _defaults(_item_values(fields))
    now = _now()
    if not _mysql():
        with _lock:
            data = _read_items()
            item_id = int(data.get("nextId") or 1)
            data["nextId"] = item_id + 1
            record = _json_record(item_id, values)
            record["updatedAt"] = _iso(now)
            data["items"][str(item_id)] = record
            _write_items(data)
            return item_id

    with db_transaction() as conn:
        values["updated_at"] = now
        result = conn.execute(insert(M.GovItem).values(**values))
        return int(result.inserted_primary_key[0])


def update_item(item_id: int, **fields) -> bool:
    values = _item_values(fields)
    now = _now()
    if not _mysql():
        with _lock:
            data = _read_items()
            record = data["items"].get(str(int(item_id)))
            if not isinstance(record, dict):
                return False
            patch = _json_record(int(item_id), values)
            patch.pop("itemId", None)
            record.update(patch)
            record["updatedAt"] = _iso(now)
            _write_items(data)
            return True

    with db_transaction() as conn:
        existing = conn.execute(
            select(M.GovItem.item_id).where(M.GovItem.item_id == int(item_id))
        ).first()
        if existing is None:
            return False
        values["updated_at"] = now
        conn.execute(
            sa_update(M.GovItem).where(M.GovItem.item_id == int(item_id)).values(**values)
        )
        return True


def delete_item(item_id: int) -> bool:
    if not _mysql():
        with _lock:
            data = _read_items()
            if str(int(item_id)) not in data["items"]:
                return False
            del data["items"][str(int(item_id))]
            _write_items(data)
            return True
    with db_transaction() as conn:
        existing = conn.execute(
            select(M.GovItem.item_id).where(M.GovItem.item_id == int(item_id))
        ).first()
        if existing is None:
            return False
        conn.execute(sa_delete(M.GovItem).where(M.GovItem.item_id == int(item_id)))
        return True


def upsert_items(items: list[dict]) -> dict:
    """同步导入：``item_id`` 或「名称 + 部门」命中则更新，否则新增。"""
    created = 0
    updated = 0
    now = _now()
    if not _mysql():
        with _lock:
            data = _read_items()
            for raw in items:
                values = _item_values(raw)
                if not values.get("item_name"):
                    continue
                item_id = int(raw.get("item_id") or 0)
                target = data["items"].get(str(item_id)) if item_id else None
                if not isinstance(target, dict):
                    target = None
                    for rec in data["items"].values():
                        if (
                            isinstance(rec, dict)
                            and rec.get("itemName") == values["item_name"]
                            and int(rec.get("deptId") or 0) == int(values.get("dept_id") or 0)
                        ):
                            target = rec
                            break
                if target is None:
                    item_id = int(data.get("nextId") or 1)
                    data["nextId"] = item_id + 1
                    record = _json_record(item_id, _defaults(dict(values)))
                    record["updatedAt"] = _iso(now)
                    data["items"][str(item_id)] = record
                    created += 1
                else:
                    target.update(_json_patch(values))
                    target["updatedAt"] = _iso(now)
                    updated += 1
            _write_items(data)
            return {"created": created, "updated": updated}

    with db_transaction() as conn:
        for raw in items:
            values = _item_values(raw)
            if not values.get("item_name"):
                continue
            item_id = int(raw.get("item_id") or 0)
            row = None
            if item_id:
                row = conn.execute(
                    select(M.GovItem.item_id).where(M.GovItem.item_id == item_id)
                ).first()
            if row is None:
                row = conn.execute(
                    select(M.GovItem.item_id).where(
                        M.GovItem.item_name == values["item_name"],
                        M.GovItem.dept_id == int(values.get("dept_id") or 0),
                    )
                ).first()
            if row is None:
                values["updated_at"] = now
                values.setdefault("need_ticket", 1)
                values.setdefault("visible_roles", ALL_ROLE)
                values.setdefault("status", 1)
                conn.execute(insert(M.GovItem).values(**values))
                created += 1
            else:
                values["updated_at"] = now
                conn.execute(
                    sa_update(M.GovItem)
                    .where(M.GovItem.item_id == int(row[0]))
                    .values(**values)
                )
                updated += 1
        return {"created": created, "updated": updated}


# ================================================================== 票据
def _ticket_dict(raw: dict) -> dict:
    return {
        "ticketId": raw.get("ticketId") or "",
        "itemId": int(raw.get("itemId") or 0),
        "userNo": raw.get("userNo") or "",
        "userName": raw.get("userName") or "",
        "role": raw.get("role") or "",
        "issuedAt": raw.get("issuedAt"),
        "expiresAt": raw.get("expiresAt"),
        "usedAt": raw.get("usedAt"),
    }


def _row_ticket(row) -> dict:
    return {
        "ticketId": row[0] or "",
        "itemId": int(row[1] or 0),
        "userNo": row[2] or "",
        "userName": row[3] or "",
        "role": row[4] or "",
        "issuedAt": _iso(row[5]),
        "expiresAt": _iso(row[6]),
        "usedAt": _iso(row[7]),
    }


def create_ticket(record: dict) -> None:
    values = {
        key: record.get(key)
        for key in ("ticket_id", "item_id", "user_no", "user_name", "role", "issued_at", "expires_at")
    }
    if not _mysql():
        with _lock:
            data = _read_tickets()
            item: dict = {"ticketId": values["ticket_id"]}
            for key, camel in _TICKET_FIELD_MAP.items():
                value = values.get(key)
                item[camel] = _iso(value) if key.endswith("_at") else value
            item["usedAt"] = None
            data["tickets"][str(values["ticket_id"])] = item
            _write_tickets(data)
            _purge_json_tickets(data)
            return

    with db_transaction() as conn:
        conn.execute(insert(M.GovTicket).values(**values))


def _purge_json_tickets(data: dict) -> None:
    cutoff = _now() - timedelta(days=1)
    stale = []
    for ticket_id, raw in data.get("tickets", {}).items():
        if not isinstance(raw, dict):
            stale.append(ticket_id)
            continue
        expiry = _as_dt(raw.get("expiresAt"))
        used = _as_dt(raw.get("usedAt"))
        if (expiry and expiry < cutoff) or (used and used < cutoff):
            stale.append(ticket_id)
    for ticket_id in stale:
        data["tickets"].pop(ticket_id, None)
    if stale:
        _write_tickets(data)


def get_ticket(ticket_id: str) -> dict | None:
    if not _mysql():
        record = _read_tickets()["tickets"].get(str(ticket_id))
        return _ticket_dict(record) if isinstance(record, dict) else None
    with db_session() as conn:
        row = conn.execute(
            select(
                M.GovTicket.ticket_id,
                M.GovTicket.item_id,
                M.GovTicket.user_no,
                M.GovTicket.user_name,
                M.GovTicket.role,
                M.GovTicket.issued_at,
                M.GovTicket.expires_at,
                M.GovTicket.used_at,
            ).where(M.GovTicket.ticket_id == str(ticket_id))
        ).first()
        return _row_ticket(row) if row is not None else None


def consume_ticket(ticket_id: str, now: datetime | None = None) -> bool:
    """原子消费票据；已使用 / 已过期返回 False。"""
    moment = now or _now()
    if not _mysql():
        with _lock:
            data = _read_tickets()
            record = data["tickets"].get(str(ticket_id))
            if not isinstance(record, dict) or record.get("usedAt"):
                return False
            expiry = _as_dt(record.get("expiresAt"))
            if expiry is None or expiry <= moment:
                return False
            record["usedAt"] = _iso(moment)
            _write_tickets(data)
            return True

    with db_transaction() as conn:
        result = conn.execute(
            sa_update(M.GovTicket)
            .where(
                M.GovTicket.ticket_id == str(ticket_id),
                M.GovTicket.used_at.is_(None),
                M.GovTicket.expires_at > moment,
            )
            .values(used_at=moment)
        )
        return bool(result.rowcount)


def purge_tickets(days: int = 1) -> int:
    cutoff = _now() - timedelta(days=days)
    if not _mysql():
        with _lock:
            data = _read_tickets()
            stale = [
                ticket_id
                for ticket_id, raw in data.get("tickets", {}).items()
                if not isinstance(raw, dict)
                or (_as_dt(raw.get("expiresAt")) or _now()) < cutoff
                or (_as_dt(raw.get("usedAt")) or _now()) < cutoff
            ]
            for ticket_id in stale:
                data["tickets"].pop(ticket_id, None)
            if stale:
                _write_tickets(data)
            return len(stale)
    with db_transaction() as conn:
        result = conn.execute(
            sa_delete(M.GovTicket).where(
                or_(M.GovTicket.expires_at < cutoff, M.GovTicket.used_at < cutoff)
            )
        )
        return int(result.rowcount or 0)
