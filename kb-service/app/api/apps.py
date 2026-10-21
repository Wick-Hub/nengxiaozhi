"""智能体广场应用接口（任务 3.3 / M-C-05·06）。

管理端挂在 ``/api/admin/apps``（RBAC：``apps.view/manage``）；
公开端 ``GET /api/apps`` 供用户端广场按角色读取，无需登录。
带有效 token 时以 token 角色为准（与问答接口一致），否则用 ``role`` 查询参数，
未传或非身份角色一律按「访客」处理。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from app.models.schemas import ApiResult, AppCreateRequest, AppUpdateRequest
from app.security import oplog
from app.security.deps import Principal, get_current_user, require_admin, require_permission
from app.security.ratelimit import rate_limit
from app.services import app_admin

admin_router = APIRouter(
    prefix="/admin",
    dependencies=[
        Depends(require_admin),
        Depends(require_permission),
        Depends(rate_limit("admin")),
    ],
)
public_router = APIRouter(prefix="/apps", dependencies=[Depends(rate_limit("apps"))])


def _unwrap(result: dict) -> ApiResult:
    if not result.get("ok"):
        from fastapi import HTTPException

        raise HTTPException(
            status_code=int(result.get("status") or 400),
            detail=result.get("error") or "操作失败",
        )
    return ApiResult(ok=True, data=result["data"])


@admin_router.get("/apps", response_model=ApiResult, summary="应用清单（M-C-05）")
def list_apps() -> ApiResult:
    return _unwrap(app_admin.list_apps())


@admin_router.get("/apps/{app_id}", response_model=ApiResult, summary="应用详情（M-C-05）")
def get_app(app_id: int) -> ApiResult:
    return _unwrap(app_admin.get_app(app_id))


@admin_router.post("/apps", response_model=ApiResult, summary="新建应用（M-C-05）")
def create_app(
    payload: AppCreateRequest,
    request: Request,
    principal: Principal = Depends(require_admin),
) -> ApiResult:
    result = app_admin.create_app(payload.to_payload())
    if result["ok"]:
        item = result["data"]
        oplog.log_from_request(
            request,
            oplog.ACTION_APP_CREATE,
            "app",
            item["appId"],
            user_id=principal.user_id,
            detail={
                "appName": item["appName"],
                "sourceType": item["sourceType"],
                "visibleRoles": item["visibleRoles"],
            },
        )
    return _unwrap(result)


@admin_router.patch("/apps/{app_id}", response_model=ApiResult, summary="更新应用 / 角色授权（M-C-06）")
def update_app(
    app_id: int,
    payload: AppUpdateRequest,
    request: Request,
    principal: Principal = Depends(require_admin),
) -> ApiResult:
    result = app_admin.update_app(app_id, payload.to_payload())
    if result["ok"]:
        item = result["data"]
        oplog.log_from_request(
            request,
            oplog.ACTION_APP_UPDATE,
            "app",
            app_id,
            user_id=principal.user_id,
            detail={
                "appName": item["appName"],
                "status": item["status"],
                "visibleRoles": item["visibleRoles"],
            },
        )
    return _unwrap(result)


@admin_router.delete("/apps/{app_id}", response_model=ApiResult, summary="删除应用（M-C-05）")
def delete_app(
    app_id: int,
    request: Request,
    principal: Principal = Depends(require_admin),
) -> ApiResult:
    result = app_admin.delete_app(app_id)
    if result["ok"]:
        oplog.log_from_request(
            request,
            oplog.ACTION_APP_DELETE,
            "app",
            app_id,
            user_id=principal.user_id,
        )
    return _unwrap(result)


@public_router.get("", response_model=ApiResult, summary="广场应用（按角色可见）")
def public_apps(
    role: str | None = Query(default=None, description="身份角色；登录后以 token 为准"),
    principal: Principal = Depends(get_current_user),
) -> ApiResult:
    effective = principal.role if principal.authenticated else role
    return _unwrap(app_admin.public_apps(effective))
