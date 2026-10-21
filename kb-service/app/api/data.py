"""问数接口（任务 4.3 / P-DQ-01·02·03）：能力状态 + 个人数据查询。

门户对话通过 ``ChatData.dataCards`` 拿到同样的卡片；本路由供独立页面、
嵌入端与联调直接调用。身份只取自登录 token，请求体不能指定学工号。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.models.schemas import ApiResult, DataQueryRequest
from app.security.deps import Principal, get_current_user
from app.security.ratelimit import rate_limit
from app.services import data_query

router = APIRouter(prefix="/data", dependencies=[Depends(rate_limit("data"))])


@router.get("/capabilities", response_model=ApiResult, summary="问数能力状态")
def capabilities() -> ApiResult:
    return ApiResult(ok=True, data=data_query.capabilities())


@router.post("/query", response_model=ApiResult, summary="查询课表 / 成绩 / 一卡通")
def run_query(
    payload: DataQueryRequest,
    principal: Principal = Depends(get_current_user),
) -> ApiResult:
    result = data_query.query(
        payload.question,
        principal,
        role_hint=payload.role,
        intent=payload.intent,
        params=payload.params,
    )
    if not result.get("ok"):
        raise HTTPException(
            status_code=int(result.get("status") or 400),
            detail=result.get("error") or "问数查询失败",
        )
    return ApiResult(ok=True, data=result["data"])
