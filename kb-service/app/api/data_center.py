"""数据中台管理接口（任务 4.2 / I-02、M-SYS-02）。

全部端点仅管理员可用，RBAC：``datacenter.view``（状态/任务/批次/镜像）、
``datacenter.manage``（手动上报、组织同步、补跑定时任务）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.db import data_center_store as store
from app.models.schemas import (
    ApiResult,
    DataCenterReportRequest,
    DataCenterSyncRequest,
)
from app.security import oplog
from app.security.deps import Principal, require_admin, require_permission
from app.security.ratelimit import rate_limit
from app.services import data_center

router = APIRouter(
    prefix="/admin/data-center",
    dependencies=[
        Depends(require_admin),
        Depends(require_permission),
        Depends(rate_limit("admin")),
    ],
)


def _unwrap(result: dict) -> ApiResult:
    if not result.get("ok"):
        raise HTTPException(
            status_code=int(result.get("status") or 400),
            detail=result.get("error") or "数据中台操作失败",
        )
    return ApiResult(ok=True, data=result["data"])


@router.get("/status", response_model=ApiResult, summary="数据中台状态与最近任务")
def dc_status() -> ApiResult:
    return ApiResult(
        ok=True,
        data={**data_center.status(), "sync": data_center.sync_meta()},
    )


@router.get("/jobs", response_model=ApiResult, summary="上报/同步任务记录")
def dc_jobs(limit: int = Query(default=30, ge=1, le=200)) -> ApiResult:
    return ApiResult(ok=True, data={"items": store.list_jobs(limit)})


@router.get("/reports", response_model=ApiResult, summary="上报批次")
def dc_reports(limit: int = Query(default=20, ge=1, le=100)) -> ApiResult:
    return ApiResult(ok=True, data={"items": store.list_reports(limit)})


@router.get("/reports/{batch_id}", response_model=ApiResult, summary="上报批次详情")
def dc_report_detail(batch_id: str) -> ApiResult:
    item = store.get_report(batch_id)
    if item is None:
        raise HTTPException(status_code=404, detail="上报批次不存在")
    return ApiResult(ok=True, data=item)


@router.post("/preview", response_model=ApiResult, summary="预览上报内容（不投递）")
def dc_preview(request: DataCenterReportRequest) -> ApiResult:
    return _unwrap(
        data_center.preview_report(request.date, request.kind)
    )


@router.post("/report", response_model=ApiResult, summary="执行聊天日志/统计上报")
def dc_report(
    request: DataCenterReportRequest,
    http_request: Request,
    principal: Principal = Depends(require_admin),
) -> ApiResult:
    result = data_center.run_report(request.date, request.kind, force=request.force)
    if result["ok"]:
        data = result["data"]
        oplog.log_from_request(
            http_request,
            oplog.ACTION_DATA_CENTER_REPORT,
            "data_center_report",
            str(data.get("jobId") or ""),
            user_id=principal.user_id,
            detail={
                "date": data.get("date"),
                "kind": data.get("kind"),
                "batches": len(data.get("batches") or []),
                "force": request.force,
            },
        )
    return _unwrap(result)


@router.post("/sync", response_model=ApiResult, summary="执行组织/用户同步")
def dc_sync(
    request: DataCenterSyncRequest,
    http_request: Request,
    principal: Principal = Depends(require_admin),
) -> ApiResult:
    result = data_center.run_sync(request.mode)
    if result["ok"]:
        data = result["data"]
        oplog.log_from_request(
            http_request,
            oplog.ACTION_DATA_CENTER_SYNC,
            "data_center_sync",
            str(data.get("jobId") or ""),
            user_id=principal.user_id,
            detail={
                "mode": data.get("mode"),
                "orgs": data.get("orgs"),
                "users": data.get("users"),
            },
        )
    return _unwrap(result)


@router.post("/run-due", response_model=ApiResult, summary="补跑到期的定时任务")
def dc_run_due() -> ApiResult:
    result = data_center.run_due()
    if not result.get("ok"):
        raise HTTPException(status_code=503, detail=result.get("error") or "数据中台未启用")
    return ApiResult(ok=True, data=result)


@router.get("/orgs", response_model=ApiResult, summary="本地组织镜像")
def dc_orgs(limit: int = Query(default=200, ge=1, le=1000)) -> ApiResult:
    return ApiResult(ok=True, data={"items": store.list_orgs(limit)})


@router.get("/users", response_model=ApiResult, summary="本地用户镜像")
def dc_users(limit: int = Query(default=200, ge=1, le=2000)) -> ApiResult:
    return ApiResult(ok=True, data={"items": store.list_users(limit)})
