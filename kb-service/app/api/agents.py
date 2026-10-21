"""公开助手矩阵接口（任务 3.8 / P-QA-06）。

门户首屏「切换助手」使用：只返回上架且对当前身份可见的助手，
以及每个助手的场景引导问题。配置类字段（提示词/检索参数/模型）不对外。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.models.schemas import ApiResult
from app.security.ratelimit import rate_limit
from app.services import agent_admin

public_router = APIRouter(prefix="/agents", dependencies=[Depends(rate_limit("channels"))])


@public_router.get("", response_model=ApiResult, summary="公开助手矩阵（按身份过滤）")
def list_public_agents(role: str | None = Query(default=None, max_length=32)) -> ApiResult:
    result = agent_admin.public_agents(role)
    return ApiResult(ok=True, data=result["data"])
