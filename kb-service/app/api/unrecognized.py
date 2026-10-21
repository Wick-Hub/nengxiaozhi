"""未识别问题闭环接口（任务 3.11 / M-S-07～09）。

独立路由文件（而非塞进 :mod:`app.api.admin`）避免该文件继续膨胀；
权限与限流口径与其它管理接口一致：``require_admin`` + ``require_permission``。
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from app.models.schemas import (
    ApiResult,
    UnrecognizedBatchRequest,
    UnrecognizedFeedbackRequest,
)
from app.security import oplog
from app.security.deps import Principal, get_current_user, require_admin, require_permission
from app.security.ratelimit import rate_limit
from app.services import unrecognized_admin

router = APIRouter(
    prefix="/admin/unrecognized",
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
            detail=result.get("error") or "操作失败",
        )
    return ApiResult(ok=True, data=result["data"])


@router.get("/filters", response_model=ApiResult, summary="未识别问题筛选项")
def filters() -> ApiResult:
    return ApiResult(ok=True, data=unrecognized_admin.filter_options())


@router.get("", response_model=ApiResult, summary="未识别问题聚合清单")
def list_questions(
    state: int | None = Query(default=None, ge=0, le=2, description="0待处理 1已反哺 2已忽略"),
    source: str | None = Query(default=None, description="weak_recall / refused / low_confidence"),
    dept: str | None = Query(default=None),
    role: str | None = Query(default=None),
    channel_code: str | None = Query(default=None, alias="channelCode"),
    date_from: str | None = Query(default=None, alias="dateFrom", description="YYYY-MM-DD"),
    date_to: str | None = Query(default=None, alias="dateTo", description="YYYY-MM-DD"),
    q: str | None = Query(default=None, description="按问题原文检索"),
    sort: str = Query(default="recent", pattern="^(recent|frequent)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200, alias="pageSize"),
) -> ApiResult:
    """一行 = 一个归一化问题，附频次、首末时间、角色/渠道/来源分布与语义簇。"""
    return ApiResult(
        ok=True,
        data=unrecognized_admin.list_questions(
            state=state,
            source=source,
            dept=dept,
            role=role,
            channel_code=channel_code,
            date_from=date_from,
            date_to=date_to,
            keyword=q,
            sort=sort,
            page=page,
            page_size=page_size,
        ),
    )


@router.get("/export", summary="未识别问题清单导出 CSV")
def export(
    request: Request,
    state: int | None = Query(default=None, ge=0, le=2),
    source: str | None = Query(default=None),
    dept: str | None = Query(default=None),
    role: str | None = Query(default=None),
    channel_code: str | None = Query(default=None, alias="channelCode"),
    date_from: str | None = Query(default=None, alias="dateFrom"),
    date_to: str | None = Query(default=None, alias="dateTo"),
    q: str | None = Query(default=None),
) -> Response:
    body, rows, truncated = unrecognized_admin.export_csv(
        state=state,
        source=source,
        dept=dept,
        role=role,
        channel_code=channel_code,
        date_from=date_from,
        date_to=date_to,
        keyword=q,
    )
    oplog.log_from_request(
        request,
        oplog.ACTION_UNRECOGNIZED_EXPORT,
        "unrecognized_question",
        "export",
        detail={
            "rows": rows,
            "truncated": truncated,
            "filters": {
                "state": state,
                "source": source,
                "dept": dept,
                "role": role,
                "channelCode": channel_code,
                "dateFrom": date_from,
                "dateTo": date_to,
                "q": q,
            },
        },
    )
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"未识别问题-{stamp}.csv"
    response = Response(content=body, media_type="text/csv; charset=utf-8")
    response.headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(filename)}"
    response.headers["X-Content-Type-Options"] = "nosniff"
    if truncated:
        response.headers["X-Export-Truncated"] = "true"
    return response


@router.post("/batch", response_model=ApiResult, summary="批量忽略 / 重新打开 / 归类")
def batch(
    payload: UnrecognizedBatchRequest,
    request: Request,
    principal: Principal = Depends(get_current_user),
) -> ApiResult:
    result = unrecognized_admin.batch(
        payload.ids,
        payload.question_keys,
        payload.action,
        dept=payload.dept,
        handler_id=principal.user_id,
    )
    if result["ok"]:
        oplog.log_from_request(
            request,
            oplog.ACTION_UNRECOGNIZED_BATCH,
            "unrecognized_question",
            payload.action,
            user_id=principal.user_id,
            detail={
                "action": payload.action,
                "dept": payload.dept,
                "updated": result["data"].get("updated"),
                "ids": result["data"].get("ids", [])[:50],
            },
        )
    return _unwrap(result)


@router.post("/feedback", response_model=ApiResult, summary="一键反哺知识库草稿")
def feedback(
    payload: UnrecognizedFeedbackRequest,
    request: Request,
    principal: Principal = Depends(get_current_user),
) -> ApiResult:
    """生成知识文档草稿；发布仍走 2.7 审核，避免「未识别问题直接变成已发布知识」。"""
    result = unrecognized_admin.feedback(
        ids=payload.ids,
        question_key=payload.question_key,
        title=payload.title,
        content=payload.content,
        dept=payload.dept,
        ktype=payload.ktype or "faq",
        visible_roles=payload.visible_roles,
        handler_id=principal.user_id,
    )
    if result["ok"]:
        data = result["data"]
        oplog.log_from_request(
            request,
            oplog.ACTION_UNRECOGNIZED_FEEDBACK,
            "kb_doc",
            data.get("docId"),
            user_id=principal.user_id,
            detail={
                "docId": data.get("docId"),
                "title": data.get("title"),
                "updated": data.get("updated"),
                "ids": data.get("ids", [])[:50],
            },
        )
    return _unwrap(result)


@router.get("/{record_id}", response_model=ApiResult, summary="未识别问题详情（追溯会话）")
def detail(record_id: int) -> ApiResult:
    return _unwrap(unrecognized_admin.detail(record_id))
