"""客服工单、坐席与分组存储（任务 4.6 / P-B-09～13、M-S-02～06）。

两种后端，对外接口一致：

* **MySQL**：``support_group`` / ``agent_seat`` / ``ticket`` / ``ticket_message``
  （见仓库根 ``database/schema.sql`` 与 ``tools/migrate_support_4_6.py``）；
* **JSON**：``data/support.json``（进程内锁 + 原子写）。

工单状态：1 待接入 / 2 接待中 / 3 离线留言 / 4 已回复 / 5 已关闭。
用户侧凭证只存 SHA-256（``access_key_hash``），原始串仅在创建时返回一次，
避免顺序工单号被枚举后读到他人会话。
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import threading
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, insert, or_, select
from sqlalchemy import update as sa_update

from app.config import settings
from app.db import models as M
from app.db.session import db_session, db_transaction

logger = logging.getLogger(__name__)

SUPPORT_PATH: Path = settings.base_dir / "data" / "support.json"
_lock = threading.RLock()

STATE_QUEUED = 1
STATE_ACTIVE = 2
STATE_OFFLINE = 3
STATE_REPLIED = 4
STATE_CLOSED = 5

STATE_LABELS = {
    STATE_QUEUED: "待接入",
    STATE_ACTIVE: "接待中",
    STATE_OFFLINE: "离线留言",
    STATE_REPLIED: "已回复",
    STATE_CLOSED: "已关闭",
}

SENDER_USER = 1
SENDER_AGENT = 2
SENDER_SYSTEM = 3

DEFAULT_GROUPS = [
    {
        "groupName": "综合客服组",
        "channelIds": "",
        "description": "默认分组，承接全部渠道的转人工请求。",
        "status": 1,
    },
    {
        "groupName": "教务客服组",
        "channelIds": "jw,academic",
        "description": "课表、成绩、学籍等教务事务。",
        "status": 1,
    },
]

DEFAULT_SEATS = [
    {
        "userNo": "seat-demo",
        "seatName": "演示坐席·小智",
        "groupName": "综合客服组",
        "maxSessions": 5,
        "online": True,
    },
    {
        "userNo": "seat-backup",
        "seatName": "演示坐席·小云",
        "groupName": "综合客服组",
        "maxSessions": 5,
        "online": False,
    },
    {
        "userNo": "seat-jw",
        "seatName": "教务坐席·小林",
        "groupName": "教务客服组",
        "maxSessions": 5,
        "online": False,
    },
]


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


def hash_key(raw: str) -> str:
    return hashlib.sha256(str(raw or "").encode("utf-8")).hexdigest()


def verify_key(raw: str, expected_hash: str) -> bool:
    import hmac

    return hmac.compare_digest(hash_key(raw), str(expected_hash or ""))


def new_access_key() -> str:
    return secrets.token_urlsafe(24)


def new_ticket_no() -> str:
    return f"SUP{datetime.now():%Y%m%d}{secrets.token_hex(3).upper()}"


# ================================================================== JSON 后端
def _empty_json() -> dict:
    return {
        "groups": [],
        "seats": [],
        "tickets": [],
        "nextGroupId": 1,
        "nextSeatId": 1,
        "nextTicketId": 1,
        "nextMessageId": 1,
    }


def _read_json() -> dict:
    if not SUPPORT_PATH.is_file():
        data = _empty_json()
    else:
        try:
            data = json.loads(SUPPORT_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 文件损坏时重建，避免客服整体不可用
            logger.warning("客服数据文件损坏，已重建：%s", SUPPORT_PATH)
            data = _empty_json()
    for key, value in _empty_json().items():
        data.setdefault(key, value)
    for key in ("groups", "seats", "tickets"):
        if not isinstance(data.get(key), list):
            data[key] = []
    return data


def _write_json(data: dict) -> None:
    SUPPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SUPPORT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SUPPORT_PATH)


def _next_id(data: dict, key: str) -> int:
    value = int(data.get(key) or 1)
    data[key] = value + 1
    return value


def _seed_json(data: dict) -> bool:
    if data["groups"] or data["seats"]:
        return False
    group_ids: dict[str, int] = {}
    now = _iso(_now())
    for raw in DEFAULT_GROUPS:
        gid = _next_id(data, "nextGroupId")
        group_ids[raw["groupName"]] = gid
        data["groups"].append({
            "groupId": gid,
            **raw,
            "createdAt": now,
            "updatedAt": now,
        })
    for raw in DEFAULT_SEATS:
        sid = _next_id(data, "nextSeatId")
        data["seats"].append({
            "seatId": sid,
            "userId": 0,
            "userNo": raw["userNo"],
            "seatName": raw["seatName"],
            "groupId": group_ids.get(raw["groupName"]),
            "channelIds": "",
            "maxSessions": raw["maxSessions"],
            "online": bool(raw["online"]),
            "createdAt": now,
            "updatedAt": now,
        })
    return True


def _ensure_json_seed() -> None:
    if not settings.support_auto_seed:
        return
    with _lock:
        data = _read_json()
        if _seed_json(data):
            _write_json(data)


def _group_json(raw: dict, data: dict) -> dict:
    seats = [s for s in data["seats"] if int(s.get("groupId") or 0) == int(raw["groupId"])]
    return {
        "groupId": int(raw["groupId"]),
        "groupName": raw.get("groupName") or "",
        "channelIds": raw.get("channelIds") or "",
        "description": raw.get("description") or "",
        "status": int(raw.get("status") or 0),
        "createdAt": raw.get("createdAt"),
        "updatedAt": raw.get("updatedAt"),
        "seatCount": len(seats),
        "onlineSeats": sum(1 for s in seats if s.get("online")),
    }


def _seat_json(raw: dict, data: dict) -> dict:
    group = next(
        (g for g in data["groups"] if int(g["groupId"]) == int(raw.get("groupId") or 0)),
        None,
    )
    active = sum(
        1
        for t in data["tickets"]
        if int(t.get("seatId") or 0) == int(raw["seatId"])
        and int(t.get("state") or 0) in (STATE_QUEUED, STATE_ACTIVE)
    )
    return {
        "seatId": int(raw["seatId"]),
        "userId": raw.get("userId"),
        "userNo": raw.get("userNo") or "",
        "seatName": raw.get("seatName") or "",
        "groupId": raw.get("groupId"),
        "groupName": (group or {}).get("groupName") or "",
        "channelIds": raw.get("channelIds") or "",
        "maxSessions": int(raw.get("maxSessions") or 5),
        "online": bool(raw.get("online")),
        "activeCount": active,
        "createdAt": raw.get("createdAt"),
        "updatedAt": raw.get("updatedAt"),
    }


def _message_json(raw: dict) -> dict:
    return {
        "messageId": int(raw["messageId"]),
        "ticketId": int(raw["ticketId"]),
        "senderType": int(raw["senderType"]),
        "senderName": raw.get("senderName") or "",
        "content": raw.get("content") or "",
        "sentiment": int(raw.get("sentiment") or 0),
        "sentimentLabel": "负面" if int(raw.get("sentiment") or 0) == 1 else "正常",
        "pushed": bool(raw.get("pushed")),
        "read": bool(raw.get("read")),
        "createdAt": raw.get("createdAt"),
    }


def _ticket_json(raw: dict, data: dict, *, with_messages: bool = False) -> dict:
    seat = next(
        (s for s in data["seats"] if int(s["seatId"]) == int(raw.get("seatId") or 0)),
        None,
    )
    group = next(
        (g for g in data["groups"] if int(g["groupId"]) == int(raw.get("groupId") or 0)),
        None,
    )
    messages = sorted(raw.get("messages") or [], key=lambda m: int(m.get("messageId") or 0))
    last = messages[-1] if messages else None
    state = int(raw.get("state") or STATE_QUEUED)
    out = {
        "ticketId": int(raw["ticketId"]),
        "ticketNo": raw.get("ticketNo") or "",
        "sessionId": raw.get("sessionId") or "",
        "channelId": int(raw.get("channelId") or 0),
        "channelCode": raw.get("channelCode") or "",
        "userId": raw.get("userId"),
        "userNo": raw.get("userNo") or "",
        "userName": raw.get("userName") or "",
        "role": raw.get("role") or "",
        "seatId": raw.get("seatId"),
        "seatName": (seat or {}).get("seatName") or "",
        "groupId": raw.get("groupId"),
        "groupName": (group or {}).get("groupName") or "",
        "state": state,
        "stateLabel": STATE_LABELS.get(state, "未知"),
        "sentiment": int(raw.get("sentiment") or 0),
        "sentimentLabel": "负面" if int(raw.get("sentiment") or 0) == 1 else "正常",
        "subject": raw.get("subject") or "",
        "messageCount": len(messages),
        "unreadAgent": sum(
            1 for m in messages if int(m.get("senderType") or 0) == SENDER_USER and not m.get("read")
        ),
        "unreadUser": sum(
            1
            for m in messages
            if int(m.get("senderType") or 0) != SENDER_USER and not m.get("pushed")
        ),
        "lastMessage": (last or {}).get("content") or "",
        "createdAt": raw.get("createdAt"),
        "updatedAt": raw.get("updatedAt"),
        "acceptedAt": raw.get("acceptedAt"),
        "lastMessageAt": raw.get("lastMessageAt"),
        "closedAt": raw.get("closedAt"),
        "closeReason": raw.get("closeReason") or "",
    }
    if with_messages:
        out["messages"] = [_message_json(m) for m in messages]
    return out


def _find_ticket(data: dict, ticket_id: int) -> dict | None:
    return next(
        (t for t in data["tickets"] if int(t.get("ticketId") or 0) == int(ticket_id)), None
    )


# ================================================================== MySQL 映射
_GROUP_COLUMNS = (
    M.SupportGroup.group_id,
    M.SupportGroup.group_name,
    M.SupportGroup.channel_ids,
    M.SupportGroup.description,
    M.SupportGroup.status,
    M.SupportGroup.created_at,
    M.SupportGroup.updated_at,
)

_SEAT_COLUMNS = (
    M.AgentSeat.seat_id,
    M.AgentSeat.user_id,
    M.AgentSeat.user_no,
    M.AgentSeat.seat_name,
    M.AgentSeat.group_id,
    M.AgentSeat.channel_ids,
    M.AgentSeat.max_sessions,
    M.AgentSeat.online_flag,
    M.AgentSeat.created_at,
    M.AgentSeat.updated_at,
)

_TICKET_COLUMNS = (
    M.Ticket.ticket_id,
    M.Ticket.ticket_no,
    M.Ticket.session_id,
    M.Ticket.channel_id,
    M.Ticket.channel_code,
    M.Ticket.user_id,
    M.Ticket.user_no,
    M.Ticket.user_name,
    M.Ticket.role,
    M.Ticket.seat_id,
    M.Ticket.group_id,
    M.Ticket.ticket_state,
    M.Ticket.sentiment,
    M.Ticket.sentiment_label,
    M.Ticket.subject,
    M.Ticket.accepted_at,
    M.Ticket.last_message_at,
    M.Ticket.closed_at,
    M.Ticket.close_reason,
    M.Ticket.created_at,
    M.Ticket.updated_at,
)

_MESSAGE_COLUMNS = (
    M.TicketMessage.id,
    M.TicketMessage.ticket_id,
    M.TicketMessage.sender_type,
    M.TicketMessage.sender_name,
    M.TicketMessage.content,
    M.TicketMessage.sentiment,
    M.TicketMessage.pushed_flag,
    M.TicketMessage.read_flag,
    M.TicketMessage.created_at,
)


def _group_row(row) -> dict:
    return {
        "groupId": int(row[0]),
        "groupName": row[1] or "",
        "channelIds": row[2] or "",
        "description": row[3] or "",
        "status": int(row[4] or 0),
        "createdAt": _iso(row[5]),
        "updatedAt": _iso(row[6]),
    }


def _seat_row(row) -> dict:
    return {
        "seatId": int(row[0]),
        "userId": row[1],
        "userNo": row[2] or "",
        "seatName": row[3] or "",
        "groupId": row[4],
        "channelIds": row[5] or "",
        "maxSessions": int(row[6] or 5),
        "online": bool(row[7]),
        "createdAt": _iso(row[8]),
        "updatedAt": _iso(row[9]),
    }


def _ticket_row(row) -> dict:
    return {
        "ticketId": int(row[0]),
        "ticketNo": row[1] or "",
        "sessionId": row[2] or "",
        "channelId": int(row[3] or 0),
        "channelCode": row[4] or "",
        "userId": row[5],
        "userNo": row[6] or "",
        "userName": row[7] or "",
        "role": row[8] or "",
        "seatId": row[9],
        "groupId": row[10],
        "state": int(row[11] or STATE_QUEUED),
        "sentiment": int(row[12] or 0),
        "sentimentLabel": row[13] or "正常",
        "subject": row[14] or "",
        "acceptedAt": _iso(row[15]),
        "lastMessageAt": _iso(row[16]),
        "closedAt": _iso(row[17]),
        "closeReason": row[18] or "",
        "createdAt": _iso(row[19]),
        "updatedAt": _iso(row[20]),
    }


def _message_row(row) -> dict:
    return {
        "messageId": int(row[0]),
        "ticketId": int(row[1]),
        "senderType": int(row[2] or 0),
        "senderName": row[3] or "",
        "content": row[4] or "",
        "sentiment": int(row[5] or 0),
        "pushed": bool(row[6]),
        "read": bool(row[7]),
        "createdAt": _iso(row[8]),
    }


def _decorate_ticket(ticket: dict, seats: list[dict], groups: list[dict]) -> dict:
    seat = next(
        (s for s in seats if int(s["seatId"]) == int(ticket.get("seatId") or 0)), None
    )
    group = next(
        (g for g in groups if int(g["groupId"]) == int(ticket.get("groupId") or 0)), None
    )
    state = int(ticket.get("state") or STATE_QUEUED)
    return {
        **ticket,
        "seatName": (seat or {}).get("seatName") or "",
        "groupName": (group or {}).get("groupName") or "",
        "stateLabel": STATE_LABELS.get(state, "未知"),
        "sentimentLabel": "负面" if int(ticket.get("sentiment") or 0) == 1 else "正常",
    }


# ================================================================== 分组
def list_groups() -> list[dict]:
    if not _mysql():
        _ensure_json_seed()
        with _lock:
            data = _read_json()
            groups = [_group_json(g, data) for g in data["groups"]]
        groups.sort(key=lambda g: int(g["groupId"]))
        return groups

    with db_session() as conn:
        rows = conn.execute(
            select(*_GROUP_COLUMNS).order_by(M.SupportGroup.group_id)
        ).all()
        seat_rows = conn.execute(
            select(M.AgentSeat.group_id, M.AgentSeat.online_flag)
        ).all()
    counts: dict[int, tuple[int, int]] = {}
    for gid, online in seat_rows:
        total, on = counts.get(int(gid or 0), (0, 0))
        counts[int(gid or 0)] = (total + 1, on + (1 if online else 0))
    out = []
    for row in rows:
        item = _group_row(row)
        total, on = counts.get(item["groupId"], (0, 0))
        item["seatCount"] = total
        item["onlineSeats"] = on
        out.append(item)
    return out


def get_group(group_id: int) -> dict | None:
    return next((g for g in list_groups() if int(g["groupId"]) == int(group_id)), None)


def create_group(raw: dict) -> dict:
    name = str(raw.get("groupName") or "").strip()
    channels = str(raw.get("channelIds") or "").strip()
    description = str(raw.get("description") or "").strip()
    status = int(raw.get("status", 1))
    if not _mysql():
        _ensure_json_seed()
        with _lock:
            data = _read_json()
            if any(g.get("groupName") == name for g in data["groups"]):
                raise ValueError("分组名称已存在")
            gid = _next_id(data, "nextGroupId")
            now = _iso(_now())
            record = {
                "groupId": gid,
                "groupName": name,
                "channelIds": channels,
                "description": description,
                "status": status,
                "createdAt": now,
                "updatedAt": now,
            }
            data["groups"].append(record)
            _write_json(data)
            return _group_json(record, data)

    with db_transaction() as conn:
        exists = conn.execute(
            select(M.SupportGroup.group_id).where(M.SupportGroup.group_name == name)
        ).first()
        if exists:
            raise ValueError("分组名称已存在")
        now = _now()
        res = conn.execute(
            insert(M.SupportGroup).values(
                group_name=name,
                channel_ids=channels,
                description=description,
                status=status,
                created_at=now,
                updated_at=now,
            )
        )
        gid = int(res.inserted_primary_key[0])
    return get_group(gid) or {}


def update_group(group_id: int, fields: dict) -> dict | None:
    if not _mysql():
        _ensure_json_seed()
        with _lock:
            data = _read_json()
            record = next(
                (g for g in data["groups"] if int(g["groupId"]) == int(group_id)), None
            )
            if record is None:
                return None
            name = str(fields.get("groupName", record["groupName"]) or "").strip()
            if any(
                g is not record and g.get("groupName") == name for g in data["groups"]
            ):
                raise ValueError("分组名称已存在")
            record["groupName"] = name
            if "channelIds" in fields:
                record["channelIds"] = str(fields.get("channelIds") or "").strip()
            if "description" in fields:
                record["description"] = str(fields.get("description") or "").strip()
            if "status" in fields:
                record["status"] = int(fields.get("status") or 0)
            record["updatedAt"] = _iso(_now())
            _write_json(data)
            return _group_json(record, data)

    with db_transaction() as conn:
        record = conn.execute(
            select(M.SupportGroup.group_id, M.SupportGroup.group_name).where(
                M.SupportGroup.group_id == int(group_id)
            )
        ).first()
        if record is None:
            return None
        name = str(fields.get("groupName", record[1]) or "").strip()
        duplicate = conn.execute(
            select(M.SupportGroup.group_id).where(
                M.SupportGroup.group_name == name,
                M.SupportGroup.group_id != int(group_id),
            )
        ).first()
        if duplicate:
            raise ValueError("分组名称已存在")
        values: dict = {"group_name": name, "updated_at": _now()}
        if "channelIds" in fields:
            values["channel_ids"] = str(fields.get("channelIds") or "").strip()
        if "description" in fields:
            values["description"] = str(fields.get("description") or "").strip()
        if "status" in fields:
            values["status"] = int(fields.get("status") or 0)
        conn.execute(
            sa_update(M.SupportGroup)
            .where(M.SupportGroup.group_id == int(group_id))
            .values(**values)
        )
    return get_group(group_id)


def delete_group(group_id: int) -> bool:
    if not _mysql():
        _ensure_json_seed()
        with _lock:
            data = _read_json()
            before = len(data["groups"])
            data["groups"] = [
                g for g in data["groups"] if int(g["groupId"]) != int(group_id)
            ]
            if len(data["groups"]) == before:
                return False
            _write_json(data)
            return True

    with db_transaction() as conn:
        res = conn.execute(
            sa_delete(M.SupportGroup).where(M.SupportGroup.group_id == int(group_id))
        )
        return bool(res.rowcount)


def group_seat_count(group_id: int) -> int:
    if not _mysql():
        _ensure_json_seed()
        with _lock:
            data = _read_json()
            return sum(
                1 for s in data["seats"] if int(s.get("groupId") or 0) == int(group_id)
            )
    with db_session() as conn:
        return int(
            conn.execute(
                select(func.count(M.AgentSeat.seat_id)).where(
                    M.AgentSeat.group_id == int(group_id)
                )
            ).scalar()
            or 0
        )


# ================================================================== 坐席
def list_seats() -> list[dict]:
    if not _mysql():
        _ensure_json_seed()
        with _lock:
            data = _read_json()
            seats = [_seat_json(s, data) for s in data["seats"]]
        seats.sort(key=lambda s: int(s["seatId"]))
        return seats

    groups = list_groups()
    with db_session() as conn:
        rows = conn.execute(select(*_SEAT_COLUMNS).order_by(M.AgentSeat.seat_id)).all()
        active_rows = conn.execute(
            select(M.Ticket.seat_id, func.count())
            .where(M.Ticket.ticket_state.in_((STATE_QUEUED, STATE_ACTIVE)))
            .group_by(M.Ticket.seat_id)
        ).all()
    active = {int(r[0] or 0): int(r[1] or 0) for r in active_rows}
    group_names = {int(g["groupId"]): g["groupName"] for g in groups}
    out = []
    for row in rows:
        item = _seat_row(row)
        item["groupName"] = group_names.get(int(item.get("groupId") or 0), "")
        item["activeCount"] = active.get(item["seatId"], 0)
        out.append(item)
    return out


def get_seat(seat_id: int) -> dict | None:
    return next((s for s in list_seats() if int(s["seatId"]) == int(seat_id)), None)


def find_seat_by_user(user_no: str) -> dict | None:
    user_no = str(user_no or "").strip()
    if not user_no:
        return None
    return next((s for s in list_seats() if s.get("userNo") == user_no), None)


def create_seat(raw: dict) -> dict:
    user_no = str(raw.get("userNo") or "").strip()
    seat_name = str(raw.get("seatName") or "").strip()
    group_id = raw.get("groupId")
    channel_ids = str(raw.get("channelIds") or "").strip()
    max_sessions = int(raw.get("maxSessions", 5))
    online = bool(raw.get("online"))
    user_id = int(raw.get("userId") or 0)
    if not _mysql():
        _ensure_json_seed()
        with _lock:
            data = _read_json()
            if any(s.get("userNo") == user_no for s in data["seats"]):
                raise ValueError("该用户已是客服坐席")
            sid = _next_id(data, "nextSeatId")
            now = _iso(_now())
            record = {
                "seatId": sid,
                "userId": user_id,
                "userNo": user_no,
                "seatName": seat_name,
                "groupId": group_id,
                "channelIds": channel_ids,
                "maxSessions": max_sessions,
                "online": online,
                "createdAt": now,
                "updatedAt": now,
            }
            data["seats"].append(record)
            _write_json(data)
            return _seat_json(record, data)

    with db_transaction() as conn:
        exists = conn.execute(
            select(M.AgentSeat.seat_id).where(M.AgentSeat.user_no == user_no)
        ).first()
        if exists:
            raise ValueError("该用户已是客服坐席")
        now = _now()
        res = conn.execute(
            insert(M.AgentSeat).values(
                user_id=user_id,
                user_no=user_no,
                seat_name=seat_name,
                group_id=group_id,
                channel_ids=channel_ids,
                max_sessions=max_sessions,
                online_flag=1 if online else 0,
                created_at=now,
                updated_at=now,
            )
        )
        seat_id = int(res.inserted_primary_key[0])
    return get_seat(seat_id) or {}


def update_seat(seat_id: int, fields: dict) -> dict | None:
    if not _mysql():
        _ensure_json_seed()
        with _lock:
            data = _read_json()
            record = next(
                (s for s in data["seats"] if int(s["seatId"]) == int(seat_id)), None
            )
            if record is None:
                return None
            if "userNo" in fields:
                user_no = str(fields.get("userNo") or "").strip()
                if any(
                    s is not record and s.get("userNo") == user_no for s in data["seats"]
                ):
                    raise ValueError("该用户已是客服坐席")
                record["userNo"] = user_no
            if "seatName" in fields:
                record["seatName"] = str(fields.get("seatName") or "").strip()
            if "groupId" in fields:
                record["groupId"] = fields.get("groupId")
            if "channelIds" in fields:
                record["channelIds"] = str(fields.get("channelIds") or "").strip()
            if "maxSessions" in fields:
                record["maxSessions"] = int(fields.get("maxSessions") or 5)
            if "online" in fields:
                record["online"] = bool(fields.get("online"))
            record["updatedAt"] = _iso(_now())
            _write_json(data)
            return _seat_json(record, data)

    with db_transaction() as conn:
        record = conn.execute(
            select(M.AgentSeat.seat_id, M.AgentSeat.user_no).where(
                M.AgentSeat.seat_id == int(seat_id)
            )
        ).first()
        if record is None:
            return None
        values: dict = {"updated_at": _now()}
        if "userNo" in fields:
            user_no = str(fields.get("userNo") or "").strip()
            duplicate = conn.execute(
                select(M.AgentSeat.seat_id).where(
                    M.AgentSeat.user_no == user_no,
                    M.AgentSeat.seat_id != int(seat_id),
                )
            ).first()
            if duplicate:
                raise ValueError("该用户已是客服坐席")
            values["user_no"] = user_no
        if "seatName" in fields:
            values["seat_name"] = str(fields.get("seatName") or "").strip()
        if "groupId" in fields:
            values["group_id"] = fields.get("groupId")
        if "channelIds" in fields:
            values["channel_ids"] = str(fields.get("channelIds") or "").strip()
        if "maxSessions" in fields:
            values["max_sessions"] = int(fields.get("maxSessions") or 5)
        if "online" in fields:
            values["online_flag"] = 1 if fields.get("online") else 0
        conn.execute(
            sa_update(M.AgentSeat).where(M.AgentSeat.seat_id == int(seat_id)).values(**values)
        )
    return get_seat(seat_id)


def set_seat_online(seat_id: int, online: bool) -> dict | None:
    return update_seat(seat_id, {"online": bool(online)})


def delete_seat(seat_id: int) -> bool:
    if not _mysql():
        _ensure_json_seed()
        with _lock:
            data = _read_json()
            before = len(data["seats"])
            data["seats"] = [
                s for s in data["seats"] if int(s["seatId"]) != int(seat_id)
            ]
            if len(data["seats"]) == before:
                return False
            _write_json(data)
            return True

    with db_transaction() as conn:
        res = conn.execute(
            sa_delete(M.AgentSeat).where(M.AgentSeat.seat_id == int(seat_id))
        )
        return bool(res.rowcount)


def seat_active_count(seat_id: int) -> int:
    if not _mysql():
        _ensure_json_seed()
        with _lock:
            data = _read_json()
            return sum(
                1
                for t in data["tickets"]
                if int(t.get("seatId") or 0) == int(seat_id)
                and int(t.get("state") or 0) in (STATE_QUEUED, STATE_ACTIVE)
            )
    with db_session() as conn:
        return int(
            conn.execute(
                select(func.count(M.Ticket.ticket_id)).where(
                    M.Ticket.seat_id == int(seat_id),
                    M.Ticket.ticket_state.in_((STATE_QUEUED, STATE_ACTIVE)),
                )
            ).scalar()
            or 0
        )


# ================================================================== 工单
def create_ticket(raw: dict, *, first_message: dict | None = None) -> dict:
    if not _mysql():
        _ensure_json_seed()
        with _lock:
            data = _read_json()
            tid = _next_id(data, "nextTicketId")
            now = _iso(_now())
            record = {
                "ticketId": tid,
                "ticketNo": raw.get("ticketNo") or new_ticket_no(),
                "sessionId": raw.get("sessionId") or "",
                "channelId": int(raw.get("channelId") or 0),
                "channelCode": raw.get("channelCode") or "",
                "userId": raw.get("userId"),
                "userNo": raw.get("userNo") or "",
                "userName": raw.get("userName") or "",
                "role": raw.get("role") or "",
                "seatId": raw.get("seatId"),
                "groupId": raw.get("groupId"),
                "state": int(raw.get("state") or STATE_QUEUED),
                "sentiment": int(raw.get("sentiment") or 0),
                "subject": raw.get("subject") or "",
                "accessKeyHash": raw.get("accessKeyHash") or "",
                "acceptedAt": None,
                "lastMessageAt": now,
                "closedAt": None,
                "closeReason": "",
                "createdAt": now,
                "updatedAt": now,
                "messages": [],
            }
            if first_message:
                record["messages"].append(_build_json_message(data, record, first_message))
            data["tickets"].append(record)
            _write_json(data)
            return _ticket_json(record, data, with_messages=True)

    now = _now()
    with db_transaction() as conn:
        res = conn.execute(
            insert(M.Ticket).values(
                ticket_no=raw.get("ticketNo") or new_ticket_no(),
                session_id=raw.get("sessionId") or "",
                channel_id=int(raw.get("channelId") or 0),
                channel_code=raw.get("channelCode") or "",
                user_id=raw.get("userId"),
                user_no=raw.get("userNo") or "",
                user_name=raw.get("userName") or "",
                role=raw.get("role") or "",
                seat_id=raw.get("seatId"),
                group_id=raw.get("groupId"),
                ticket_state=int(raw.get("state") or STATE_QUEUED),
                sentiment=int(raw.get("sentiment") or 0),
                sentiment_label="负面" if int(raw.get("sentiment") or 0) == 1 else "正常",
                subject=raw.get("subject") or "",
                access_key_hash=raw.get("accessKeyHash") or "",
                last_message_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        ticket_id = int(res.inserted_primary_key[0])
        if first_message:
            conn.execute(
                insert(M.TicketMessage).values(
                    ticket_id=ticket_id,
                    sender_type=int(first_message.get("senderType") or SENDER_USER),
                    sender_name=first_message.get("senderName") or "",
                    content=first_message.get("content") or "",
                    sentiment=int(first_message.get("sentiment") or 0),
                    pushed_flag=0 if int(first_message.get("senderType") or 1) != SENDER_USER else 1,
                    read_flag=0 if int(first_message.get("senderType") or 1) == SENDER_USER else 1,
                    created_at=now,
                )
            )
    return get_ticket(ticket_id) or {}


def _build_json_message(data: dict, ticket: dict, raw: dict) -> dict:
    sender_type = int(raw.get("senderType") or SENDER_USER)
    return {
        "messageId": _next_id(data, "nextMessageId"),
        "ticketId": int(ticket["ticketId"]),
        "senderType": sender_type,
        "senderName": raw.get("senderName") or "",
        "content": raw.get("content") or "",
        "sentiment": int(raw.get("sentiment") or 0),
        # pushed = 用户是否已拉到；read = 坐席是否已读。首次写入时两者都取反默认，
        # 之后分别由 mark_pushed / mark_read 推进。
        "pushed": sender_type == SENDER_USER,
        "read": sender_type != SENDER_USER,
        "createdAt": _iso(_now()),
    }


def _resolve_names_mysql(conn, tickets: list[dict]) -> tuple[list[dict], list[dict]]:
    seats = [_seat_row(row) for row in conn.execute(select(*_SEAT_COLUMNS)).all()]
    groups = [_group_row(row) for row in conn.execute(select(*_GROUP_COLUMNS)).all()]
    return seats, groups


def _ticket_messages_mysql(conn, ticket_ids: list[int]) -> dict[int, list[dict]]:
    if not ticket_ids:
        return {}
    out: dict[int, list[dict]] = {tid: [] for tid in ticket_ids}
    rows = conn.execute(
        select(*_MESSAGE_COLUMNS)
        .where(M.TicketMessage.ticket_id.in_(ticket_ids))
        .order_by(M.TicketMessage.id)
    ).all()
    for row in rows:
        item = _message_row(row)
        out.setdefault(item["ticketId"], []).append(item)
    return out


def ticket_access_hash(ticket_id: int) -> str:
    if not _mysql():
        with _lock:
            record = _find_ticket(_read_json(), ticket_id)
        return str((record or {}).get("accessKeyHash") or "")
    with db_session() as conn:
        return str(
            conn.execute(
                select(M.Ticket.access_key_hash).where(
                    M.Ticket.ticket_id == int(ticket_id)
                )
            ).scalar()
            or ""
        )


def verify_ticket_key(ticket_id: int, raw: str) -> bool:
    return verify_key(raw, ticket_access_hash(ticket_id))


def set_ticket_access(ticket_id: int, raw: str) -> None:
    digest = hash_key(raw)
    if not _mysql():
        with _lock:
            data = _read_json()
            record = _find_ticket(data, ticket_id)
            if record is not None:
                record["accessKeyHash"] = digest
                _write_json(data)
        return
    with db_transaction() as conn:
        conn.execute(
            sa_update(M.Ticket)
            .where(M.Ticket.ticket_id == int(ticket_id))
            .values(access_key_hash=digest, updated_at=_now())
        )


def get_ticket(ticket_id: int, *, with_messages: bool = True) -> dict | None:
    if not _mysql():
        _ensure_json_seed()
        with _lock:
            data = _read_json()
            record = _find_ticket(data, ticket_id)
            return _ticket_json(record, data, with_messages=with_messages) if record else None

    with db_session() as conn:
        row = conn.execute(
            select(*_TICKET_COLUMNS).where(M.Ticket.ticket_id == int(ticket_id))
        ).first()
        if row is None:
            return None
        ticket = _ticket_row(row)
        seats, groups = _resolve_names_mysql(conn, [ticket])
        messages = _ticket_messages_mysql(conn, [ticket["ticketId"]]).get(
            ticket["ticketId"], []
        )
        ticket.update(_status_counts(messages))
        ticket["messageCount"] = len(messages)
        ticket["lastMessage"] = messages[-1]["content"] if messages else ""
    out = _decorate_ticket(ticket, seats, groups)
    if with_messages:
        out["messages"] = messages
    return out


def _status_counts(messages: list[dict]) -> dict:
    return {
        "unreadAgent": sum(
            1 for m in messages if int(m.get("senderType") or 0) == SENDER_USER and not m.get("read")
        ),
        "unreadUser": sum(
            1
            for m in messages
            if int(m.get("senderType") or 0) != SENDER_USER and not m.get("pushed")
        ),
    }


def get_open_ticket_by_session(session_id: str) -> dict | None:
    session_id = str(session_id or "").strip()
    if not session_id:
        return None
    if not _mysql():
        _ensure_json_seed()
        with _lock:
            data = _read_json()
            matches = [
                t
                for t in data["tickets"]
                if t.get("sessionId") == session_id
                and int(t.get("state") or 0) != STATE_CLOSED
            ]
            matches.sort(key=lambda t: int(t["ticketId"]), reverse=True)
            return _ticket_json(matches[0], data, with_messages=True) if matches else None

    with db_session() as conn:
        row = conn.execute(
            select(M.Ticket.ticket_id)
            .where(
                M.Ticket.session_id == session_id,
                M.Ticket.ticket_state != STATE_CLOSED,
            )
            .order_by(M.Ticket.ticket_id.desc())
        ).first()
    return get_ticket(int(row[0])) if row else None


def list_tickets(
    *,
    state: int | None = None,
    group_id: int | None = None,
    seat_id: int | None = None,
    sentiment: int | None = None,
    channel_code: str = "",
    q: str = "",
    page: int = 1,
    page_size: int = 20,
) -> dict:
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 20), 200))
    keyword = str(q or "").strip().lower()
    channel_code = str(channel_code or "").strip()

    if not _mysql():
        _ensure_json_seed()
        with _lock:
            data = _read_json()
            tickets = [_ticket_json(t, data) for t in data["tickets"]]
        filtered = []
        for ticket in tickets:
            if state is not None and int(ticket["state"]) != int(state):
                continue
            if group_id is not None and int(ticket.get("groupId") or 0) != int(group_id):
                continue
            if seat_id is not None and int(ticket.get("seatId") or 0) != int(seat_id):
                continue
            if sentiment is not None and int(ticket["sentiment"]) != int(sentiment):
                continue
            if channel_code and ticket.get("channelCode") != channel_code:
                continue
            if keyword:
                haystack = " ".join(
                    str(ticket.get(key) or "")
                    for key in ("ticketNo", "subject", "userNo", "userName", "seatName", "lastMessage")
                ).lower()
                if keyword not in haystack:
                    continue
            filtered.append(ticket)
        filtered.sort(
            key=lambda t: (t.get("updatedAt") or "", int(t["ticketId"])), reverse=True
        )
        total = len(filtered)
        start = (page - 1) * page_size
        return {
            "items": filtered[start : start + page_size],
            "total": total,
            "page": page,
            "pageSize": page_size,
        }

    conditions = []
    if state is not None:
        conditions.append(M.Ticket.ticket_state == int(state))
    if group_id is not None:
        conditions.append(M.Ticket.group_id == int(group_id))
    if seat_id is not None:
        conditions.append(M.Ticket.seat_id == int(seat_id))
    if sentiment is not None:
        conditions.append(M.Ticket.sentiment == int(sentiment))
    if channel_code:
        conditions.append(M.Ticket.channel_code == channel_code)
    if keyword:
        like = f"%{keyword}%"
        conditions.append(
            or_(
                M.Ticket.ticket_no.like(like),
                M.Ticket.subject.like(like),
                M.Ticket.user_no.like(like),
                M.Ticket.user_name.like(like),
            )
        )
    with db_session() as conn:
        stmt = select(*_TICKET_COLUMNS)
        count_stmt = select(func.count(M.Ticket.ticket_id))
        if conditions:
            stmt = stmt.where(*conditions)
            count_stmt = count_stmt.where(*conditions)
        total = int(conn.execute(count_stmt).scalar() or 0)
        rows = conn.execute(
            stmt.order_by(M.Ticket.updated_at.desc(), M.Ticket.ticket_id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        seats, groups = _resolve_names_mysql(conn, [])
        tickets = [_decorate_ticket(_ticket_row(row), seats, groups) for row in rows]
        messages = _ticket_messages_mysql(conn, [t["ticketId"] for t in tickets])
    for ticket in tickets:
        thread = messages.get(ticket["ticketId"], [])
        ticket.update(_status_counts(thread))
        ticket["messageCount"] = len(thread)
        ticket["lastMessage"] = thread[-1]["content"] if thread else ""
    return {"items": tickets, "total": total, "page": page, "pageSize": page_size}


def claim_ticket(ticket_id: int, seat_id: int) -> dict | None:
    """原子接入工单：仅 1/3 状态可接入，重复接入由后到者拿到 None。"""
    if not _mysql():
        _ensure_json_seed()
        with _lock:
            data = _read_json()
            record = _find_ticket(data, ticket_id)
            if record is None or int(record.get("state") or 0) not in (
                STATE_QUEUED,
                STATE_OFFLINE,
            ):
                return None
            record["state"] = STATE_ACTIVE
            record["seatId"] = int(seat_id)
            record["acceptedAt"] = _iso(_now())
            record["updatedAt"] = record["acceptedAt"]
            _write_json(data)
            return _ticket_json(record, data, with_messages=True)

    with db_transaction() as conn:
        res = conn.execute(
            sa_update(M.Ticket)
            .where(
                M.Ticket.ticket_id == int(ticket_id),
                M.Ticket.ticket_state.in_((STATE_QUEUED, STATE_OFFLINE)),
            )
            .values(
                ticket_state=STATE_ACTIVE,
                seat_id=int(seat_id),
                accepted_at=_now(),
                updated_at=_now(),
            )
        )
        if not res.rowcount:
            return None
    return get_ticket(ticket_id)


def assign_ticket(
    ticket_id: int, *, group_id: int | None, seat_id: int | None
) -> dict | None:
    if not _mysql():
        _ensure_json_seed()
        with _lock:
            data = _read_json()
            record = _find_ticket(data, ticket_id)
            if record is None:
                return None
            record["groupId"] = group_id
            record["seatId"] = seat_id
            record["updatedAt"] = _iso(_now())
            _write_json(data)
            return _ticket_json(record, data, with_messages=True)

    with db_transaction() as conn:
        res = conn.execute(
            sa_update(M.Ticket)
            .where(M.Ticket.ticket_id == int(ticket_id))
            .values(group_id=group_id, seat_id=seat_id, updated_at=_now())
        )
        if not res.rowcount:
            return None
    return get_ticket(ticket_id)


def add_message(
    ticket_id: int,
    *,
    sender_type: int,
    sender_name: str,
    content: str,
    sentiment: int = 0,
) -> dict | None:
    if not _mysql():
        _ensure_json_seed()
        with _lock:
            data = _read_json()
            record = _find_ticket(data, ticket_id)
            if record is None:
                return None
            message = _build_json_message(
                data,
                record,
                {
                    "senderType": sender_type,
                    "senderName": sender_name,
                    "content": content,
                    "sentiment": sentiment,
                },
            )
            record["messages"].append(message)
            record["lastMessageAt"] = message["createdAt"]
            record["updatedAt"] = message["createdAt"]
            if int(sentiment or 0) == 1:
                record["sentiment"] = 1
            if sender_type == SENDER_AGENT:
                if int(record.get("state") or 0) in (STATE_QUEUED, STATE_OFFLINE):
                    record["state"] = STATE_REPLIED
                    record["acceptedAt"] = record.get("acceptedAt") or message["createdAt"]
            elif sender_type == SENDER_USER and int(record.get("state") or 0) == STATE_REPLIED:
                record["state"] = STATE_ACTIVE
            _write_json(data)
            return {
                "ticket": _ticket_json(record, data, with_messages=True),
                "message": message,
            }

    now = _now()
    with db_transaction() as conn:
        record = conn.execute(
            select(
                M.Ticket.ticket_state,
                M.Ticket.sentiment,
                M.Ticket.accepted_at,
            ).where(M.Ticket.ticket_id == int(ticket_id))
        ).first()
        if record is None:
            return None
        state = int(record[0] or STATE_QUEUED)
        new_state = state
        if sender_type == SENDER_AGENT and state in (STATE_QUEUED, STATE_OFFLINE):
            new_state = STATE_REPLIED
        elif sender_type == SENDER_USER and state == STATE_REPLIED:
            new_state = STATE_ACTIVE
        values: dict = {
            "updated_at": now,
            "last_message_at": now,
            "ticket_state": new_state,
        }
        if int(sentiment or 0) == 1:
            values["sentiment"] = 1
            values["sentiment_label"] = "负面"
        if sender_type == SENDER_AGENT and record[2] is None:
            values["accepted_at"] = now
        conn.execute(
            sa_update(M.Ticket).where(M.Ticket.ticket_id == int(ticket_id)).values(**values)
        )
        res = conn.execute(
            insert(M.TicketMessage).values(
                ticket_id=int(ticket_id),
                sender_type=int(sender_type),
                sender_name=sender_name or "",
                content=content,
                sentiment=int(sentiment or 0),
                pushed_flag=0 if sender_type != SENDER_USER else 1,
                read_flag=0 if sender_type == SENDER_USER else 1,
                created_at=now,
            )
        )
        message_id = int(res.inserted_primary_key[0])
    ticket = get_ticket(ticket_id)
    message = None
    if ticket:
        message = next(
            (m for m in ticket.get("messages", []) if m["messageId"] == message_id), None
        )
    return {"ticket": ticket, "message": message}


def mark_pushed(ticket_id: int) -> int:
    """用户拉取会话后，把坐席/系统消息标记为已推送（离线回复提醒）。"""
    if not _mysql():
        _ensure_json_seed()
        with _lock:
            data = _read_json()
            record = _find_ticket(data, ticket_id)
            if record is None:
                return 0
            count = 0
            for message in record.get("messages") or []:
                if int(message.get("senderType") or 0) != SENDER_USER and not message.get("pushed"):
                    message["pushed"] = True
                    count += 1
            if count:
                _write_json(data)
            return count

    with db_transaction() as conn:
        res = conn.execute(
            sa_update(M.TicketMessage)
            .where(
                M.TicketMessage.ticket_id == int(ticket_id),
                M.TicketMessage.sender_type != SENDER_USER,
                M.TicketMessage.pushed_flag == 0,
            )
            .values(pushed_flag=1)
        )
        return int(res.rowcount or 0)


def mark_read(ticket_id: int) -> int:
    """坐席打开会话后，把用户消息标记为已读。"""
    if not _mysql():
        _ensure_json_seed()
        with _lock:
            data = _read_json()
            record = _find_ticket(data, ticket_id)
            if record is None:
                return 0
            count = 0
            for message in record.get("messages") or []:
                if int(message.get("senderType") or 0) == SENDER_USER and not message.get("read"):
                    message["read"] = True
                    count += 1
            if count:
                _write_json(data)
            return count

    with db_transaction() as conn:
        res = conn.execute(
            sa_update(M.TicketMessage)
            .where(
                M.TicketMessage.ticket_id == int(ticket_id),
                M.TicketMessage.sender_type == SENDER_USER,
                M.TicketMessage.read_flag == 0,
            )
            .values(read_flag=1)
        )
        return int(res.rowcount or 0)


def close_ticket(ticket_id: int, reason: str = "") -> dict | None:
    if not _mysql():
        _ensure_json_seed()
        with _lock:
            data = _read_json()
            record = _find_ticket(data, ticket_id)
            if record is None:
                return None
            now = _iso(_now())
            record["state"] = STATE_CLOSED
            record["closedAt"] = now
            record["updatedAt"] = now
            record["closeReason"] = reason
            _write_json(data)
            return _ticket_json(record, data, with_messages=True)

    with db_transaction() as conn:
        res = conn.execute(
            sa_update(M.Ticket)
            .where(M.Ticket.ticket_id == int(ticket_id))
            .values(
                ticket_state=STATE_CLOSED,
                closed_at=_now(),
                updated_at=_now(),
                close_reason=reason,
            )
        )
        if not res.rowcount:
            return None
    return get_ticket(ticket_id)


def overview() -> dict:
    groups = list_groups()
    seats = list_seats()
    if not _mysql():
        _ensure_json_seed()
        with _lock:
            data = _read_json()
            tickets = [_ticket_json(t, data) for t in data["tickets"]]
    else:
        with db_session() as conn:
            rows = conn.execute(select(*_TICKET_COLUMNS)).all()
            seat_rows, group_rows = _resolve_names_mysql(conn, [])
            ticket_rows = [_decorate_ticket(_ticket_row(row), seat_rows, group_rows) for row in rows]
            messages = _ticket_messages_mysql(conn, [t["ticketId"] for t in ticket_rows])
        tickets = []
        for ticket in ticket_rows:
            thread = messages.get(ticket["ticketId"], [])
            ticket.update(_status_counts(thread))
            ticket["messageCount"] = len(thread)
            tickets.append(ticket)

    state_counts = {state: 0 for state in STATE_LABELS}
    for ticket in tickets:
        state_counts[int(ticket["state"])] = state_counts.get(int(ticket["state"]), 0) + 1
    today = _now().date().isoformat()
    return {
        "total": len(tickets),
        "queued": state_counts.get(STATE_QUEUED, 0),
        "active": state_counts.get(STATE_ACTIVE, 0),
        "offline": state_counts.get(STATE_OFFLINE, 0),
        "replied": state_counts.get(STATE_REPLIED, 0),
        "closed": state_counts.get(STATE_CLOSED, 0),
        "negativeOpen": sum(
            1
            for t in tickets
            if int(t.get("sentiment") or 0) == 1
            and int(t["state"]) in (STATE_QUEUED, STATE_ACTIVE, STATE_OFFLINE, STATE_REPLIED)
        ),
        "closedToday": sum(
            1
            for t in tickets
            if int(t["state"]) == STATE_CLOSED
            and str(t.get("closedAt") or "").startswith(today)
        ),
        "unreadAgent": sum(int(t.get("unreadAgent") or 0) for t in tickets),
        "onlineSeats": sum(1 for s in seats if s.get("online")),
        "totalSeats": len(seats),
        "groups": groups,
        "seats": seats,
    }


def reply_logs(*, limit: int = 50) -> list[dict]:
    """人工回复日志（M-S-05）：按工单聚合坐席回复条数与最近时间。"""
    if not _mysql():
        _ensure_json_seed()
        with _lock:
            data = _read_json()
            tickets = [_ticket_json(t, data) for t in data["tickets"]]
            raw_messages: list[tuple[dict, dict]] = []
            for raw in data["tickets"]:
                ticket = next(
                    (t for t in tickets if t["ticketId"] == int(raw["ticketId"])), None
                )
                for message in raw.get("messages") or []:
                    if int(message.get("senderType") or 0) == SENDER_AGENT:
                        raw_messages.append((ticket or {}, message))
        grouped: dict[tuple[int, str], dict] = {}
        for ticket, message in raw_messages:
            key = (int(ticket.get("ticketId") or 0), message.get("senderName") or "")
            row = grouped.setdefault(
                key,
                {
                    "ticketId": ticket.get("ticketId"),
                    "ticketNo": ticket.get("ticketNo"),
                    "subject": ticket.get("subject"),
                    "userNo": ticket.get("userNo"),
                    "seatName": message.get("senderName") or ticket.get("seatName") or "",
                    "messageCount": 0,
                    "lastReplyAt": "",
                    "state": ticket.get("state"),
                    "stateLabel": ticket.get("stateLabel"),
                },
            )
            row["messageCount"] += 1
            if str(message.get("createdAt") or "") >= str(row["lastReplyAt"]):
                row["lastReplyAt"] = message.get("createdAt")
        rows = sorted(grouped.values(), key=lambda r: r["lastReplyAt"], reverse=True)
        return rows[: max(1, min(int(limit), 200))]

    with db_session() as conn:
        rows = conn.execute(
            select(
                M.TicketMessage.ticket_id,
                M.TicketMessage.sender_name,
                func.count(M.TicketMessage.id),
                func.max(M.TicketMessage.created_at),
            )
            .where(M.TicketMessage.sender_type == SENDER_AGENT)
            .group_by(M.TicketMessage.ticket_id, M.TicketMessage.sender_name)
            .order_by(func.max(M.TicketMessage.created_at).desc())
            .limit(max(1, min(int(limit), 200)))
        ).all()
        tickets = {
            int(row[0]): _ticket_row(row)
            for row in conn.execute(select(*_TICKET_COLUMNS)).all()
        }
    return [
        {
            "ticketId": int(row[0]),
            "ticketNo": tickets.get(int(row[0]), {}).get("ticketNo", ""),
            "subject": tickets.get(int(row[0]), {}).get("subject", ""),
            "userNo": tickets.get(int(row[0]), {}).get("userNo", ""),
            "seatName": row[1] or "",
            "messageCount": int(row[2] or 0),
            "lastReplyAt": _iso(row[3]),
            "state": tickets.get(int(row[0]), {}).get("state"),
            "stateLabel": STATE_LABELS.get(
                int(tickets.get(int(row[0]), {}).get("state") or 0), "未知"
            ),
        }
        for row in rows
    ]
