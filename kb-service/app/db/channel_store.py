"""渠道分身存储层（任务 3.1 / M-C-01）。

两种后端，对外接口一致：

* **MySQL**：``channel`` + ``channel_agent`` 两张既有表；
* **JSON**：``data/channels.json``（进程内加锁 + 原子写），无库环境也可演示
  渠道创建、助手绑定与嵌入代码生成。

**默认助手恒在绑定集合内**：``channel.default_agent_id`` 是渠道主助手，
``channel_agent`` 允许为空（历史数据 / 只配主助手的渠道）。读取时把主助手
补到集合首位，写入时保证主助手存在且排第一——这样前端不需要理解两套语义。
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

CHANNELS_PATH: Path = settings.base_dir / "data" / "channels.json"
_lock = threading.RLock()

#: 渠道排序：新建的在前，便于运营看到刚配好的渠道
_STATUS_NAMES = {1: "启用", 0: "停用"}

_CHANNEL_COLUMNS = (
    M.Channel.channel_id,
    M.Channel.channel_code,
    M.Channel.channel_name,
    M.Channel.default_agent_id,
    M.Channel.welcome_text,
    M.Channel.theme_config,
    M.Channel.route_config,
    M.Channel.embed_url,
    M.Channel.embed_snippet,
    M.Channel.need_auth,
    M.Channel.copy_of,
    M.Channel.status,
    M.Channel.created_at,
    M.Channel.updated_at,
)


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


def _theme(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _normalize_agent_ids(raw, default_agent_id: int) -> list[int]:
    """去重保序，并保证主助手在首位。"""
    out: list[int] = []
    for value in raw or []:
        try:
            aid = int(value)
        except (TypeError, ValueError):
            continue
        if aid > 0 and aid not in out:
            out.append(aid)
    default = int(default_agent_id)
    if default not in out:
        out.insert(0, default)
    else:
        out.remove(default)
        out.insert(0, default)
    return out


def _row_to_dict(row) -> dict:
    return {
        "channelId": int(row[0]),
        "channelCode": row[1] or "",
        "channelName": row[2] or "",
        "defaultAgentId": int(row[3] or 0),
        "welcomeText": row[4] or "",
        "themeConfig": _theme(row[5]),
        "routeConfig": _theme(row[6]),
        "embedUrl": row[7] or "",
        "embedSnippet": row[8] or "",
        "needAuth": 1 if int(row[9] or 0) else 0,
        "copyOf": int(row[10]) if row[10] is not None else None,
        "status": int(row[11] or 0),
        "createdAt": _iso(row[12]),
        "updatedAt": _iso(row[13]),
        "agentIds": [],
    }


def _json_to_dict(record: dict) -> dict:
    default_agent_id = int(record.get("defaultAgentId") or 0)
    return {
        "channelId": int(record.get("channelId") or 0),
        "channelCode": record.get("channelCode") or "",
        "channelName": record.get("channelName") or "",
        "defaultAgentId": default_agent_id,
        "welcomeText": record.get("welcomeText") or "",
        "themeConfig": _theme(record.get("themeConfig")),
        "routeConfig": _theme(record.get("routeConfig")),
        "embedUrl": record.get("embedUrl") or "",
        "embedSnippet": record.get("embedSnippet") or "",
        "needAuth": 1 if int(record.get("needAuth", 1)) else 0,
        "copyOf": int(record["copyOf"]) if record.get("copyOf") is not None else None,
        "status": 1 if int(record.get("status", 1)) else 0,
        "createdAt": record.get("createdAt"),
        "updatedAt": record.get("updatedAt"),
        "agentIds": _normalize_agent_ids(record.get("agentIds"), default_agent_id)
        if default_agent_id
        else [],
    }


# ------------------------------------------------------------------ JSON 后端
def _read_all() -> dict:
    if not CHANNELS_PATH.is_file():
        return {"channels": {}, "nextId": 1}
    try:
        data = json.loads(CHANNELS_PATH.read_text(encoding="utf-8"))
        if not isinstance(data.get("channels"), dict):
            data["channels"] = {}
        data["nextId"] = int(data.get("nextId") or len(data["channels"]) + 1)
        return data
    except Exception:  # noqa: BLE001 - 文件损坏时重建，避免渠道页整体不可用
        logger.warning("渠道文件损坏，已重建：%s", CHANNELS_PATH)
        return {"channels": {}, "nextId": 1}


def _write_all(data: dict) -> None:
    CHANNELS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CHANNELS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CHANNELS_PATH)


def _json_list() -> list[dict]:
    data = _read_all()
    items = [_json_to_dict(rec) for rec in data["channels"].values() if isinstance(rec, dict)]
    items.sort(key=lambda item: item["channelId"], reverse=True)
    return items


# ------------------------------------------------------------------ MySQL 查询
def _attach_agents(conn, items: list[dict]) -> list[dict]:
    ids = [item["channelId"] for item in items if item["channelId"]]
    if not ids:
        return items
    grouped: dict[int, list[int]] = {channel_id: [] for channel_id in ids}
    rows = conn.execute(
        select(M.ChannelAgent.channel_id, M.ChannelAgent.agent_id)
        .where(M.ChannelAgent.channel_id.in_(ids))
        .order_by(M.ChannelAgent.channel_id, M.ChannelAgent.sort_no, M.ChannelAgent.id)
    ).all()
    for channel_id, agent_id in rows:
        grouped.setdefault(int(channel_id), []).append(int(agent_id))
    for item in items:
        item["agentIds"] = _normalize_agent_ids(
            grouped.get(item["channelId"], []), item["defaultAgentId"]
        )
    return items


def list_channels() -> list[dict]:
    if not _mysql():
        return _json_list()
    with db_session() as conn:
        rows = conn.execute(
            select(*_CHANNEL_COLUMNS).order_by(M.Channel.channel_id.desc())
        ).all()
        return _attach_agents(conn, [_row_to_dict(row) for row in rows])


def get_channel(channel_id: int) -> dict | None:
    if not _mysql():
        data = _read_all()
        record = data["channels"].get(str(int(channel_id)))
        return _json_to_dict(record) if isinstance(record, dict) else None
    with db_session() as conn:
        row = conn.execute(
            select(*_CHANNEL_COLUMNS).where(M.Channel.channel_id == int(channel_id))
        ).first()
        if row is None:
            return None
        return _attach_agents(conn, [_row_to_dict(row)])[0]


def get_by_code(channel_code: str) -> dict | None:
    code = (channel_code or "").strip()
    if not code:
        return None
    if not _mysql():
        for item in _json_list():
            if item["channelCode"] == code:
                return item
        return None
    with db_session() as conn:
        row = conn.execute(
            select(*_CHANNEL_COLUMNS).where(M.Channel.channel_code == code)
        ).first()
        if row is None:
            return None
        return _attach_agents(conn, [_row_to_dict(row)])[0]


# ------------------------------------------------------------------ 写操作
def _set_agents(conn, channel_id: int, agent_ids: list[int]) -> None:
    conn.execute(sa_delete(M.ChannelAgent).where(M.ChannelAgent.channel_id == channel_id))
    if agent_ids:
        conn.execute(
            insert(M.ChannelAgent),
            [
                {"channel_id": channel_id, "agent_id": agent_id, "sort_no": sort_no}
                for sort_no, agent_id in enumerate(agent_ids)
            ],
        )


def create_channel(
    *,
    channel_code: str,
    channel_name: str,
    default_agent_id: int,
    welcome_text: str = "",
    theme_config: dict | None = None,
    route_config: dict | None = None,
    embed_url: str = "",
    embed_snippet: str = "",
    need_auth: int = 1,
    status: int = 1,
    copy_of: int | None = None,
    agent_ids: list[int] | None = None,
) -> int:
    ids = _normalize_agent_ids(agent_ids, default_agent_id)
    if not _mysql():
        with _lock:
            data = _read_all()
            if any(
                rec.get("channelCode") == channel_code
                for rec in data["channels"].values()
                if isinstance(rec, dict)
            ):
                raise ValueError(f"渠道编码已存在：{channel_code}")
            channel_id = int(data.get("nextId") or 1)
            data["nextId"] = channel_id + 1
            now = _now()
            data["channels"][str(channel_id)] = {
                "channelId": channel_id,
                "channelCode": channel_code,
                "channelName": channel_name,
                "defaultAgentId": int(default_agent_id),
                "welcomeText": welcome_text,
                "themeConfig": theme_config or {},
                "routeConfig": route_config or {},
                "embedUrl": embed_url,
                "embedSnippet": embed_snippet,
                "needAuth": 1 if need_auth else 0,
                "copyOf": copy_of,
                "status": 1 if status else 0,
                "createdAt": now,
                "updatedAt": now,
                "agentIds": ids,
            }
            _write_all(data)
            return channel_id

    with db_transaction() as conn:
        result = conn.execute(
            insert(M.Channel).values(
                channel_code=channel_code,
                channel_name=channel_name,
                default_agent_id=int(default_agent_id),
                welcome_text=welcome_text,
                theme_config=theme_config or {},
                route_config=route_config or {},
                embed_url=embed_url,
                embed_snippet=embed_snippet,
                need_auth=1 if need_auth else 0,
                copy_of=copy_of,
                status=1 if status else 0,
            )
        )
        channel_id = int(result.inserted_primary_key[0])
        _set_agents(conn, channel_id, ids)
        return channel_id


def update_channel(channel_id: int, *, agent_ids: list[int] | None = None, **fields) -> bool:
    """按传入字段更新；``agent_ids`` 为 ``None`` 时不动关联表。"""
    allowed = {
        "channel_code",
        "channel_name",
        "default_agent_id",
        "welcome_text",
        "theme_config",
        "route_config",
        "embed_url",
        "embed_snippet",
        "need_auth",
        "copy_of",
        "status",
    }
    values = {key: value for key, value in fields.items() if key in allowed}
    if not _mysql():
        with _lock:
            data = _read_all()
            record = data["channels"].get(str(int(channel_id)))
            if not isinstance(record, dict):
                return False
            for key, value in values.items():
                camel = {
                    "channel_code": "channelCode",
                    "channel_name": "channelName",
                    "default_agent_id": "defaultAgentId",
                    "welcome_text": "welcomeText",
                    "theme_config": "themeConfig",
                    "route_config": "routeConfig",
                    "embed_url": "embedUrl",
                    "embed_snippet": "embedSnippet",
                    "need_auth": "needAuth",
                    "copy_of": "copyOf",
                    "status": "status",
                }[key]
                record[camel] = value
            if agent_ids is not None:
                record["agentIds"] = _normalize_agent_ids(
                    agent_ids, int(record.get("defaultAgentId") or 0)
                )
            record["updatedAt"] = _now()
            _write_all(data)
            return True

    with db_transaction() as conn:
        existing = conn.execute(
            select(M.Channel.channel_id).where(M.Channel.channel_id == int(channel_id))
        ).first()
        if existing is None:
            return False
        if values:
            conn.execute(
                sa_update(M.Channel).where(M.Channel.channel_id == channel_id).values(**values)
            )
        if agent_ids is not None:
            row = conn.execute(
                select(M.Channel.default_agent_id).where(M.Channel.channel_id == channel_id)
            ).first()
            default_id = int(row[0]) if row else 0
            _set_agents(conn, channel_id, _normalize_agent_ids(agent_ids, default_id))
        return True


def delete_channel(channel_id: int) -> bool:
    if not _mysql():
        with _lock:
            data = _read_all()
            if str(int(channel_id)) not in data["channels"]:
                return False
            del data["channels"][str(int(channel_id))]
            _write_all(data)
            return True
    with db_transaction() as conn:
        existing = conn.execute(
            select(M.Channel.channel_id).where(M.Channel.channel_id == int(channel_id))
        ).first()
        if existing is None:
            return False
        conn.execute(sa_delete(M.ChannelAgent).where(M.ChannelAgent.channel_id == channel_id))
        conn.execute(sa_delete(M.Channel).where(M.Channel.channel_id == channel_id))
        return True


def set_channel_agents(channel_id: int, agent_ids: list[int]) -> bool:
    item = get_channel(channel_id)
    if item is None:
        return False
    return update_channel(
        channel_id,
        agent_ids=_normalize_agent_ids(agent_ids, item["defaultAgentId"]),
    )


def status_name(status: int) -> str:
    return _STATUS_NAMES.get(int(status), "未知")
