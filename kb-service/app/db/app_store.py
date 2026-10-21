"""智能体广场应用存储层（任务 3.3 / M-C-05·06）。

两种后端，对外接口一致：

* **MySQL**：``app`` 表；
* **JSON**：``data/apps.json``（进程内加锁 + 原子写），无库环境也可演示
  应用增删改、角色授权与广场过滤。

``visible_roles`` 在库里是逗号分隔字符串，对外一律归一为 ``list[str]``；
含「全部」时收敛为 ``["全部"]``，避免「全部 + 某个角色」两种等价写法。

排序口径：``sort_no`` 小的在前，相同则按 ``app_id`` 升序（先建的在前）。
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete as sa_delete
from sqlalchemy import insert, select
from sqlalchemy import update as sa_update

from app.config import settings
from app.db import models as M
from app.db.session import db_session, db_transaction

logger = logging.getLogger(__name__)

APPS_PATH: Path = settings.base_dir / "data" / "apps.json"
_lock = threading.RLock()

ALL_ROLE = "全部"
VISIBLE_ROLES = ("访客", "本科生", "研究生", "教职工", "离退休人员")
SOURCE_TYPES = ("agent", "link")
STATUS_NAMES = {1: "上架", 0: "下架"}

_COLUMNS = (
    M.App.app_id,
    M.App.app_name,
    M.App.source_type,
    M.App.agent_id,
    M.App.link_url,
    M.App.category,
    M.App.description,
    M.App.icon_url,
    M.App.sort_no,
    M.App.visible_roles,
    M.App.status,
    M.App.created_at,
    M.App.updated_at,
)

_FIELD_MAP = {
    "app_name": "appName",
    "source_type": "sourceType",
    "agent_id": "agentId",
    "link_url": "linkUrl",
    "category": "category",
    "description": "description",
    "icon_url": "iconUrl",
    "sort_no": "sortNo",
    "visible_roles": "visibleRoles",
    "status": "status",
}


def _mysql() -> bool:
    return settings.store_label == "mysql"


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat(timespec="seconds")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def split_roles(raw) -> list[str]:
    if isinstance(raw, list):
        values = raw
    else:
        values = str(raw or "").replace("、", ",").split(",")
    roles: list[str] = []
    for value in values:
        name = str(value).strip()
        if name and name not in roles:
            roles.append(name)
    if not roles or ALL_ROLE in roles:
        return [ALL_ROLE]
    order = {name: idx for idx, name in enumerate(VISIBLE_ROLES)}
    roles.sort(key=lambda name: order.get(name, len(order)))
    return roles


def join_roles(raw) -> str:
    return ",".join(split_roles(raw))


def _row_to_dict(row) -> dict:
    return {
        "appId": int(row[0]),
        "appName": row[1] or "",
        "sourceType": row[2] or "agent",
        "agentId": int(row[3]) if row[3] is not None else None,
        "linkUrl": row[4] or "",
        "category": row[5] or "",
        "description": row[6] or "",
        "iconUrl": row[7] or "",
        "sortNo": int(row[8] or 0),
        "visibleRoles": split_roles(row[9]),
        "status": int(row[10] or 0),
        "createdAt": _iso(row[11]),
        "updatedAt": _iso(row[12]),
    }


def _json_to_dict(record: dict) -> dict:
    return {
        "appId": int(record.get("appId") or 0),
        "appName": record.get("appName") or "",
        "sourceType": record.get("sourceType") or "agent",
        "agentId": int(record["agentId"]) if record.get("agentId") is not None else None,
        "linkUrl": record.get("linkUrl") or "",
        "category": record.get("category") or "",
        "description": record.get("description") or "",
        "iconUrl": record.get("iconUrl") or "",
        "sortNo": int(record.get("sortNo") or 0),
        "visibleRoles": split_roles(record.get("visibleRoles")),
        "status": 1 if int(record.get("status", 1)) else 0,
        "createdAt": record.get("createdAt"),
        "updatedAt": record.get("updatedAt"),
    }


def _read_all() -> dict:
    if not APPS_PATH.is_file():
        return {"apps": {}, "nextId": 1}
    try:
        data = json.loads(APPS_PATH.read_text(encoding="utf-8"))
        if not isinstance(data.get("apps"), dict):
            data["apps"] = {}
        data["nextId"] = int(data.get("nextId") or len(data["apps"]) + 1)
        return data
    except Exception:  # noqa: BLE001 - 文件损坏时重建，避免应用页整体不可用
        logger.warning("应用文件损坏，已重建：%s", APPS_PATH)
        return {"apps": {}, "nextId": 1}


def _write_all(data: dict) -> None:
    APPS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = APPS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(APPS_PATH)


def _sort(items: list[dict]) -> list[dict]:
    items.sort(key=lambda item: (item["sortNo"], item["appId"]))
    return items


# ------------------------------------------------------------------ 查询
def list_apps() -> list[dict]:
    if not _mysql():
        return _sort([_json_to_dict(rec) for rec in _read_all()["apps"].values()
                      if isinstance(rec, dict)])
    with db_session() as conn:
        rows = conn.execute(
            select(*_COLUMNS).order_by(M.App.sort_no, M.App.app_id)
        ).all()
        return [_row_to_dict(row) for row in rows]


def get_app(app_id: int) -> dict | None:
    if not _mysql():
        record = _read_all()["apps"].get(str(int(app_id)))
        return _json_to_dict(record) if isinstance(record, dict) else None
    with db_session() as conn:
        row = conn.execute(
            select(*_COLUMNS).where(M.App.app_id == int(app_id))
        ).first()
        return _row_to_dict(row) if row is not None else None


# ------------------------------------------------------------------ 写操作
def _record_values(fields: dict) -> dict:
    return {key: value for key, value in fields.items() if key in _FIELD_MAP}


def create_app(**fields) -> int:
    values = _record_values(fields)
    values.setdefault("app_name", "")
    values.setdefault("source_type", "agent")
    values.setdefault("agent_id", None)
    values.setdefault("link_url", "")
    values.setdefault("category", "")
    values.setdefault("description", "")
    values.setdefault("icon_url", "")
    values.setdefault("sort_no", 0)
    values.setdefault("visible_roles", [ALL_ROLE])
    values.setdefault("status", 1)
    if not _mysql():
        with _lock:
            data = _read_all()
            app_id = int(data.get("nextId") or 1)
            data["nextId"] = app_id + 1
            now = _now()
            record = {"appId": app_id}
            for key, camel in _FIELD_MAP.items():
                value = values.get(key)
                if key == "visible_roles":
                    value = join_roles(value)
                elif key == "status":
                    value = 1 if value else 0
                record[camel] = value
            record["createdAt"] = now
            record["updatedAt"] = now
            data["apps"][str(app_id)] = record
            _write_all(data)
            return app_id

    with db_transaction() as conn:
        values["visible_roles"] = join_roles(values.get("visible_roles"))
        result = conn.execute(insert(M.App).values(**values))
        return int(result.inserted_primary_key[0])


def update_app(app_id: int, **fields) -> bool:
    values = _record_values(fields)
    if not _mysql():
        with _lock:
            data = _read_all()
            record = data["apps"].get(str(int(app_id)))
            if not isinstance(record, dict):
                return False
            for key, value in values.items():
                camel = _FIELD_MAP[key]
                if key == "visible_roles":
                    value = join_roles(value)
                elif key == "status":
                    value = 1 if value else 0
                record[camel] = value
            record["updatedAt"] = _now()
            _write_all(data)
            return True

    with db_transaction() as conn:
        existing = conn.execute(
            select(M.App.app_id).where(M.App.app_id == int(app_id))
        ).first()
        if existing is None:
            return False
        if "visible_roles" in values:
            values["visible_roles"] = join_roles(values["visible_roles"])
        if values:
            conn.execute(sa_update(M.App).where(M.App.app_id == int(app_id)).values(**values))
        return True


def delete_app(app_id: int) -> bool:
    if not _mysql():
        with _lock:
            data = _read_all()
            if str(int(app_id)) not in data["apps"]:
                return False
            del data["apps"][str(int(app_id))]
            _write_all(data)
            return True
    with db_transaction() as conn:
        existing = conn.execute(
            select(M.App.app_id).where(M.App.app_id == int(app_id))
        ).first()
        if existing is None:
            return False
        conn.execute(sa_delete(M.App).where(M.App.app_id == int(app_id)))
        return True


def status_name(status: int) -> str:
    return STATUS_NAMES.get(int(status), "未知")
