"""导办事项与免登录票据接口（任务 4.5 / P-DB-01·02、M-G-01、I-03）。

管理端挂在 ``/api/admin/gov``（RBAC：``gov.view/manage``）；
公开端 ``/api/gov`` 提供事项检索与票据兑换，无需登录。签发票据需要登录
（鉴权关闭的演示环境允许按请求身份签发），兑换票据只在服务端一次性消费。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.config import settings
from app.models.schemas import (
    ApiResult,
    GovItemCreateRequest,
    GovItemUpdateRequest,
    GovTicketRequest,
    GovTicketVerifyRequest,
)
from app.security import oplog
from app.security.deps import Principal, get_current_user, require_admin, require_permission
from app.security.ratelimit import rate_limit
from app.services import gov_admin

admin_router = APIRouter(
    prefix="/admin",
    dependencies=[
        Depends(require_admin),
        Depends(require_permission),
        Depends(rate_limit("admin")),
    ],
)
public_router = APIRouter(prefix="/gov", dependencies=[Depends(rate_limit("gov"))])


def _unwrap(result: dict) -> ApiResult:
    if not result.get("ok"):
        raise HTTPException(
            status_code=int(result.get("status") or 400),
            detail=result.get("error") or "操作失败",
        )
    return ApiResult(ok=True, data=result["data"])


# ------------------------------------------------------------------ 管理端
@admin_router.get("/gov/items", response_model=ApiResult, summary="导办事项清单（M-G-01）")
def admin_list(
    q: str | None = Query(default=None, max_length=64),
    status: str | None = Query(default=None, max_length=16),
) -> ApiResult:
    return _unwrap(gov_admin.list_admin(q, status))


@admin_router.get("/gov/meta", response_model=ApiResult, summary="导办字典与同步配置（M-G-01）")
def admin_meta() -> ApiResult:
    return ApiResult(
        ok=True,
        data={
            "deptOptions": gov_admin.DEPTS,
            "roleOptions": gov_admin.ROLE_OPTIONS,
            "sync": gov_admin.sync_meta(),
            "ticketTtlSeconds": settings.gov_ticket_ttl_seconds,
        },
    )


@admin_router.get("/gov/items/{item_id}", response_model=ApiResult, summary="导办事项详情（M-G-01）")
def admin_get(item_id: int) -> ApiResult:
    return _unwrap(gov_admin.get_admin_item(item_id))


@admin_router.post("/gov/items", response_model=ApiResult, summary="新建导办事项（M-G-01）")
def admin_create(
    payload: GovItemCreateRequest,
    request: Request,
    principal: Principal = Depends(require_admin),
) -> ApiResult:
    result = gov_admin.create_item(payload.to_payload())
    if result["ok"]:
        item = result["data"]
        oplog.log_from_request(
            request,
            oplog.ACTION_GOV_CREATE,
            "gov_item",
            item["itemId"],
            user_id=principal.user_id,
            detail={
                "itemName": item["itemName"],
                "deptName": item["deptName"],
                "needTicket": item["needTicket"],
                "visibleRoles": item["visibleRoles"],
            },
        )
    return _unwrap(result)


@admin_router.patch("/gov/items/{item_id}", response_model=ApiResult, summary="更新 / 启停 / 生效时段（M-G-01）")
def admin_update(
    item_id: int,
    payload: GovItemUpdateRequest,
    request: Request,
    principal: Principal = Depends(require_admin),
) -> ApiResult:
    result = gov_admin.update_item(item_id, payload.to_payload())
    if result["ok"]:
        item = result["data"]
        oplog.log_from_request(
            request,
            oplog.ACTION_GOV_UPDATE,
            "gov_item",
            item_id,
            user_id=principal.user_id,
            detail={
                "itemName": item["itemName"],
                "status": item["status"],
                "effectiveFrom": item.get("effectiveFrom"),
                "effectiveTo": item.get("effectiveTo"),
            },
        )
    return _unwrap(result)


@admin_router.delete("/gov/items/{item_id}", response_model=ApiResult, summary="删除导办事项（M-G-01）")
def admin_delete(
    item_id: int,
    request: Request,
    principal: Principal = Depends(require_admin),
) -> ApiResult:
    result = gov_admin.delete_item(item_id)
    if result["ok"]:
        oplog.log_from_request(
            request,
            oplog.ACTION_GOV_DELETE,
            "gov_item",
            item_id,
            user_id=principal.user_id,
        )
    return _unwrap(result)


@admin_router.post("/gov/sync", response_model=ApiResult, summary="同步一网通办事项（I-03）")
def admin_sync(request: Request, principal: Principal = Depends(require_admin)) -> ApiResult:
    result = gov_admin.sync_items()
    if result["ok"]:
        data = result["data"]
        oplog.log_from_request(
            request,
            oplog.ACTION_GOV_SYNC,
            "gov_item",
            data["provider"],
            user_id=principal.user_id,
            detail={
                "provider": data["provider"],
                "created": data["created"],
                "updated": data["updated"],
                "skipped": data["skipped"],
            },
        )
    return _unwrap(result)


# ------------------------------------------------------------------ 公开端
@public_router.get("/items", response_model=ApiResult, summary="导办事项检索（P-DB-01）")
def public_items(
    q: str | None = Query(default=None, max_length=200),
    role: str | None = Query(default=None, description="身份角色；登录后以 token 为准"),
    limit: int = Query(default=20, ge=1, le=50),
    principal: Principal = Depends(get_current_user),
) -> ApiResult:
    effective = principal.role if principal.authenticated else role
    return _unwrap(gov_admin.public_items(effective, q, limit))


@public_router.post("/items/{item_id}/ticket", response_model=ApiResult, summary="签发免登录票据（P-DB-02）")
def issue_ticket(
    item_id: int,
    payload: GovTicketRequest,
    principal: Principal = Depends(get_current_user),
) -> ApiResult:
    if payload.item_id != item_id:
        raise HTTPException(status_code=400, detail="事项 ID 不一致")
    return _unwrap(gov_admin.issue_ticket(item_id, principal, payload.role))


@public_router.post("/tickets/verify", response_model=ApiResult, summary="兑换免登录票据（P-DB-02）")
def verify_ticket(payload: GovTicketVerifyRequest) -> ApiResult:
    return _unwrap(gov_admin.verify_ticket(payload.ticket))
