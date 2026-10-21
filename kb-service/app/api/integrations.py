"""集成中心管理接口（任务 4.8 / C-05、M-A-07/10）。

全部端点仅管理员可用，RBAC：``integrations.view``（查看配置与测试结果）、
``integrations.manage``（新增/更新/删除/启用/测试/写入示例）。
所有写操作与连通性测试落操作日志，日志中不含任何密钥。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from app.models.schemas import (
    ApiResult,
    IntegrationAgentAppRequest,
    IntegrationLocalModelRequest,
    IntegrationMcpServerRequest,
)
from app.security import oplog
from app.security.deps import Principal, require_admin, require_permission
from app.security.ratelimit import rate_limit
from app.services import integration_admin as service

router = APIRouter(
    prefix="/admin/integrations",
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
            detail=result.get("error") or "集成配置操作失败",
        )
    return ApiResult(ok=True, data=result["data"])


def _audit(
    request: Request,
    principal: Principal,
    action: str,
    entity: str,
    entity_id,
    detail: dict,
) -> None:
    oplog.log_from_request(
        request,
        action,
        entity,
        str(entity_id or ""),
        user_id=principal.user_id,
        detail=detail,
    )


# ------------------------------------------------------------------ 概览
@router.get("/overview", response_model=ApiResult, summary="集成中心概览")
def overview() -> ApiResult:
    return ApiResult(ok=True, data=service.overview())


# ------------------------------------------------------------------ 本地模型
@router.get("/local-models", response_model=ApiResult, summary="本地模型清单")
def list_local_models() -> ApiResult:
    return ApiResult(ok=True, data={"items": service.list_local_models()})


@router.post("/local-models", response_model=ApiResult, summary="新增本地模型")
def create_local_model(
    payload: IntegrationLocalModelRequest,
    request: Request,
    principal: Principal = Depends(require_admin),
) -> ApiResult:
    result = service.create_local_model(payload.to_payload())
    if result["ok"]:
        item = result["data"]
        _audit(request, principal, oplog.ACTION_INTEGRATION_SAVE,
               "integration_local_model", item["localId"],
               {"action": "create", "code": item["code"], "name": item["name"]})
    return _unwrap(result)


@router.patch("/local-models/{local_id}", response_model=ApiResult, summary="更新本地模型")
def update_local_model(
    local_id: int,
    payload: IntegrationLocalModelRequest,
    request: Request,
    principal: Principal = Depends(require_admin),
) -> ApiResult:
    result = service.update_local_model(local_id, payload.to_payload())
    if result["ok"]:
        item = result["data"]
        _audit(request, principal, oplog.ACTION_INTEGRATION_SAVE,
               "integration_local_model", local_id,
               {"action": "update", "code": item["code"]})
    return _unwrap(result)


@router.delete("/local-models/{local_id}", response_model=ApiResult, summary="删除本地模型")
def delete_local_model(
    local_id: int,
    request: Request,
    principal: Principal = Depends(require_admin),
) -> ApiResult:
    result = service.delete_local_model(local_id)
    if result["ok"]:
        _audit(request, principal, oplog.ACTION_INTEGRATION_DELETE,
               "integration_local_model", local_id, {})
    return _unwrap(result)


@router.post("/local-models/{local_id}/activate", response_model=ApiResult,
             summary="设为当前本地模型")
def activate_local_model(
    local_id: int,
    request: Request,
    principal: Principal = Depends(require_admin),
) -> ApiResult:
    result = service.activate_local_model(local_id)
    if result["ok"]:
        _audit(request, principal, oplog.ACTION_INTEGRATION_ACTIVATE,
               "integration_local_model", local_id, {})
    return _unwrap(result)


@router.post("/local-models/{local_id}/test", response_model=ApiResult,
             summary="测试本地模型对话")
def test_local_model(
    local_id: int,
    request: Request,
    principal: Principal = Depends(require_admin),
) -> ApiResult:
    result = service.test_local_model(local_id)
    if result["ok"]:
        data = result["data"]
        _audit(request, principal, oplog.ACTION_INTEGRATION_TEST,
               "integration_local_model", local_id,
               {"state": data.get("state"), "mock": data.get("mock"),
                "latencyMs": data.get("latencyMs")})
    return _unwrap(result)


# ------------------------------------------------------------------ MCP
@router.get("/mcp-servers", response_model=ApiResult, summary="MCP 服务清单")
def list_mcp_servers() -> ApiResult:
    return ApiResult(ok=True, data={"items": service.list_mcp_servers()})


@router.post("/mcp-servers", response_model=ApiResult, summary="新增 MCP 服务")
def create_mcp_server(
    payload: IntegrationMcpServerRequest,
    request: Request,
    principal: Principal = Depends(require_admin),
) -> ApiResult:
    result = service.create_mcp_server(payload.to_payload())
    if result["ok"]:
        item = result["data"]
        _audit(request, principal, oplog.ACTION_INTEGRATION_SAVE,
               "integration_mcp_server", item["serverId"],
               {"action": "create", "code": item["code"],
                "transport": item["transport"]})
    return _unwrap(result)


@router.patch("/mcp-servers/{server_id}", response_model=ApiResult, summary="更新 MCP 服务")
def update_mcp_server(
    server_id: int,
    payload: IntegrationMcpServerRequest,
    request: Request,
    principal: Principal = Depends(require_admin),
) -> ApiResult:
    result = service.update_mcp_server(server_id, payload.to_payload())
    if result["ok"]:
        item = result["data"]
        _audit(request, principal, oplog.ACTION_INTEGRATION_SAVE,
               "integration_mcp_server", server_id,
               {"action": "update", "code": item["code"]})
    return _unwrap(result)


@router.delete("/mcp-servers/{server_id}", response_model=ApiResult, summary="删除 MCP 服务")
def delete_mcp_server(
    server_id: int,
    request: Request,
    principal: Principal = Depends(require_admin),
) -> ApiResult:
    result = service.delete_mcp_server(server_id)
    if result["ok"]:
        _audit(request, principal, oplog.ACTION_INTEGRATION_DELETE,
               "integration_mcp_server", server_id, {})
    return _unwrap(result)


@router.post("/mcp-servers/{server_id}/test", response_model=ApiResult,
             summary="MCP 连通性与工具清单")
def test_mcp_server(
    server_id: int,
    request: Request,
    principal: Principal = Depends(require_admin),
) -> ApiResult:
    result = service.test_mcp_server(server_id)
    if result["ok"]:
        data = result["data"]
        _audit(request, principal, oplog.ACTION_INTEGRATION_TEST,
               "integration_mcp_server", server_id,
               {"state": data.get("state"), "mock": data.get("mock"),
                "toolCount": data.get("toolCount")})
    return _unwrap(result)


# ------------------------------------------------------------------ 第三方智能体
@router.get("/agent-apps", response_model=ApiResult, summary="第三方智能体清单")
def list_agent_apps() -> ApiResult:
    return ApiResult(ok=True, data={"items": service.list_agent_apps()})


@router.post("/agent-apps", response_model=ApiResult, summary="新增第三方智能体")
def create_agent_app(
    payload: IntegrationAgentAppRequest,
    request: Request,
    principal: Principal = Depends(require_admin),
) -> ApiResult:
    result = service.create_agent_app(payload.to_payload())
    if result["ok"]:
        item = result["data"]
        _audit(request, principal, oplog.ACTION_INTEGRATION_SAVE,
               "integration_agent_app", item["appId"],
               {"action": "create", "code": item["code"], "kind": item["kind"]})
    return _unwrap(result)


@router.patch("/agent-apps/{app_id}", response_model=ApiResult, summary="更新第三方智能体")
def update_agent_app(
    app_id: int,
    payload: IntegrationAgentAppRequest,
    request: Request,
    principal: Principal = Depends(require_admin),
) -> ApiResult:
    result = service.update_agent_app(app_id, payload.to_payload())
    if result["ok"]:
        item = result["data"]
        _audit(request, principal, oplog.ACTION_INTEGRATION_SAVE,
               "integration_agent_app", app_id,
               {"action": "update", "code": item["code"]})
    return _unwrap(result)


@router.delete("/agent-apps/{app_id}", response_model=ApiResult, summary="删除第三方智能体")
def delete_agent_app(
    app_id: int,
    request: Request,
    principal: Principal = Depends(require_admin),
) -> ApiResult:
    result = service.delete_agent_app(app_id)
    if result["ok"]:
        _audit(request, principal, oplog.ACTION_INTEGRATION_DELETE,
               "integration_agent_app", app_id, {})
    return _unwrap(result)


@router.post("/agent-apps/{app_id}/test", response_model=ApiResult,
             summary="第三方智能体连通性测试")
def test_agent_app(
    app_id: int,
    request: Request,
    principal: Principal = Depends(require_admin),
) -> ApiResult:
    result = service.test_agent_app(app_id)
    if result["ok"]:
        data = result["data"]
        _audit(request, principal, oplog.ACTION_INTEGRATION_TEST,
               "integration_agent_app", app_id,
               {"state": data.get("state"), "mock": data.get("mock"),
                "latencyMs": data.get("latencyMs")})
    return _unwrap(result)


# ------------------------------------------------------------------ 示例数据
@router.post("/seed-examples", response_model=ApiResult, summary="写入示例集成配置")
def seed_examples(
    request: Request,
    principal: Principal = Depends(require_admin),
) -> ApiResult:
    result = service.seed_examples()
    _audit(request, principal, oplog.ACTION_INTEGRATION_SEED,
           "integration", "examples", {"created": result["created"]})
    return ApiResult(ok=True, data=result)
