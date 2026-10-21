"""管理接口：会话日志（任务 2.1）/ 操作日志（任务 2.10）。

响应沿用统一封装 ``{"ok": bool, "data": ..., "error": ...}``，字段一律 camelCase。
鉴权与限流沿用批1 1.13 / 1.12 的管理接口口径：``AUTH_ENABLED=true`` 时需管理员，
默认关闭以便开发与演示。

查询参数用 camelCase 别名（``agentId`` / ``pageSize`` …），与前端契约一致。
"""

from __future__ import annotations

import logging
from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from app.core import llm_router
from app.core.llm_client import LLMError
from app.models.schemas import (
    AdminCreateRequest,
    AdminUpdateRequest,
    AgentCreateRequest,
    AgentRetrievalRequest,
    AgentStatusRequest,
    AgentTestRequest,
    AgentUpdateRequest,
    ApiResult,
    KbCategoryCreateRequest,
    KbCategoryUpdateRequest,
    KbPermissionCreateRequest,
    KbPermissionUpdateRequest,
    RoleCreateRequest,
    RoleUpdateRequest,
    SensitiveWordCreateRequest,
    SensitiveWordUpdateRequest,
)
from app.security import oplog
from app.security.deps import (
    Principal,
    get_current_user,
    require_admin,
    require_permission,
)
from app.security.ratelimit import rate_limit
from app.services import (
    agent_admin,
    agent_config,
    chatlog_admin,
    gateway_admin,
    kb_category,
    kb_permission as kb_permission_service,
    rbac_admin,
    sensitive_admin,
    stats_admin,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin",
    dependencies=[
        Depends(require_admin),
        Depends(require_permission),
        Depends(rate_limit("admin")),
    ],
)


@router.get("/chatlogs/filters", response_model=ApiResult, summary="会话日志筛选项")
def chatlog_filters() -> ApiResult:
    """智能体 / 渠道 / 角色 下拉数据（供筛选条渲染）。"""
    return ApiResult(ok=True, data=chatlog_admin.filter_options())


@router.get("/chatlogs/users", response_model=ApiResult, summary="会话日志·咨询用户视图")
def chatlog_users(
    agent_id: int | None = Query(default=None, alias="agentId"),
    channel_id: int | None = Query(default=None, alias="channelId"),
    role: str | None = Query(default=None, description="咨询用户角色"),
    date_from: str | None = Query(default=None, alias="dateFrom", description="YYYY-MM-DD"),
    date_to: str | None = Query(default=None, alias="dateTo", description="YYYY-MM-DD"),
    q: str | None = Query(default=None, description="按问题/回答检索"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200, alias="pageSize"),
) -> ApiResult:
    """按会话聚合的咨询用户列表（一行 = 一个会话）。"""
    return ApiResult(
        ok=True,
        data=chatlog_admin.query_users(
            agent_id=agent_id,
            channel_id=channel_id,
            role=role,
            date_from=date_from,
            date_to=date_to,
            keyword=q,
            page=page,
            page_size=page_size,
        ),
    )


@router.get("/chatlogs/export", summary="会话日志导出 CSV")
def chatlog_export(
    request: Request,
    view: str = Query(default="qa", pattern="^(qa|users)$", description="qa=问答 / users=咨询用户"),
    agent_id: int | None = Query(default=None, alias="agentId"),
    channel_id: int | None = Query(default=None, alias="channelId"),
    role: str | None = Query(default=None),
    date_from: str | None = Query(default=None, alias="dateFrom"),
    date_to: str | None = Query(default=None, alias="dateTo"),
    q: str | None = Query(default=None),
) -> Response:
    """导出当前筛选条件下的**全集**（与页面所见同一筛选口径）。

    CSV 带 UTF-8 BOM，Excel 直接打开不乱码；文件名用 RFC 5987 编码保证中文不乱码。
    """
    rows, truncated = chatlog_admin.export_rows(
        view,
        agent_id=agent_id,
        channel_id=channel_id,
        role=role,
        date_from=date_from,
        date_to=date_to,
        keyword=q,
    )
    body = chatlog_admin.to_csv(view, rows)

    oplog.log_from_request(
        request,
        oplog.ACTION_LOG_EXPORT,
        "chatlog",
        view,
        detail={
            "view": view,
            "rows": len(rows),
            "truncated": truncated,
            "filters": {
                "agentId": agent_id,
                "channelId": channel_id,
                "role": role,
                "dateFrom": date_from,
                "dateTo": date_to,
                "q": q,
            },
        },
    )

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    label = "咨询用户" if view == "users" else "问答"
    filename = f"会话日志-{label}-{stamp}.csv"
    response = Response(content=body, media_type="text/csv; charset=utf-8")
    response.headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(filename)}"
    response.headers["X-Content-Type-Options"] = "nosniff"
    if truncated:
        # 让调用方知道结果被 EXPORT_LIMIT 截断（不做静默截断）
        response.headers["X-Export-Truncated"] = f"true; limit={chatlog_admin.EXPORT_LIMIT}"
    return response


@router.get("/chatlogs", response_model=ApiResult, summary="会话日志·问答列表")
def list_chatlogs(
    agent_id: int | None = Query(default=None, alias="agentId"),
    channel_id: int | None = Query(default=None, alias="channelId"),
    role: str | None = Query(default=None, description="咨询用户角色"),
    date_from: str | None = Query(default=None, alias="dateFrom", description="YYYY-MM-DD"),
    date_to: str | None = Query(default=None, alias="dateTo", description="YYYY-MM-DD"),
    q: str | None = Query(default=None, description="按问题/回答检索"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200, alias="pageSize"),
) -> ApiResult:
    """问答列表（一行 = 一问一答），支持按机器人/渠道/角色/时间筛选与关键词检索。"""
    return ApiResult(
        ok=True,
        data=chatlog_admin.query_qa(
            agent_id=agent_id,
            channel_id=channel_id,
            role=role,
            date_from=date_from,
            date_to=date_to,
            keyword=q,
            page=page,
            page_size=page_size,
        ),
    )


@router.get("/stats/filters", response_model=ApiResult, summary="统计筛选项")
def stats_filters() -> ApiResult:
    """渠道 / 助手 / 角色下拉数据（与会话日志同源）。"""
    return ApiResult(ok=True, data=chatlog_admin.filter_options())


@router.get("/stats/overview", response_model=ApiResult, summary="统计概览（M-T-01/04/05）")
def stats_overview(
    agent_id: int | None = Query(default=None, alias="agentId"),
    channel_id: int | None = Query(default=None, alias="channelId"),
    role: str | None = Query(default=None, description="咨询用户角色"),
    date_from: str | None = Query(default=None, alias="dateFrom", description="YYYY-MM-DD"),
    date_to: str | None = Query(default=None, alias="dateTo", description="YYYY-MM-DD"),
) -> ApiResult:
    """指标卡 + 按天趋势 + 渠道统计 + 助手统计。"""
    return ApiResult(
        ok=True,
        data=stats_admin.overview(
            agent_id=agent_id,
            channel_id=channel_id,
            role=role,
            date_from=date_from,
            date_to=date_to,
        ),
    )


@router.get("/stats/rankings", response_model=ApiResult, summary="统计排行（M-T-02/03/06/07/08）")
def stats_rankings(
    agent_id: int | None = Query(default=None, alias="agentId"),
    channel_id: int | None = Query(default=None, alias="channelId"),
    role: str | None = Query(default=None),
    date_from: str | None = Query(default=None, alias="dateFrom"),
    date_to: str | None = Query(default=None, alias="dateTo"),
    limit: int = Query(default=10, ge=1, le=50),
) -> ApiResult:
    """热门知识 / 分类 / 设备 / 情绪 / 应用排行；无数据维度返回空态与说明。"""
    return ApiResult(
        ok=True,
        data=stats_admin.rankings(
            agent_id=agent_id,
            channel_id=channel_id,
            role=role,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
        ),
    )


@router.get("/stats/export", summary="统计报表导出 CSV")
def stats_export(
    request: Request,
    kind: str = Query(
        default="summary",
        pattern="^(summary|trend|channels|agents|knowledge|categories|devices|sentiments|apps)$",
    ),
    agent_id: int | None = Query(default=None, alias="agentId"),
    channel_id: int | None = Query(default=None, alias="channelId"),
    role: str | None = Query(default=None),
    date_from: str | None = Query(default=None, alias="dateFrom"),
    date_to: str | None = Query(default=None, alias="dateTo"),
) -> Response:
    """按当前筛选导出统计表（UTF-8 BOM）。"""
    try:
        body, rows = stats_admin.export_csv(
            kind,
            agent_id=agent_id,
            channel_id=channel_id,
            role=role,
            date_from=date_from,
            date_to=date_to,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    oplog.log_from_request(
        request,
        oplog.ACTION_STATS_EXPORT,
        "stats",
        kind,
        detail={
            "kind": kind,
            "rows": rows,
            "filters": {
                "agentId": agent_id,
                "channelId": channel_id,
                "role": role,
                "dateFrom": date_from,
                "dateTo": date_to,
            },
        },
    )

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"统计分析-{kind}-{stamp}.csv"
    response = Response(content=body, media_type="text/csv; charset=utf-8")
    response.headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(filename)}"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@router.get("/operation-logs", response_model=ApiResult, summary="操作日志（M-SYS-08）")
def list_operation_logs(
    user_id: int | None = Query(default=None, alias="userId", description="操作人 user_id"),
    action: str | None = Query(default=None, description="按动作码筛选"),
    date_from: str | None = Query(default=None, alias="dateFrom", description="YYYY-MM-DD"),
    date_to: str | None = Query(default=None, alias="dateTo", description="YYYY-MM-DD"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200, alias="pageSize"),
) -> ApiResult:
    """后台操作留痕：按操作人 / 动作 / 时间筛选并分页（任务 2.10）。"""
    return ApiResult(
        ok=True,
        data=oplog.query_operations(
            user_id=user_id,
            action=action,
            date_from=date_from,
            date_to=date_to,
            page=page,
            page_size=page_size,
        ),
    )


@router.get("/sensitive-words", response_model=ApiResult, summary="敏感词/关键词库（M-SYS-07）")
def list_sensitive_words(
    kind: str | None = Query(default=None, description="sensitive / keyword"),
    status: int | None = Query(default=None, ge=0, le=1),
    q: str | None = Query(default=None, description="按词条检索"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200, alias="pageSize"),
) -> ApiResult:
    """词条清单。JSON 模式返回当前生效的内置/文件词表（只读）。"""
    return _unwrap(
        sensitive_admin.list_words(
            kind=kind, status=status, keyword=q, page=page, page_size=page_size
        )
    )


@router.post("/sensitive-words", response_model=ApiResult, summary="新增词条（M-SYS-07）")
def create_sensitive_word(
    payload: SensitiveWordCreateRequest,
    request: Request,
    principal: Principal = Depends(require_admin),
) -> ApiResult:
    result = sensitive_admin.create_word(payload.to_payload(), user_id=principal.user_id)
    if result["ok"]:
        item = result["data"]
        oplog.log_from_request(
            request,
            oplog.ACTION_SENSITIVE_CREATE,
            "sensitive_word",
            item["wordId"],
            user_id=principal.user_id,
            detail={"word": item["word"], "kind": item["kind"]},
        )
    return _unwrap(result)


@router.patch("/sensitive-words/{word_id}", response_model=ApiResult, summary="更新/启停词条")
def update_sensitive_word(
    word_id: int,
    payload: SensitiveWordUpdateRequest,
    request: Request,
    principal: Principal = Depends(require_admin),
) -> ApiResult:
    result = sensitive_admin.update_word(word_id, payload.to_payload())
    if result["ok"]:
        item = result["data"]
        oplog.log_from_request(
            request,
            oplog.ACTION_SENSITIVE_UPDATE,
            "sensitive_word",
            word_id,
            user_id=principal.user_id,
            detail={
                "word": item["word"],
                "kind": item["kind"],
                "status": item["status"],
            },
        )
    return _unwrap(result)


@router.delete("/sensitive-words/{word_id}", response_model=ApiResult, summary="删除词条")
def delete_sensitive_word(
    word_id: int,
    request: Request,
    principal: Principal = Depends(require_admin),
) -> ApiResult:
    result = sensitive_admin.delete_word(word_id)
    if result["ok"]:
        oplog.log_from_request(
            request,
            oplog.ACTION_SENSITIVE_DELETE,
            "sensitive_word",
            word_id,
            user_id=principal.user_id,
        )
    return _unwrap(result)


# ==================================================================== 任务 2.2 智能体 / 助手管理
def _unwrap(result: dict) -> ApiResult:
    """服务层结果 → 接口响应；失败时按 ``status`` 抛 ``HTTPException``。

    服务层用 ``status`` 区分「参数错 400 / 不存在 404 / 冲突 409」，
    这里统一翻译成 HTTP 状态码，前端 ``api()`` 能拿到可读的 ``detail``。
    """
    if not result.get("ok"):
        raise HTTPException(
            status_code=int(result.get("status") or 400),
            detail=result.get("error") or "操作失败",
        )
    return ApiResult(ok=True, data=result["data"])


# 注意：``/agents/presets`` 必须注册在 ``/agents/{agent_id}`` **之前**，
# 否则 ``presets`` 会被当作 agent_id 去转 int，直接 422。
@router.get("/agents/presets", response_model=ApiResult, summary="场景智能体预设（M-A-01）")
def agent_presets() -> ApiResult:
    """六个场景模板（财务/招生/后勤/国资/信网/迎新）及其搭建状态。"""
    return _unwrap(agent_admin.list_presets())


@router.get("/agents", response_model=ApiResult, summary="助手列表（M-A-01/02/03）")
def list_agents(
    status: int | None = Query(default=None, ge=0, le=1, description="1上架 0下架"),
    q: str | None = Query(default=None, description="按名称/编码/描述检索"),
) -> ApiResult:
    return _unwrap(agent_admin.list_agents(status=status, keyword=q))


@router.get("/agents/{agent_id}", response_model=ApiResult, summary="助手详情")
def get_agent(agent_id: int) -> ApiResult:
    return _unwrap(agent_admin.get_agent(agent_id))


@router.post("/agents", response_model=ApiResult, summary="创建助手（M-A-02）")
def create_agent(payload: AgentCreateRequest, request: Request) -> ApiResult:
    body = payload.to_payload()
    result = agent_admin.create_agent(body)
    if result["ok"]:
        oplog.log_from_request(
            request,
            oplog.ACTION_AGENT_CREATE,
            "agent",
            result["data"]["agentId"],
            detail={
                "agentCode": result["data"]["agentCode"],
                "agentName": result["data"]["agentName"],
                "agentType": result["data"]["agentType"],
            },
        )
    return _unwrap(result)


@router.post(
    "/agents/presets/{scene_key}", response_model=ApiResult, summary="一键搭建场景智能体（M-A-01）"
)
def create_agent_from_preset(scene_key: str, request: Request) -> ApiResult:
    """按预设搭建场景助手；已存在时**复用原记录**（不建重复行）。"""
    result = agent_admin.create_from_preset(scene_key)
    if result["ok"] and not result["data"].get("reused"):
        oplog.log_from_request(
            request,
            oplog.ACTION_AGENT_CREATE,
            "agent",
            result["data"]["agentId"],
            detail={"sceneKey": scene_key, "agentCode": result["data"]["agentCode"]},
        )
    return _unwrap(result)


@router.patch("/agents/{agent_id}", response_model=ApiResult, summary="更新助手基础配置（M-A-03）")
def update_agent(agent_id: int, payload: AgentUpdateRequest, request: Request) -> ApiResult:
    body = payload.to_payload()
    result = agent_admin.update_agent(agent_id, body)
    if result["ok"]:
        # 只记录改了哪些字段名，不落字段值（提示词/话术可能含敏感内容）
        oplog.log_from_request(
            request,
            oplog.ACTION_AGENT_UPDATE,
            "agent",
            agent_id,
            detail={"fields": sorted(body)},
        )
    return _unwrap(result)


@router.post("/agents/{agent_id}/status", response_model=ApiResult, summary="上架/下架（M-A-03）")
def set_agent_status(agent_id: int, payload: AgentStatusRequest, request: Request) -> ApiResult:
    result = agent_admin.set_status(agent_id, payload.status)
    if result["ok"]:
        oplog.log_from_request(
            request,
            oplog.ACTION_AGENT_STATUS,
            "agent",
            agent_id,
            detail={"status": payload.status},
        )
    return _unwrap(result)


@router.delete("/agents/{agent_id}", response_model=ApiResult, summary="删除助手")
def delete_agent(agent_id: int, request: Request) -> ApiResult:
    result = agent_admin.delete_agent(agent_id)
    if result["ok"]:
        oplog.log_from_request(
            request,
            oplog.ACTION_AGENT_DELETE,
            "agent",
            agent_id,
            detail={"agentCode": result["data"]["agentCode"]},
        )
    return _unwrap(result)


# ==================================================================== 任务 2.3 提示词 / 检索参数 / 对话测试
@router.get("/kb-folders", response_model=ApiResult, summary="知识文件夹清单（M-A-05）")
def kb_folders() -> ApiResult:
    """「部门 × 知识类型」组合及其文档数/切片数，供助手「知识范围」勾选。"""
    return _unwrap(agent_config.list_kb_folders())


@router.get("/agents/{agent_id}/retrieval", response_model=ApiResult, summary="助手检索配置（M-A-05）")
def get_agent_retrieval(agent_id: int) -> ApiResult:
    """读取检索配置，并附带「沿用全局时实际生效的数值」，供管理页展示。"""
    return _unwrap(agent_config.get_retrieval(agent_id))


@router.put("/agents/{agent_id}/retrieval", response_model=ApiResult, summary="保存检索配置（M-A-05）")
def update_agent_retrieval(
    agent_id: int, payload: AgentRetrievalRequest, request: Request
) -> ApiResult:
    """增量保存：**未传的键保持原值**（显式传 0 / [] 才是重置为沿用全局 / 不限定）。"""
    body = payload.to_payload()
    result = agent_config.update_retrieval(agent_id, body)
    if result["ok"]:
        oplog.log_from_request(
            request,
            oplog.ACTION_AGENT_RETRIEVAL,
            "agent",
            agent_id,
            detail={"fields": sorted(body), "retrievalConfig": result["data"]["retrievalConfig"]},
        )
    return _unwrap(result)


@router.post("/agents/{agent_id}/test", response_model=ApiResult, summary="助手对话测试（M-A-09）")
def test_agent(agent_id: int, payload: AgentTestRequest) -> ApiResult:
    """用该助手的提示词 + 检索配置跑一次问答。

    **不落库**（不写会话日志、不进未识别问题池），因此不留操作日志——
    否则运营调试时会把操作日志刷满，真正需要审计的配置变更反而被淹没。
    """
    return _unwrap(
        agent_config.test_chat(
            agent_id,
            payload.question,
            role=payload.role,
            top_k=payload.top_k,
        )
    )


# ================================================================== 知识库分类管理（任务 2.5 / M-K-01·02）
# 分类按 （部门 × 知识类型） 组织。MySQL 模式为真实表记录（kb_category），
# JSON 模式为索引只读推导（editable=False，写操作返回 400 + 提示）。
@router.get("/kb-categories", response_model=ApiResult, summary="知识分类清单（M-K-01）")
def kb_categories(
    dept: str | None = Query(default=None, description="按部门名称过滤（可选）"),
    ktype: str | None = Query(default=None, description="按知识类型过滤（可选）"),
) -> ApiResult:
    """分类清单（含文档数）。

    返回 ``items``（扁平，前端按 dept 分组）、``depts``/``ktypes`` 下拉数据、
    ``docTotal`` 与 ``editable``（JSON 模式恒为 false，管理页据此禁用增删改）。
    """
    return _unwrap(kb_category.list_categories(dept=dept, ktype=ktype))


@router.post("/kb-categories", response_model=ApiResult, summary="新增分类（M-K-01）")
def create_kb_category(payload: KbCategoryCreateRequest, request: Request) -> ApiResult:
    """新增分类；同部门同类型已存在时返回 409。"""
    result = kb_category.create_category(payload.model_dump(by_alias=True, exclude_none=True))
    if result["ok"]:
        item = result["data"] or {}
        oplog.log_from_request(
            request,
            oplog.ACTION_CATEGORY_CREATE,
            "kb_category",
            item.get("categoryId"),
            detail={"dept": item.get("dept"), "ktype": item.get("ktype")},
        )
    return _unwrap(result)


@router.patch("/kb-categories/{category_id}", response_model=ApiResult, summary="重命名/排序分类（M-K-02）")
def update_kb_category(
    category_id: int, payload: KbCategoryUpdateRequest, request: Request
) -> ApiResult:
    result = kb_category.update_category(
        category_id, payload.model_dump(by_alias=True, exclude_none=True)
    )
    if result["ok"]:
        oplog.log_from_request(
            request,
            oplog.ACTION_CATEGORY_UPDATE,
            "kb_category",
            category_id,
            detail={"categoryName": result["data"].get("categoryName")},
        )
    return _unwrap(result)


@router.delete("/kb-categories/{category_id}", response_model=ApiResult, summary="删除分类（M-K-02）")
def delete_kb_category(category_id: int, request: Request) -> ApiResult:
    """删除分类；有文档或子分类时返回 409。"""
    result = kb_category.delete_category(category_id)
    if result["ok"]:
        oplog.log_from_request(
            request,
            oplog.ACTION_CATEGORY_DELETE,
            "kb_category",
            category_id,
            detail={},
        )
    return _unwrap(result)


# ================================================================== 知识库部门授权（任务 2.6 / M-K-03）
# 超管为部门联络员按「用户 × 部门 × 知识类型」分配五操作位。
# JSON 模式为只读（editable=False，写操作 400 + 指引）。
@router.get("/kb-permissions", response_model=ApiResult, summary="知识授权清单（M-K-03）")
def kb_permissions(
    user_id: int | None = Query(default=None, alias="userId", description="按被授权人过滤"),
) -> ApiResult:
    """授权清单；返回 ``items`` + ``liaisons``（被授权人下拉）+ ``depts`` + ``actions``。"""
    return _unwrap(kb_permission_service.list_permissions(user_id=user_id))


@router.post("/kb-permissions", response_model=ApiResult, summary="授予知识权限（M-K-03）")
def grant_kb_permission(
    payload: KbPermissionCreateRequest,
    request: Request,
    principal: Principal = Depends(get_current_user),
) -> ApiResult:
    """授予权限；同键已存在返回 409。"""
    body = payload.model_dump(by_alias=True, exclude_none=True)
    result = kb_permission_service.grant(body, principal)
    if result["ok"]:
        item = result["data"] or {}
        oplog.log_from_request(
            request,
            oplog.ACTION_PERM_GRANT,
            "kb_permission",
            item.get("permId"),
            detail={
                "userId": item.get("userId"),
                "deptId": item.get("deptId"),
                "ktype": item.get("ktype"),
            },
        )
    return _unwrap(result)


@router.patch(
    "/kb-permissions/{perm_id}", response_model=ApiResult, summary="更新授权操作位（M-K-03）"
)
def update_kb_permission(
    perm_id: int, payload: KbPermissionUpdateRequest, request: Request
) -> ApiResult:
    """更新某条授权记录的操作位。"""
    body = payload.model_dump(by_alias=True, exclude_none=True)
    result = kb_permission_service.update(perm_id, body)
    if result["ok"]:
        oplog.log_from_request(
            request,
            oplog.ACTION_PERM_UPDATE,
            "kb_permission",
            perm_id,
            detail={k: v for k, v in body.items() if v is not None},
        )
    return _unwrap(result)


@router.delete("/kb-permissions/{perm_id}", response_model=ApiResult, summary="撤销授权（M-K-03）")
def revoke_kb_permission(perm_id: int, request: Request) -> ApiResult:
    result = kb_permission_service.revoke(perm_id)
    if result["ok"]:
        oplog.log_from_request(
            request,
            oplog.ACTION_PERM_REVOKE,
            "kb_permission",
            perm_id,
            detail={},
        )
    return _unwrap(result)


# ================================================================== 大模型管理（任务 2.4）
# ------------------------------------------------------------------ 模型（只读，来源=网关）
# 架构（2026-10 变更）：**模型的唯一来源是网关**。
# 能小智不再有自己的模型台账，因此这里**只剩只读接口**——
# 新增/更新/删除模型、以及连通性自测，全部改为在网关控制台操作。
# 保留这些只读接口是因为「助手绑定模型」的下拉、以及路由预览仍需要它们。
@router.get("/models/choices", response_model=ApiResult, summary="助手可绑定的模型下拉")
def model_choices() -> ApiResult:
    """``default`` + **网关下发的可用模型**（受业务 Key 白名单过滤）。

    清单来自 ``GET {LLM_BASE_URL}/models``，带 30 秒缓存；能小智不落库、不维护第二份。
    """
    return ApiResult(ok=True, data=gateway_admin.choices())


@router.get("/models/route-preview", response_model=ApiResult, summary="模型路由预览")
def model_route_preview(modelCode: str | None = Query(default=None, alias="modelCode")) -> ApiResult:
    """只解析路由、不发起调用，供助手编辑页显示「实际会走哪个模型」。"""
    route = llm_router.route_of(modelCode)
    unknown = gateway_admin.unknown_binding(route.model_code)
    data = route.to_dict()
    # 顺带告诉前端「这个编码网关认不认」，比等到问答报错更早暴露问题
    data["knownToGateway"] = unknown is None
    data["warning"] = unknown
    return ApiResult(ok=True, data=data)


@router.get("/models", response_model=ApiResult, summary="网关下发的模型清单（只读）")
def list_models(refresh: bool = Query(default=False, description="跳过缓存强制刷新")) -> ApiResult:
    """**只读**：网关当前下发给本业务 Key 的模型清单。

    刻意不提供 keyword/status 过滤——清单通常只有个位数，前端本地过滤即可；
    少一层参数就少一处「筛选后看不到某个模型」的困惑。

    ``refresh=true`` 用于管理页的「刷新」按钮（绕过 30 秒缓存）。
    """
    data = gateway_admin.list_remote_models(refresh=refresh)
    return ApiResult(
        ok=True,
        data={
            "items": data["items"],
            "total": len(data["items"]),
            "baseUrl": data.get("baseUrl", ""),
            "error": data.get("error"),
            "stale": data.get("stale", False),
            "defaultModelCode": llm_router.DEFAULT_MODEL_CODE,
            "globalDefault": llm_router.route_of(None).to_dict(),
        },
    )


@router.get("/models/{model_code}", response_model=ApiResult, summary="单个模型的路由详情（只读）")
def get_model(model_code: str) -> ApiResult:
    """该编码解析后的连接配置（不含明文 Key）+ 网关是否认可它。"""
    route = llm_router.route_of(model_code)
    unknown = gateway_admin.unknown_binding(route.model_code)
    data = route.to_dict()
    data["knownToGateway"] = unknown is None
    data["warning"] = unknown
    return ApiResult(ok=True, data=data)


# ------------------------------------------------------------------ 模型网关
@router.get("/gateway/status", response_model=ApiResult, summary="模型网关状态与控制台入口")
def gateway_status() -> ApiResult:
    """网关（campus-ai-gateway）的可达性与控制台入口。

    **本接口不持有网关管理员凭据**：只用无需鉴权的 ``/healthz`` 判断可达性，
    因此拿不到配额/用量类指标（那些要 ``/admin/status``）。这是有意的取舍——
    能小智不需要为"看个状态"而多保存一份高权限凭据。

    探测失败**不算错误**：``ok: true`` + 端点的 ``status`` 字段说明情况，
    否则管理页会因为一个暂时不通的地址而整页报错。属**只读**操作，不记操作日志。
    """
    return ApiResult(ok=True, data=gateway_admin.gateway_overview())


@router.post("/gateway/models/{model_code}/probe", response_model=ApiResult,
             summary="网关模型连通性自测")
def probe_gateway_model(model_code: str) -> ApiResult:
    """用该网关模型跑一次最小调用，验证业务 Key / 白名单 / 模型名是否配套。

    **失败也是结果**（HTTP 200 + ``ok: false``）——运营需要看到网关返回的错误码
    （如 ``model_not_allowed``）与 ``request_id`` 才能定位，抛 500 反而丢信息。
    属**调试**操作，**不留操作日志**（与既有的对话测试同理）。
    """
    route = llm_router.route_of(model_code)
    unknown = gateway_admin.unknown_binding(route.model_code)
    if unknown:
        return ApiResult(
            ok=True,
            data={
                "modelCode": route.model_code,
                "ok": False,
                "error": unknown,
                "code": "not_in_gateway_list",
                "hint": "在网关控制台把该模型加入业务 Key 的 AllowedModels 白名单",
            },
        )

    client = llm_router.get_client_for(route.model_code)
    try:
        resp = client.chat(
            [{"role": "user", "content": "连通性自测：请只回复「OK」。"}],
            max_tokens=512,  # 推理模型会先花 token 思考，给太小会得到空回答
            temperature=0.0,
        )
    except LLMError as exc:
        return ApiResult(
            ok=True,
            data={
                "modelCode": route.model_code,
                "source": route.source,
                "sourceLabel": route.source_label,
                "baseUrl": route.base_url,
                "ok": False,
                "error": str(exc),
                "code": exc.code,
                "status": exc.status_code,
                "requestId": exc.request_id,
                "hint": exc.hint,
            },
        )
    except Exception as exc:  # noqa: BLE001 - 自测的失败必须当结果返回
        return ApiResult(
            ok=True,
            data={
                "modelCode": route.model_code,
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )

    return ApiResult(
        ok=True,
        data={
            "modelCode": route.model_code,
            "source": route.source,
            "sourceLabel": route.source_label,
            "baseUrl": route.base_url,
            "respondedModel": resp.model or "",
            "mocked": client.mock,
            "ok": True,
            "latencyMs": resp.latency_ms,
            "usage": resp.usage,
            "sample": (resp.content or "")[:120],
        },
    )


# ================================================================== 角色权限（任务 2.9 / M-SYS-01·03）
# 角色矩阵 + 管理员账号。JSON 模式为只读（editable=False，写操作 400 + 指引）。
@router.get("/roles", response_model=ApiResult, summary="管理角色与权限矩阵")
def list_roles() -> ApiResult:
    """返回 ``items``（管理角色 + 权限点）、``catalog``（模块 × 权限点目录）。"""
    return _unwrap(rbac_admin.list_roles())


@router.post("/roles", response_model=ApiResult, summary="新增管理角色")
def create_role(
    payload: RoleCreateRequest,
    request: Request,
    principal: Principal = Depends(require_admin),
) -> ApiResult:
    result = rbac_admin.create_role(payload.to_payload())
    if result["ok"]:
        item = result["data"]
        oplog.log_from_request(
            request,
            oplog.ACTION_ROLE_CREATE,
            "sys_role",
            item["roleId"],
            user_id=principal.user_id,
            detail={
                "roleCode": item["roleCode"],
                "roleName": item["roleName"],
                "permissions": item["permissions"],
            },
        )
    return _unwrap(result)


@router.patch("/roles/{role_id}", response_model=ApiResult, summary="更新角色/权限矩阵")
def update_role(
    role_id: int,
    payload: RoleUpdateRequest,
    request: Request,
    principal: Principal = Depends(require_admin),
) -> ApiResult:
    body = payload.to_payload()
    result = rbac_admin.update_role(role_id, body)
    if result["ok"]:
        item = result["data"]
        oplog.log_from_request(
            request,
            oplog.ACTION_ROLE_UPDATE,
            "sys_role",
            role_id,
            user_id=principal.user_id,
            detail={
                "fields": sorted(body),
                "roleName": item["roleName"],
                "permissions": item["permissions"],
            },
        )
    return _unwrap(result)


@router.delete("/roles/{role_id}", response_model=ApiResult, summary="删除管理角色")
def delete_role(
    role_id: int,
    request: Request,
    principal: Principal = Depends(require_admin),
) -> ApiResult:
    result = rbac_admin.delete_role(role_id)
    if result["ok"]:
        oplog.log_from_request(
            request,
            oplog.ACTION_ROLE_DELETE,
            "sys_role",
            role_id,
            user_id=principal.user_id,
        )
    return _unwrap(result)


@router.get("/admins", response_model=ApiResult, summary="管理员账号清单")
def list_admins() -> ApiResult:
    return _unwrap(rbac_admin.list_admins())


@router.post("/admins", response_model=ApiResult, summary="新增管理员账号")
def create_admin(
    payload: AdminCreateRequest,
    request: Request,
    principal: Principal = Depends(require_admin),
) -> ApiResult:
    result = rbac_admin.create_admin(payload.to_payload())
    if result["ok"]:
        item = result["data"]
        oplog.log_from_request(
            request,
            oplog.ACTION_ADMIN_CREATE,
            "sys_user",
            item["userId"],
            user_id=principal.user_id,
            detail={
                "userNo": item["userNo"],
                "realName": item["realName"],
                "roleIds": item["roleIds"],
            },
        )
    return _unwrap(result)


@router.patch("/admins/{user_id}", response_model=ApiResult, summary="更新管理员账号")
def update_admin(
    user_id: int,
    payload: AdminUpdateRequest,
    request: Request,
    principal: Principal = Depends(require_admin),
) -> ApiResult:
    body = payload.to_payload()
    result = rbac_admin.update_admin(user_id, body, actor_user_id=principal.user_id)
    if result["ok"]:
        item = result["data"]
        safe = {k: v for k, v in body.items() if k != "password"}
        # 只记录「改没改密码」，绝不落密码明文
        if body.get("password"):
            safe["passwordChanged"] = True
        oplog.log_from_request(
            request,
            oplog.ACTION_ADMIN_UPDATE,
            "sys_user",
            user_id,
            user_id=principal.user_id,
            detail={"userNo": item["userNo"], "fields": safe},
        )
    return _unwrap(result)


@router.delete("/admins/{user_id}", response_model=ApiResult, summary="移除管理员角色")
def delete_admin(
    user_id: int,
    request: Request,
    principal: Principal = Depends(require_admin),
) -> ApiResult:
    result = rbac_admin.delete_admin(user_id, actor_user_id=principal.user_id)
    if result["ok"]:
        oplog.log_from_request(
            request,
            oplog.ACTION_ADMIN_DELETE,
            "sys_user",
            user_id,
            user_id=principal.user_id,
        )
    return _unwrap(result)
