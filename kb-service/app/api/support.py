"""客服体系接口（任务 4.6 / P-B-09～13、M-S-02～06、M-SYS-04·05）。

公开端 ``/api/support``：

* ``POST /tickets`` 转人工 / 离线留言（登录；演示模式使用固定身份）；
* ``GET /tickets/{id}``、``POST /tickets/{id}/messages``、``POST /tickets/{id}/close``
  必须携带创建时下发的高熵 ``accessKey``；
* ``GET /ws`` 坐席工作台 WebSocket（鉴权走子协议，不把 token 放 URL）。

管理端 ``/api/admin/support``（RBAC：``support.view/manage``）：工单、分组、
坐席、上下线、回复日志与概览。
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)

from app.config import settings
from app.models.schemas import (
    ApiResult,
    SupportAssignRequest,
    SupportCloseRequest,
    SupportGroupRequest,
    SupportReplyRequest,
    SupportSeatRequest,
    SupportSeatStatusRequest,
    SupportTicketCreateRequest,
    SupportUserCloseRequest,
    SupportUserMessageRequest,
)
from app.security import oplog, permissions
from app.security.auth import TokenError, decode_token
from app.security.deps import Principal, get_current_user, require_admin, require_permission
from app.security.ratelimit import rate_limit
from app.services import support, support_hub
from app.db import support_store as store

logger = logging.getLogger(__name__)

admin_router = APIRouter(
    prefix="/admin/support",
    dependencies=[
        Depends(require_admin),
        Depends(require_permission),
        Depends(rate_limit("admin")),
    ],
)
# WebSocket 的 scope 里没有 Request，限流依赖只能挂在 HTTP 端点上；
# 若放到 router 级会让 /api/support/ws 建连即 500。
public_router = APIRouter(prefix="/support")
_PUBLIC_RATE_LIMIT = [Depends(rate_limit("support"))]


def _unwrap(result: dict) -> ApiResult:
    if not result.get("ok"):
        raise HTTPException(
            status_code=int(result.get("status") or 400),
            detail=result.get("error") or "操作失败",
        )
    return ApiResult(ok=True, data=result["data"])


def _disabled() -> None:
    if not settings.support_enabled:
        raise HTTPException(status_code=503, detail="客服功能未启用")


def _resolve_seat(principal: Principal, explicit: int | None) -> dict:
    seats = store.list_seats()
    if not seats:
        raise HTTPException(status_code=409, detail="尚未配置客服坐席")
    seat = None
    if explicit is not None:
        seat = next((s for s in seats if int(s["seatId"]) == int(explicit)), None)
        if seat is None:
            raise HTTPException(status_code=404, detail="坐席不存在")
    else:
        if principal.authenticated:
            seat = next(
                (
                    s
                    for s in seats
                    if (principal.user_no and s.get("userNo") == principal.user_no)
                    or (principal.user_id and s.get("userId") == principal.user_id)
                ),
                None,
            )
        if seat is None:
            seat = next((s for s in seats if s.get("online")), seats[0])
    if settings.auth_enabled and principal.authenticated and not principal.is_admin:
        if seat.get("userNo") != principal.user_no and seat.get("userId") != principal.user_id:
            raise HTTPException(status_code=403, detail="当前账号未绑定该客服坐席")
    return seat


# ------------------------------------------------------------------ 用户端
@public_router.get(
    "/status", response_model=ApiResult, summary="客服可用状态", dependencies=_PUBLIC_RATE_LIMIT
)
def support_status() -> ApiResult:
    return ApiResult(ok=True, data=support.status())


@public_router.post(
    "/tickets", response_model=ApiResult, summary="转人工 / 离线留言", dependencies=_PUBLIC_RATE_LIMIT
)
def create_ticket(
    payload: SupportTicketCreateRequest,
    principal: Principal = Depends(get_current_user),
) -> ApiResult:
    _disabled()
    return _unwrap(
        support.create_ticket(
            session_id=payload.session_id or "",
            question=payload.question,
            channel_code=payload.channel_code or "",
            role=payload.role,
            principal=principal,
        )
    )


@public_router.get(
    "/tickets/{ticket_id}",
    response_model=ApiResult,
    summary="查看人工会话",
    dependencies=_PUBLIC_RATE_LIMIT,
)
def get_ticket(
    ticket_id: int,
    access_key: str = Query(alias="accessKey", min_length=10, max_length=128),
) -> ApiResult:
    _disabled()
    return _unwrap(support.user_view(ticket_id, access_key))


@public_router.post(
    "/tickets/{ticket_id}/messages",
    response_model=ApiResult,
    summary="用户继续发言",
    dependencies=_PUBLIC_RATE_LIMIT,
)
def user_message(ticket_id: int, payload: SupportUserMessageRequest) -> ApiResult:
    _disabled()
    return _unwrap(support.submit_user_message(ticket_id, payload.access_key, payload.content))


@public_router.post(
    "/tickets/{ticket_id}/close",
    response_model=ApiResult,
    summary="用户结束会话",
    dependencies=_PUBLIC_RATE_LIMIT,
)
def user_close(ticket_id: int, payload: SupportUserCloseRequest) -> ApiResult:
    _disabled()
    return _unwrap(support.user_close(ticket_id, payload.access_key, payload.reason or ""))


# ------------------------------------------------------------------ 管理端
@admin_router.get("/overview", response_model=ApiResult, summary="客服工作台概览")
def overview() -> ApiResult:
    _disabled()
    return ApiResult(ok=True, data=support.overview())


@admin_router.get("/filters", response_model=ApiResult, summary="客服筛选项")
def filters() -> ApiResult:
    _disabled()
    return ApiResult(ok=True, data=support.filter_options())


@admin_router.get("/tickets", response_model=ApiResult, summary="工单清单")
def list_tickets(
    state: int | None = Query(default=None, ge=1, le=5),
    group_id: int | None = Query(default=None, alias="groupId", ge=1),
    seat_id: int | None = Query(default=None, alias="seatId", ge=1),
    sentiment: int | None = Query(default=None, ge=0, le=1),
    channel_code: str | None = Query(default=None, alias="channelCode", max_length=64),
    q: str | None = Query(default=None, max_length=64),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200, alias="pageSize"),
) -> ApiResult:
    _disabled()
    return ApiResult(
        ok=True,
        data=support.list_admin(
            state=state,
            group_id=group_id,
            seat_id=seat_id,
            sentiment_value=sentiment,
            channel_code=channel_code or "",
            q=q or "",
            page=page,
            page_size=page_size,
        ),
    )


@admin_router.get("/tickets/{ticket_id}", response_model=ApiResult, summary="工单详情（标记已读）")
def ticket_detail(
    ticket_id: int,
    seat_id: int | None = Query(default=None, alias="seatId", ge=1),
) -> ApiResult:
    _disabled()
    ticket = support.admin_ticket(ticket_id, seat_id=seat_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="工单不存在")
    return ApiResult(ok=True, data=ticket)


@admin_router.post("/tickets/{ticket_id}/claim", response_model=ApiResult, summary="坐席接入")
def claim_ticket(
    ticket_id: int,
    request: Request,
    seat_id: int | None = Query(default=None, alias="seatId", ge=1),
    principal: Principal = Depends(require_admin),
) -> ApiResult:
    _disabled()
    seat = _resolve_seat(principal, seat_id)
    result = support.claim(ticket_id, int(seat["seatId"]))
    if result.get("ok"):
        oplog.log_from_request(
            request,
            oplog.ACTION_SUPPORT_CLAIM,
            "ticket",
            ticket_id,
            user_id=principal.user_id,
            detail={"seatId": seat["seatId"], "seatName": seat["seatName"]},
        )
    return _unwrap(result)


@admin_router.post("/tickets/{ticket_id}/reply", response_model=ApiResult, summary="坐席回复")
def reply_ticket(
    ticket_id: int,
    payload: SupportReplyRequest,
    request: Request,
    principal: Principal = Depends(require_admin),
) -> ApiResult:
    _disabled()
    seat = _resolve_seat(principal, payload.seat_id)
    result = support.reply(ticket_id, int(seat["seatId"]), payload.content)
    if result.get("ok"):
        oplog.log_from_request(
            request,
            oplog.ACTION_SUPPORT_REPLY,
            "ticket",
            ticket_id,
            user_id=principal.user_id,
            detail={"seatId": seat["seatId"], "length": len(payload.content)},
        )
    return _unwrap(result)


@admin_router.post("/tickets/{ticket_id}/close", response_model=ApiResult, summary="结束会话")
def close_ticket(
    ticket_id: int,
    payload: SupportCloseRequest,
    request: Request,
    principal: Principal = Depends(require_admin),
) -> ApiResult:
    _disabled()
    seat_id = None
    if payload.seat_id is not None:
        seat_id = int(_resolve_seat(principal, payload.seat_id)["seatId"])
    elif principal.authenticated and not principal.is_admin:
        seat_id = int(_resolve_seat(principal, None)["seatId"])
    result = support.close(ticket_id, seat_id, payload.reason or "")
    if result.get("ok"):
        oplog.log_from_request(
            request,
            oplog.ACTION_SUPPORT_CLOSE,
            "ticket",
            ticket_id,
            user_id=principal.user_id,
            detail={"seatId": seat_id},
        )
    return _unwrap(result)


@admin_router.post("/tickets/{ticket_id}/assign", response_model=ApiResult, summary="转交分组 / 坐席")
def assign_ticket(
    ticket_id: int,
    payload: SupportAssignRequest,
    request: Request,
    principal: Principal = Depends(require_admin),
) -> ApiResult:
    _disabled()
    result = support.assign(
        ticket_id, group_id=payload.group_id, seat_id=payload.seat_id
    )
    if result.get("ok"):
        oplog.log_from_request(
            request,
            oplog.ACTION_SUPPORT_ASSIGN,
            "ticket",
            ticket_id,
            user_id=principal.user_id,
            detail={"groupId": payload.group_id, "seatId": payload.seat_id},
        )
    return _unwrap(result)


@admin_router.get("/reply-logs", response_model=ApiResult, summary="人工回复日志")
def reply_logs(limit: int = Query(default=50, ge=1, le=200)) -> ApiResult:
    _disabled()
    return ApiResult(ok=True, data=support.reply_logs(limit))


@admin_router.get("/groups", response_model=ApiResult, summary="客服分组")
def list_groups() -> ApiResult:
    return ApiResult(ok=True, data=store.list_groups())


@admin_router.post("/groups", response_model=ApiResult, summary="新建客服分组")
def create_group(payload: SupportGroupRequest) -> ApiResult:
    return _unwrap(
        support.create_group(payload.model_dump(by_alias=True, exclude_unset=True))
    )


@admin_router.patch("/groups/{group_id}", response_model=ApiResult, summary="更新客服分组")
def update_group(group_id: int, payload: SupportGroupRequest) -> ApiResult:
    return _unwrap(
        support.update_group(group_id, payload.model_dump(by_alias=True, exclude_unset=True))
    )


@admin_router.delete("/groups/{group_id}", response_model=ApiResult, summary="删除客服分组")
def delete_group(group_id: int) -> ApiResult:
    return _unwrap(support.delete_group(group_id))


@admin_router.get("/seats", response_model=ApiResult, summary="客服坐席")
def list_seats() -> ApiResult:
    return ApiResult(ok=True, data=store.list_seats())


@admin_router.post("/seats", response_model=ApiResult, summary="新增客服坐席")
def create_seat(payload: SupportSeatRequest) -> ApiResult:
    return _unwrap(support.create_seat(payload.to_payload(creating=True)))


@admin_router.patch("/seats/{seat_id}", response_model=ApiResult, summary="更新客服坐席")
def update_seat(seat_id: int, payload: SupportSeatRequest) -> ApiResult:
    return _unwrap(support.update_seat(seat_id, payload.to_payload()))


@admin_router.post("/seats/{seat_id}/status", response_model=ApiResult, summary="坐席上线 / 下线")
def seat_status(seat_id: int, payload: SupportSeatStatusRequest) -> ApiResult:
    return _unwrap(support.seat_status(seat_id, payload.online))


@admin_router.delete("/seats/{seat_id}", response_model=ApiResult, summary="删除客服坐席")
def delete_seat(seat_id: int) -> ApiResult:
    return _unwrap(support.delete_seat(seat_id))


# ------------------------------------------------------------------ WebSocket
def _ws_principal(websocket: WebSocket) -> tuple[Principal, bool]:
    offered = list(websocket.scope.get("subprotocols") or [])
    token = ""
    if len(offered) >= 2 and offered[0] == "nxz":
        token = offered[1]
    if not token:
        return Principal(), not settings.auth_enabled
    try:
        payload = decode_token(token)
    except TokenError:
        return Principal(), False
    role = str(payload.get("role") or "")
    principal = Principal(
        user_no=str(payload.get("sub") or ""),
        role=role,
        user_id=payload.get("uid"),
        authenticated=True,
        role_id=int(payload["rid"]) if isinstance(payload.get("rid"), int) else None,
        manager=bool(payload.get("mgr")),
    )
    if principal.is_admin:
        return principal, True
    if not settings.auth_enabled:
        return principal, True
    if not principal.manager:
        return principal, False
    return principal, permissions.has(principal, "support.manage")


@public_router.websocket("/ws")
async def support_ws(
    websocket: WebSocket,
    seat_id: int | None = Query(default=None, alias="seatId", ge=1),
) -> None:
    if not settings.support_enabled:
        await websocket.close(code=4503, reason="客服功能未启用")
        return
    principal, allowed = _ws_principal(websocket)
    if not allowed:
        await websocket.close(code=4403, reason="无权连接客服工作台")
        return
    if settings.auth_enabled and not principal.authenticated:
        await websocket.close(code=4401, reason="需要登录")
        return

    seats = store.list_seats()
    seat = None
    if seat_id is not None:
        seat = next((s for s in seats if int(s["seatId"]) == int(seat_id)), None)
    if seat is None and principal.authenticated:
        seat = next(
            (
                s
                for s in seats
                if (principal.user_no and s.get("userNo") == principal.user_no)
                or (principal.user_id and s.get("userId") == principal.user_id)
            ),
            None,
        )
    if seat is None:
        seat = next((s for s in seats if s.get("online")), seats[0] if seats else None)
    if seat is None:
        await websocket.close(code=4409, reason="尚未配置客服坐席")
        return
    if settings.auth_enabled and principal.authenticated and not principal.is_admin:
        if seat.get("userNo") != principal.user_no and seat.get("userId") != principal.user_id:
            await websocket.close(code=4403, reason="当前账号未绑定该坐席")
            return

    offered = list(websocket.scope.get("subprotocols") or [])
    await websocket.accept(subprotocol="nxz" if "nxz" in offered else None)
    store.set_seat_online(int(seat["seatId"]), True)
    sub_id, queue = support_hub.subscribe(seat_id=int(seat["seatId"]))
    try:
        await websocket.send_text(
            json.dumps(
                {
                    "type": "hello",
                    "seatId": seat["seatId"],
                    "seatName": seat["seatName"],
                    "online": True,
                    "serverTime": store._iso(store._now()),
                },
                ensure_ascii=False,
            )
        )
        while True:
            try:
                event = await asyncio.wait_for(
                    queue.get(), timeout=max(5, settings.support_ws_heartbeat)
                )
                await websocket.send_text(json.dumps(event, ensure_ascii=False))
            except asyncio.TimeoutError:
                await websocket.send_text(
                    json.dumps(
                        {"type": "ping", "serverTime": store._iso(store._now())},
                        ensure_ascii=False,
                    )
                )
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        support_hub.unsubscribe(sub_id)
        if support_hub.seat_connection_count(int(seat["seatId"])) == 0:
            store.set_seat_online(int(seat["seatId"]), False)
        try:
            await websocket.close()
        except RuntimeError:
            pass
