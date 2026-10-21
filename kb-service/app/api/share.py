"""问答分享与用户反馈接口（任务 4.9 / P-QA-07·08）。

公开端：

* ``POST /api/share``：按会话 + AI 消息生成分享卡片（服务端快照）；
* ``GET /api/share/{id}``：分享页数据（渲染好的安全 HTML + AI 标识 + 赞踩数）；
* ``POST /api/feedback``：点赞/点踩，点踩必须选择原因；
* ``GET /api/feedback/reasons``：点踩原因字典。

管理端（RBAC：``feedback.view/manage``）：反馈清单、CSV 导出、处理留痕。
公开写接口只挂限流，不要求登录——校园门户允许访客提问，也允许访客反馈。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from app.core import ai_label
from app.models.schemas import (
    ApiResult,
    FeedbackHandleRequest,
    FeedbackSubmitRequest,
    ShareCreateRequest,
)
from app.security import oplog
from app.security.deps import Principal, get_current_user, require_admin, require_permission
from app.security.ratelimit import rate_limit
from app.services import share_admin

public_router = APIRouter(prefix="/share", dependencies=[Depends(rate_limit("share"))])
feedback_router = APIRouter(
    prefix="/feedback", dependencies=[Depends(rate_limit("feedback"))]
)
admin_router = APIRouter(
    prefix="/admin/feedback",
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


@public_router.post("", response_model=ApiResult, summary="生成问答分享卡片")
def create_share(payload: ShareCreateRequest, request: Request) -> ApiResult:
    return _unwrap(
        share_admin.create_share(
            payload.session_id,
            payload.msg_id,
            base_url=str(request.base_url),
            agent_name=payload.agent_name or "",
            channel_code=payload.channel_code or "",
        )
    )


@public_router.get("/{share_id}", summary="分享卡片公开数据")
def public_share(share_id: str, response: Response):
    data = share_admin.get_public_share(share_id)
    if data is None:
        raise HTTPException(status_code=404, detail="分享不存在或已失效")
    # 隐式 AI 标识（任务 3.7）：分享页同样声明内容为 AI 生成
    for key, value in ai_label.headers(f"share:{share_id}").items():
        response.headers[key] = value
    return ApiResult(ok=True, data=data)


@feedback_router.get("/reasons", response_model=ApiResult, summary="点踩原因字典")
def feedback_reasons() -> ApiResult:
    from app.db import share_store

    return ApiResult(
        ok=True,
        data=[
            {"code": code, "label": label}
            for code, label in share_store.REASON_CODES.items()
        ],
    )


@feedback_router.post("", response_model=ApiResult, summary="点赞 / 点踩")
def submit_feedback(payload: FeedbackSubmitRequest) -> ApiResult:
    return _unwrap(
        share_admin.submit_feedback(
            rating=payload.rating,
            share_id=payload.share_id or "",
            session_id=payload.session_id or "",
            msg_id=payload.msg_id,
            reason_code=payload.reason_code or "",
            reason=payload.reason or "",
            channel_code=payload.channel_code or "",
        )
    )


@admin_router.get("/filters", response_model=ApiResult, summary="反馈筛选项")
def filters() -> ApiResult:
    return ApiResult(ok=True, data=share_admin.filter_options())


@admin_router.get("", response_model=ApiResult, summary="反馈记录")
def list_feedback(
    rating: int | None = Query(default=None, description="1点赞 -1点踩"),
    handled: int | None = Query(default=None, ge=0, le=1),
    q: str | None = Query(default=None, description="问题 / 原因 / 回答摘要"),
    date_from: str | None = Query(default=None, alias="dateFrom"),
    date_to: str | None = Query(default=None, alias="dateTo"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200, alias="pageSize"),
) -> ApiResult:
    return ApiResult(
        ok=True,
        data=share_admin.list_feedback(
            rating=rating,
            handled=handled,
            keyword=q,
            date_from=date_from,
            date_to=date_to,
            page=page,
            page_size=page_size,
        ),
    )


@admin_router.get("/export", summary="反馈记录导出 CSV")
def export(
    request: Request,
    rating: int | None = Query(default=None),
    handled: int | None = Query(default=None, ge=0, le=1),
    q: str | None = Query(default=None),
    date_from: str | None = Query(default=None, alias="dateFrom"),
    date_to: str | None = Query(default=None, alias="dateTo"),
) -> Response:
    body, rows, truncated = share_admin.export_csv(
        rating=rating, handled=handled, keyword=q, date_from=date_from, date_to=date_to
    )
    oplog.log_from_request(
        request,
        oplog.ACTION_FEEDBACK_EXPORT,
        "qa_feedback",
        "export",
        detail={
            "rows": rows,
            "truncated": truncated,
            "filters": {
                "rating": rating,
                "handled": handled,
                "q": q,
                "dateFrom": date_from,
                "dateTo": date_to,
            },
        },
    )
    response = Response(content=body, media_type="text/csv; charset=utf-8")
    response.headers["Content-Disposition"] = share_admin.quoted_filename(
        share_admin.export_filename()
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    if truncated:
        response.headers["X-Export-Truncated"] = "true"
    return response


@admin_router.get("/{feedback_id}", response_model=ApiResult, summary="反馈详情")
def detail(feedback_id: int) -> ApiResult:
    item = share_admin.get_feedback(feedback_id)
    if item is None:
        raise HTTPException(status_code=404, detail="反馈不存在")
    return ApiResult(ok=True, data=item)


@admin_router.patch("/{feedback_id}", response_model=ApiResult, summary="标记处理 / 重新打开")
def handle(
    feedback_id: int,
    payload: FeedbackHandleRequest,
    request: Request,
    principal: Principal = Depends(get_current_user),
) -> ApiResult:
    result = share_admin.handle_feedback(
        feedback_id, handled=payload.handled, note=payload.note or ""
    )
    if result["ok"]:
        item = result["data"]
        oplog.log_from_request(
            request,
            oplog.ACTION_FEEDBACK_HANDLE,
            "qa_feedback",
            feedback_id,
            user_id=principal.user_id,
            detail={
                "handled": item.get("handled"),
                "note": item.get("handleNote"),
            },
        )
    return _unwrap(result)
