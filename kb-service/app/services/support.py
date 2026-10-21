"""客服体系业务层（任务 4.6 / P-B-09～13、M-S-02～06、M-SYS-04·05）。

职责：

* **转人工**：按会话幂等创建工单；按渠道匹配客服分组，组内挑在线且未满
  负载的坐席；无人在线转离线留言。
* **工单生命周期**：待接入 → 接入（接待中）→ 回复 / 结束；离线工单回复后
  标记「已回复」，用户回话自动恢复接待中。
* **情绪打标**：用户首条消息与后续消息都过 :mod:`app.services.sentiment`，
  工单取「出现过负面」为准，供工作台筛选与 3.10 统计。
* **工作台**：分组 / 坐席维护、上下线、接入、回复、转交、结束、回复日志。
* **安全**：用户侧读写必须携带创建时下发的一次性高熵凭证；服务端只存
  SHA-256，不把凭证写进日志或工单响应。

返回风格与导办 / 应用一致：``{"ok": bool, "data": ...}``，错误带 ``status``。
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime

from app.config import settings
from app.db import support_store as store
from app.services import sentiment, support_hub

logger = logging.getLogger(__name__)

MAX_SUBJECT = 255
MAX_GROUP_NAME = 32
MAX_SEAT_NAME = 32
MAX_CHANNEL_IDS = 255
MAX_DESCRIPTION = 255
MAX_REASON = 120

HANDOFF_WORDS = (
    "转人工",
    "人工客服",
    "真人客服",
    "找人工",
    "人工服务",
    "人工坐席",
    "客服在吗",
    "联系客服",
    "我要投诉",
    "投诉",
)

STATE_LABELS = store.STATE_LABELS


def _ok(data) -> dict:
    return {"ok": True, "data": data}


def _err(message: str, status: int = 400) -> dict:
    return {"ok": False, "status": status, "error": message}


def enabled() -> bool:
    return bool(settings.support_enabled)


def status() -> dict:
    """公开状态：门户据此决定是否展示「转人工」入口。"""
    seats = store.list_seats() if enabled() else []
    return {
        "enabled": enabled(),
        "onlineSeats": sum(1 for s in seats if s.get("online")),
        "totalSeats": len(seats),
    }


def handoff_hint(question: str) -> dict:
    """语义触发提示（P-B-11）：命中明确「找真人」表达时建议转接。"""
    text = str(question or "").strip()
    hits = [word for word in HANDOFF_WORDS if word in text]
    return {
        "suggested": bool(hits),
        "keywords": hits[:3],
        "reason": "检测到人工客服诉求" if hits else "",
    }


# ================================================================== 校验
def _text(value, *, name: str, max_len: int, required: bool = False) -> str:
    content = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value or "")).strip()
    if required and not content:
        raise ValueError(f"{name}不能为空")
    if len(content) > max_len:
        raise ValueError(f"{name}不能超过 {max_len} 字")
    return content


def _status(value) -> int:
    try:
        state = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("状态取值不合法") from exc
    if state not in (0, 1):
        raise ValueError("状态只能是 0 或 1")
    return state


def _group_id(value) -> int | None:
    if value in (None, "", "0", 0):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("分组不合法") from exc


def _seat_id(value) -> int | None:
    if value in (None, "", "0", 0):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("坐席不合法") from exc


# ================================================================== 转人工
def _identity(principal, role: str | None) -> tuple[str, str, str, int | None]:
    if principal is not None and getattr(principal, "authenticated", False):
        user_no = str(principal.user_no or "").strip()
        return user_no, user_no or "校园用户", str(role or principal.role or "访客"), principal.user_id
    if settings.auth_enabled:
        raise PermissionError("请先登录后再转人工")
    # 演示模式：与问数 / 导办保持一致，使用固定演示身份
    return "20230001", "演示用户", str(role or "本科生"), None


def _match_group(channel_code: str) -> dict | None:
    groups = [g for g in store.list_groups() if int(g.get("status") or 0) == 1]
    if not groups:
        return None
    code = str(channel_code or "").strip()
    if code:
        for group in groups:
            codes = [item.strip() for item in str(group.get("channelIds") or "").split(",")]
            if code in codes:
                return group
    return next(
        (g for g in groups if not str(g.get("channelIds") or "").strip()),
        groups[0],
    )


def _pick_seat(group: dict | None) -> dict | None:
    if group is None:
        return None
    seats = [
        seat
        for seat in store.list_seats()
        if int(seat.get("groupId") or 0) == int(group["groupId"])
        and seat.get("online")
        and int(seat.get("activeCount") or 0) < int(seat.get("maxSessions") or 5)
    ]
    if not seats:
        return None
    seats.sort(key=lambda s: (int(s.get("activeCount") or 0), int(s["seatId"])))
    return seats[0]


def create_ticket(
    *,
    session_id: str,
    question: str,
    channel_code: str,
    role: str | None,
    principal=None,
) -> dict:
    if not enabled():
        return _err("客服功能未启用", 503)
    try:
        subject = _text(question, name="问题描述", max_len=500, required=True)
        user_no, user_name, role_name, user_id = _identity(principal, role)
    except ValueError as exc:
        return _err(str(exc))
    except PermissionError as exc:
        return _err(str(exc), 401)

    sid = _text(session_id, name="会话", max_len=64) or f"support-{uuid.uuid4().hex[:12]}"
    channel = _text(channel_code, name="渠道", max_len=64)

    existing = store.get_open_ticket_by_session(sid)
    access_key = store.new_access_key()
    if existing is not None:
        # 同一会话重复转人工：复用进行中的工单，并轮换用户凭证（旧标签页需重新转接）
        store.set_ticket_access(existing["ticketId"], access_key)
        ticket = store.get_ticket(existing["ticketId"]) or existing
        support_hub.publish({"type": "ticket.updated", "ticket": _event_ticket(ticket)})
        return _ok({"ticket": ticket, "accessKey": access_key, "reused": True})

    group = _match_group(channel)
    seat = _pick_seat(group)
    state = store.STATE_QUEUED if seat else store.STATE_OFFLINE
    ticket = store.create_ticket(
        {
            "sessionId": sid,
            "channelCode": channel,
            "userId": user_id,
            "userNo": user_no,
            "userName": user_name,
            "role": role_name,
            "seatId": int(seat["seatId"]) if seat else None,
            "groupId": int(group["groupId"]) if group else None,
            "state": state,
            "sentiment": sentiment.tag(subject),
            "subject": subject[:MAX_SUBJECT],
            "accessKeyHash": store.hash_key(access_key),
        },
        first_message={
            "senderType": store.SENDER_USER,
            "senderName": user_name,
            "content": subject,
            "sentiment": sentiment.tag(subject),
        },
    )
    notice = (
        f"已为您接入客服「{seat['seatName']}」，请稍候。"
        if seat
        else "当前客服均离线，已转为留言，客服上线后会尽快回复。"
    )
    added = store.add_message(
        ticket["ticketId"],
        sender_type=store.SENDER_SYSTEM,
        sender_name="系统",
        content=notice,
    )
    ticket = (added or {}).get("ticket") or store.get_ticket(ticket["ticketId"], with_messages=True)
    support_hub.publish({"type": "ticket.created", "ticket": _event_ticket(ticket)})
    return _ok({"ticket": ticket, "accessKey": access_key, "reused": False})


def _event_ticket(ticket: dict | None) -> dict | None:
    if not ticket:
        return None
    return {
        "ticketId": ticket.get("ticketId"),
        "ticketNo": ticket.get("ticketNo"),
        "state": ticket.get("state"),
        "stateLabel": ticket.get("stateLabel"),
        "seatId": ticket.get("seatId"),
        "seatName": ticket.get("seatName"),
        "groupId": ticket.get("groupId"),
        "sentiment": ticket.get("sentiment"),
        "subject": ticket.get("subject"),
        "userNo": ticket.get("userNo"),
        "updatedAt": ticket.get("updatedAt"),
    }


def user_view(ticket_id: int, access_key: str, *, mark_pushed: bool = True) -> dict:
    if not store.verify_ticket_key(ticket_id, access_key or ""):
        return _err("工单凭证无效", 403)
    before = store.get_ticket(ticket_id)
    if before is None:
        return _err("工单不存在", 404)
    new_replies = sum(
        1
        for message in before.get("messages") or []
        if int(message.get("senderType") or 0) == store.SENDER_AGENT
        and not message.get("pushed")
    )
    if mark_pushed:
        store.mark_pushed(ticket_id)
    ticket = store.get_ticket(ticket_id)
    notice = None
    if ticket and int(ticket.get("state") or 0) == store.STATE_ACTIVE and ticket.get("acceptedAt"):
        notice = {
            "kind": "agent_joined",
            "text": f"客服「{ticket.get('seatName') or '坐席'}」已接入",
        }
    elif new_replies:
        notice = {"kind": "new_reply", "text": f"收到 {new_replies} 条客服回复"}
    elif ticket and int(ticket.get("state") or 0) == store.STATE_CLOSED:
        notice = {"kind": "closed", "text": "本次人工会话已结束"}
    return _ok({"ticket": ticket, "newReplies": new_replies, "notice": notice})


def submit_user_message(ticket_id: int, access_key: str, content: str) -> dict:
    if not enabled():
        return _err("客服功能未启用", 503)
    if not store.verify_ticket_key(ticket_id, access_key or ""):
        return _err("工单凭证无效", 403)
    ticket = store.get_ticket(ticket_id)
    if ticket is None:
        return _err("工单不存在", 404)
    if int(ticket["state"]) == store.STATE_CLOSED:
        return _err("会话已结束，请重新发起转人工", 409)
    try:
        text = _text(content, name="消息", max_len=settings.support_max_message, required=True)
    except ValueError as exc:
        return _err(str(exc))
    result = store.add_message(
        ticket_id,
        sender_type=store.SENDER_USER,
        sender_name=ticket.get("userName") or "用户",
        content=text,
        sentiment=sentiment.tag(text),
    )
    if result is None:
        return _err("工单不存在", 404)
    support_hub.publish(
        {
            "type": "ticket.message",
            "ticketId": ticket_id,
            "message": result.get("message"),
            "ticket": _event_ticket(result.get("ticket")),
        }
    )
    return _ok(result)


def user_close(ticket_id: int, access_key: str, reason: str = "") -> dict:
    if not store.verify_ticket_key(ticket_id, access_key or ""):
        return _err("工单凭证无效", 403)
    ticket = store.get_ticket(ticket_id)
    if ticket is None:
        return _err("工单不存在", 404)
    if int(ticket["state"]) == store.STATE_CLOSED:
        return _ok(ticket)
    try:
        text = _text(reason, name="结束原因", max_len=MAX_REASON)
    except ValueError as exc:
        return _err(str(exc))
    store.add_message(
        ticket_id,
        sender_type=store.SENDER_SYSTEM,
        sender_name="系统",
        content="用户已结束本次人工会话。",
    )
    result = store.close_ticket(ticket_id, text or "用户主动结束")
    support_hub.publish({"type": "ticket.closed", "ticket": _event_ticket(result)})
    return _ok(result)


# ================================================================== 工作台
def overview() -> dict:
    data = store.overview()
    data.update(
        {
            "enabled": enabled(),
            "wsConnections": support_hub.connection_count(),
            "states": [
                {"state": key, "label": label} for key, label in store.STATE_LABELS.items()
            ],
            "sentiments": [
                {"value": 0, "label": "正常"},
                {"value": 1, "label": "负面"},
            ],
        }
    )
    return data


def list_admin(
    *,
    state: int | None = None,
    group_id: int | None = None,
    seat_id: int | None = None,
    sentiment_value: int | None = None,
    channel_code: str = "",
    q: str = "",
    page: int = 1,
    page_size: int = 20,
) -> dict:
    result = store.list_tickets(
        state=state,
        group_id=group_id,
        seat_id=seat_id,
        sentiment=sentiment_value,
        channel_code=channel_code,
        q=q,
        page=page,
        page_size=page_size,
    )
    return {**result, "overview": store.overview()}


def admin_ticket(ticket_id: int, *, seat_id: int | None = None) -> dict | None:
    ticket = store.get_ticket(ticket_id)
    if ticket is None:
        return None
    store.mark_read(ticket_id)
    return store.get_ticket(ticket_id)


def claim(ticket_id: int, seat_id: int) -> dict:
    seat = store.get_seat(seat_id)
    if seat is None:
        return _err("坐席不存在", 404)
    if not seat.get("online"):
        return _err("坐席已离线，请先上线", 409)
    ticket = store.get_ticket(ticket_id)
    if ticket is None:
        return _err("工单不存在", 404)
    if int(ticket["state"]) == store.STATE_CLOSED:
        return _err("工单已结束", 409)
    if ticket.get("seatId") and int(ticket["seatId"]) != int(seat_id):
        return _err(f"工单已分配给「{ticket.get('seatName') or '其他坐席'}」", 409)
    result = store.claim_ticket(ticket_id, seat_id)
    if result is None:
        return _err("工单已被其他坐席接入", 409)
    store.add_message(
        ticket_id,
        sender_type=store.SENDER_SYSTEM,
        sender_name="系统",
        content=f"客服「{seat['seatName']}」已接入会话。",
    )
    ticket = store.get_ticket(ticket_id)
    support_hub.publish({"type": "ticket.claimed", "ticket": _event_ticket(ticket)})
    return _ok(ticket)


def reply(ticket_id: int, seat_id: int, content: str) -> dict:
    seat = store.get_seat(seat_id)
    if seat is None:
        return _err("坐席不存在", 404)
    if not seat.get("online"):
        return _err("坐席已离线，请先上线", 409)
    ticket = store.get_ticket(ticket_id)
    if ticket is None:
        return _err("工单不存在", 404)
    if int(ticket["state"]) == store.STATE_CLOSED:
        return _err("工单已结束", 409)
    if ticket.get("seatId") and int(ticket["seatId"]) != int(seat_id):
        return _err(f"工单已分配给「{ticket.get('seatName') or '其他坐席'}」", 409)
    try:
        text = _text(content, name="回复内容", max_len=settings.support_max_message, required=True)
    except ValueError as exc:
        return _err(str(exc))

    if int(ticket["state"]) == store.STATE_OFFLINE:
        # 离线留言：直接认领坐席并回复，工单进入「已回复」等待用户查看
        store.assign_ticket(ticket_id, group_id=seat.get("groupId"), seat_id=seat_id)
        result = store.add_message(
            ticket_id,
            sender_type=store.SENDER_AGENT,
            sender_name=seat["seatName"],
            content=text,
        )
    else:
        if int(ticket["state"]) == store.STATE_QUEUED:
            claimed = store.claim_ticket(ticket_id, seat_id)
            if claimed is None:
                return _err("工单已被其他坐席接入", 409)
            store.add_message(
                ticket_id,
                sender_type=store.SENDER_SYSTEM,
                sender_name="系统",
                content=f"客服「{seat['seatName']}」已接入会话。",
            )
        result = store.add_message(
            ticket_id,
            sender_type=store.SENDER_AGENT,
            sender_name=seat["seatName"],
            content=text,
        )
    if result is None:
        return _err("工单不存在", 404)
    support_hub.publish(
        {
            "type": "ticket.message",
            "ticketId": ticket_id,
            "message": result.get("message"),
            "ticket": _event_ticket(result.get("ticket")),
        }
    )
    return _ok(result)


def assign(ticket_id: int, *, group_id: int | None, seat_id: int | None) -> dict:
    ticket = store.get_ticket(ticket_id)
    if ticket is None:
        return _err("工单不存在", 404)
    if int(ticket["state"]) == store.STATE_CLOSED:
        return _err("工单已结束", 409)
    if group_id is not None and store.get_group(group_id) is None:
        return _err("分组不存在", 404)
    if seat_id is not None:
        seat = store.get_seat(seat_id)
        if seat is None:
            return _err("坐席不存在", 404)
        if group_id is None:
            group_id = seat.get("groupId")
        if group_id is not None and int(seat.get("groupId") or 0) != int(group_id):
            return _err("坐席不属于该分组")
    elif group_id is not None:
        seat = _pick_seat(store.get_group(group_id))
        seat_id = int(seat["seatId"]) if seat else None
    result = store.assign_ticket(ticket_id, group_id=group_id, seat_id=seat_id)
    support_hub.publish({"type": "ticket.assigned", "ticket": _event_ticket(result)})
    return _ok(result)


def close(ticket_id: int, seat_id: int | None, reason: str = "") -> dict:
    ticket = store.get_ticket(ticket_id)
    if ticket is None:
        return _err("工单不存在", 404)
    if int(ticket["state"]) == store.STATE_CLOSED:
        return _ok(ticket)
    if seat_id is not None and ticket.get("seatId") and int(ticket["seatId"]) != int(seat_id):
        return _err("只能结束自己接待的工单", 409)
    try:
        text = _text(reason, name="结束原因", max_len=MAX_REASON)
    except ValueError as exc:
        return _err(str(exc))
    store.add_message(
        ticket_id,
        sender_type=store.SENDER_SYSTEM,
        sender_name="系统",
        content="客服已结束本次会话。" + (f"（{text}）" if text else ""),
    )
    result = store.close_ticket(ticket_id, text or "客服结束")
    support_hub.publish({"type": "ticket.closed", "ticket": _event_ticket(result)})
    return _ok(result)


# ================================================================== 分组 / 坐席
def create_group(raw: dict) -> dict:
    try:
        name = _text(raw.get("groupName"), name="分组名称", max_len=MAX_GROUP_NAME, required=True)
        if len(name) < 2:
            raise ValueError("分组名称至少 2 个字")
        channels = _text(raw.get("channelIds"), name="渠道编码", max_len=MAX_CHANNEL_IDS)
        description = _text(raw.get("description"), name="分组说明", max_len=MAX_DESCRIPTION)
        status = _status(raw.get("status", 1))
    except ValueError as exc:
        return _err(str(exc))
    try:
        return _ok(store.create_group({
            "groupName": name,
            "channelIds": channels,
            "description": description,
            "status": status,
        }))
    except ValueError as exc:
        return _err(str(exc), 409)


def update_group(group_id: int, raw: dict) -> dict:
    try:
        fields: dict = {}
        if "groupName" in raw:
            name = _text(raw.get("groupName"), name="分组名称", max_len=MAX_GROUP_NAME, required=True)
            if len(name) < 2:
                raise ValueError("分组名称至少 2 个字")
            fields["groupName"] = name
        if "channelIds" in raw:
            fields["channelIds"] = _text(raw.get("channelIds"), name="渠道编码", max_len=MAX_CHANNEL_IDS)
        if "description" in raw:
            fields["description"] = _text(raw.get("description"), name="分组说明", max_len=MAX_DESCRIPTION)
        if "status" in raw:
            fields["status"] = _status(raw.get("status"))
    except ValueError as exc:
        return _err(str(exc))
    try:
        result = store.update_group(group_id, fields)
    except ValueError as exc:
        return _err(str(exc), 409)
    if result is None:
        return _err("分组不存在", 404)
    return _ok(result)


def delete_group(group_id: int) -> dict:
    if store.get_group(group_id) is None:
        return _err("分组不存在", 404)
    if store.group_seat_count(group_id):
        return _err("请先移除该分组下的坐席", 409)
    store.delete_group(group_id)
    return _ok({"deleted": group_id})


def create_seat(raw: dict) -> dict:
    try:
        user_no = _text(raw.get("userNo"), name="学工号", max_len=64, required=True)
        seat_name = _text(raw.get("seatName"), name="坐席昵称", max_len=MAX_SEAT_NAME, required=True)
        if len(seat_name) < 2:
            raise ValueError("坐席昵称至少 2 个字")
        group_id = _group_id(raw.get("groupId"))
        channels = _text(raw.get("channelIds"), name="渠道编码", max_len=MAX_CHANNEL_IDS)
        try:
            max_sessions = int(raw.get("maxSessions", 5))
        except (TypeError, ValueError) as exc:
            raise ValueError("最大接待数不合法") from exc
        if not 1 <= max_sessions <= 20:
            raise ValueError("最大接待数需在 1–20 之间")
        online = bool(raw.get("online"))
    except ValueError as exc:
        return _err(str(exc))
    if group_id is not None and store.get_group(group_id) is None:
        return _err("分组不存在", 404)
    try:
        return _ok(store.create_seat({
            "userNo": user_no,
            "seatName": seat_name,
            "groupId": group_id,
            "channelIds": channels,
            "maxSessions": max_sessions,
            "online": online,
            "userId": int(raw.get("userId") or 0),
        }))
    except ValueError as exc:
        return _err(str(exc), 409)


def update_seat(seat_id: int, raw: dict) -> dict:
    try:
        fields: dict = {}
        if "userNo" in raw:
            fields["userNo"] = _text(raw.get("userNo"), name="学工号", max_len=64, required=True)
        if "seatName" in raw:
            name = _text(raw.get("seatName"), name="坐席昵称", max_len=MAX_SEAT_NAME, required=True)
            if len(name) < 2:
                raise ValueError("坐席昵称至少 2 个字")
            fields["seatName"] = name
        if "groupId" in raw:
            group_id = _group_id(raw.get("groupId"))
            if group_id is not None and store.get_group(group_id) is None:
                return _err("分组不存在", 404)
            fields["groupId"] = group_id
        if "channelIds" in raw:
            fields["channelIds"] = _text(raw.get("channelIds"), name="渠道编码", max_len=MAX_CHANNEL_IDS)
        if "maxSessions" in raw:
            try:
                max_sessions = int(raw.get("maxSessions"))
            except (TypeError, ValueError) as exc:
                raise ValueError("最大接待数不合法") from exc
            if not 1 <= max_sessions <= 20:
                raise ValueError("最大接待数需在 1–20 之间")
            fields["maxSessions"] = max_sessions
        if "online" in raw:
            fields["online"] = bool(raw.get("online"))
    except ValueError as exc:
        return _err(str(exc))
    try:
        result = store.update_seat(seat_id, fields)
    except ValueError as exc:
        return _err(str(exc), 409)
    if result is None:
        return _err("坐席不存在", 404)
    if "online" in fields and not fields["online"]:
        _requeue_offline(seat_id)
    support_hub.publish({"type": "seat.updated", "seat": result})
    return _ok(result)


def _requeue_offline(seat_id: int) -> None:
    """坐席下线：待接入工单转给同组在线坐席；无人可接则退回离线留言。"""
    queued = store.list_tickets(state=store.STATE_QUEUED, seat_id=seat_id, page_size=200)
    for ticket in queued.get("items") or []:
        group = store.get_group(ticket.get("groupId") or 0)
        seat = _pick_seat(group) if group else None
        if seat is not None and int(seat["seatId"]) == int(seat_id):
            seat = None
        if seat is not None:
            store.assign_ticket(
                ticket["ticketId"], group_id=group["groupId"], seat_id=seat["seatId"]
            )
        else:
            store.assign_ticket(ticket["ticketId"], group_id=ticket.get("groupId"), seat_id=None)
            store.add_message(
                ticket["ticketId"],
                sender_type=store.SENDER_SYSTEM,
                sender_name="系统",
                content="客服已离线，您的留言已保留，客服上线后会尽快回复。",
            )


def delete_seat(seat_id: int) -> dict:
    seat = store.get_seat(seat_id)
    if seat is None:
        return _err("坐席不存在", 404)
    if store.seat_active_count(seat_id):
        return _err("该坐席还有进行中的工单，请先转交或结束", 409)
    store.delete_seat(seat_id)
    return _ok({"deleted": seat_id})


def seat_status(seat_id: int, online: bool) -> dict:
    return update_seat(seat_id, {"online": bool(online)})


def reply_logs(limit: int = 50) -> list[dict]:
    return store.reply_logs(limit=limit)


def filter_options() -> dict:
    return {
        "states": [
            {"value": key, "label": label} for key, label in store.STATE_LABELS.items()
        ],
        "sentiments": [
            {"value": 0, "label": "正常"},
            {"value": 1, "label": "负面"},
        ],
        "groups": store.list_groups(),
        "seats": store.list_seats(),
    }
