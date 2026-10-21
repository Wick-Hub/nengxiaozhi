"""渠道分身接口（任务 3.1 / 3.2 / M-C-01·02·04）。

管理端挂在 ``/api/admin/channels``（RBAC：``channels.view/manage``）；
公开端 ``/api/channels/{code}`` 供对话页/门户/iframe 读取渠道配置，**无需登录**
——认证开关 ``needAuth`` 只作为配置透传，真正的登录拦截在门户与 4.1 身份适配层。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.models.schemas import (
    ApiResult,
    ChannelCopyRequest,
    ChannelCreateRequest,
    ChannelUpdateRequest,
)
from app.security import oplog
from app.security.deps import Principal, require_admin, require_permission
from app.security.ratelimit import rate_limit
from app.services import channel_admin

admin_router = APIRouter(
    prefix="/admin",
    dependencies=[
        Depends(require_admin),
        Depends(require_permission),
        Depends(rate_limit("admin")),
    ],
)
public_router = APIRouter(prefix="/channels", dependencies=[Depends(rate_limit("channels"))])


def _unwrap(result: dict) -> ApiResult:
    if not result.get("ok"):
        raise HTTPException(
            status_code=int(result.get("status") or 400),
            detail=result.get("error") or "操作失败",
        )
    return ApiResult(ok=True, data=result["data"])


def _base(request: Request) -> str:
    return str(request.base_url).rstrip("/")


@admin_router.get("/channels", response_model=ApiResult, summary="渠道清单（M-C-01）")
def list_channels(request: Request) -> ApiResult:
    return _unwrap(channel_admin.list_channels(_base(request)))


@admin_router.get("/channels/{channel_id}", response_model=ApiResult, summary="渠道详情（M-C-01）")
def get_channel(channel_id: int, request: Request) -> ApiResult:
    return _unwrap(channel_admin.get_channel(channel_id, _base(request)))


@admin_router.post("/channels", response_model=ApiResult, summary="新建渠道（M-C-01）")
def create_channel(
    payload: ChannelCreateRequest,
    request: Request,
    principal: Principal = Depends(require_admin),
) -> ApiResult:
    result = channel_admin.create_channel(payload.to_payload(), base_url=_base(request))
    if result["ok"]:
        item = result["data"]
        oplog.log_from_request(
            request,
            oplog.ACTION_CHANNEL_CREATE,
            "channel",
            item["channelId"],
            user_id=principal.user_id,
            detail={
                "channelCode": item["channelCode"],
                "channelName": item["channelName"],
                "defaultAgentId": item["defaultAgentId"],
            },
        )
    return _unwrap(result)


@admin_router.post(
    "/channels/{channel_id}/copy",
    response_model=ApiResult,
    summary="复制渠道配置（M-C-04）",
)
def copy_channel(
    channel_id: int,
    payload: ChannelCopyRequest,
    request: Request,
    principal: Principal = Depends(require_admin),
) -> ApiResult:
    result = channel_admin.copy_channel(
        channel_id, payload.to_payload(), base_url=_base(request)
    )
    if result["ok"]:
        item = result["data"]
        oplog.log_from_request(
            request,
            oplog.ACTION_CHANNEL_COPY,
            "channel",
            item["channelId"],
            user_id=principal.user_id,
            detail={
                "sourceChannelId": channel_id,
                "channelCode": item["channelCode"],
                "channelName": item["channelName"],
            },
        )
    return _unwrap(result)


@admin_router.patch(
    "/channels/{channel_id}", response_model=ApiResult, summary="更新渠道 / 绑定助手（M-C-01）"
)
def update_channel(
    channel_id: int,
    payload: ChannelUpdateRequest,
    request: Request,
    principal: Principal = Depends(require_admin),
) -> ApiResult:
    result = channel_admin.update_channel(
        channel_id, payload.to_payload(), base_url=_base(request)
    )
    if result["ok"]:
        item = result["data"]
        oplog.log_from_request(
            request,
            oplog.ACTION_CHANNEL_UPDATE,
            "channel",
            channel_id,
            user_id=principal.user_id,
            detail={
                "channelCode": item["channelCode"],
                "status": item["status"],
                "needAuth": item["needAuth"],
                "defaultAgentId": item["defaultAgentId"],
                "agentIds": item["agentIds"],
                "themeKeys": sorted((item.get("themeConfig") or {}).keys()),
                "routeRoles": [
                    r.get("role")
                    for r in (item.get("routeConfig") or {}).get("routes") or []
                    if isinstance(r, dict)
                ],
            },
        )
    return _unwrap(result)


@admin_router.delete(
    "/channels/{channel_id}", response_model=ApiResult, summary="删除渠道（M-C-01）"
)
def delete_channel(
    channel_id: int,
    request: Request,
    principal: Principal = Depends(require_admin),
) -> ApiResult:
    existing = channel_admin.get_channel(channel_id)
    result = channel_admin.delete_channel(channel_id)
    if result["ok"]:
        detail = {}
        if existing.get("ok"):
            detail = {
                "channelCode": existing["data"]["channelCode"],
                "channelName": existing["data"]["channelName"],
            }
        oplog.log_from_request(
            request,
            oplog.ACTION_CHANNEL_DELETE,
            "channel",
            channel_id,
            user_id=principal.user_id,
            detail=detail,
        )
    return _unwrap(result)


@public_router.get(
    "/{channel_code}", response_model=ApiResult, summary="渠道公开配置（门户/嵌入页）"
)
def public_channel(
    channel_code: str,
    role: str | None = Query(default=None, max_length=32),
) -> ApiResult:
    return _unwrap(channel_admin.public_info(channel_code, role))
