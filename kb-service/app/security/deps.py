"""FastAPI 鉴权依赖（任务 1.13 / 2.9）。

策略：

- **无 token / token 非法 → 访客身份**（``authenticated=False``），不抛错。
  这样公开接口与访客问答照常可用，符合「访客模式」要求。
- **``AUTH_ENABLED=false``（默认）时管理接口不拦截**，便于开发与演示
  （已迁移的前端管理页尚未带 token）；生产必须置 ``AUTH_ENABLED=true``。
- 开启后：未登录访问管理接口 → **401**；已登录但非管理角色 → **403**。
- 任务 2.9：管理角色（``role_type=2``）还要通过 :func:`require_permission`
  的**权限点**校验（``sys_role_permission``）；越权 403 并写操作日志。
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Request

from app.config import settings
from app.security import oplog, permissions, rbac
from app.security.auth import TokenError, decode_token


@dataclass(frozen=True)
class Principal:
    """当前请求身份；未携带有效 token 时即访客。"""

    user_no: str = ""
    role: str = rbac.DEFAULT_ROLE
    user_id: int | None = None
    authenticated: bool = False
    role_id: int | None = None
    manager: bool = False

    @property
    def is_admin(self) -> bool:
        return rbac.is_admin(self.role)


def get_current_user(authorization: str | None = Header(default=None)) -> Principal:
    """解析 ``Authorization: Bearer <jwt>``；缺失或非法一律降级为访客。"""
    if not authorization:
        return Principal()

    scheme, _, raw = authorization.partition(" ")
    if scheme.lower() != "bearer" or not raw.strip():
        return Principal()

    try:
        payload = decode_token(raw.strip())
    except TokenError:
        return Principal()

    role = str(payload.get("role") or rbac.DEFAULT_ROLE)
    role_id = payload.get("rid")
    return Principal(
        user_no=str(payload.get("sub") or ""),
        role=role,
        user_id=payload.get("uid"),
        authenticated=True,
        role_id=int(role_id) if isinstance(role_id, int) else None,
        manager=bool(payload.get("mgr")) or rbac.is_management(role),
    )


def require_admin(principal: Principal = Depends(get_current_user)) -> Principal:
    """管理接口守卫：``AUTH_ENABLED=false`` 时放行，否则要求管理角色。

    细粒度权限点由 :func:`require_permission` 按请求路径二次判定。
    """
    if not settings.auth_enabled:
        return principal
    if not principal.authenticated:
        raise HTTPException(status_code=401, detail="需要登录后访问（管理接口）")
    if not (principal.is_admin or principal.manager):
        raise HTTPException(status_code=403, detail=f"当前角色「{principal.role}」无管理权限")
    return principal


def require_doc_operator(principal: Principal = Depends(get_current_user)) -> Principal:
    """知识库操作守卫（任务 2.6 / 2.9）：管理角色可通过。

    ``AUTH_ENABLED=false`` 时放行（开发/演示）；开启后：
    管理员放行；其他管理角色由 :func:`require_permission` 校验
    ``docs.*`` / ``points.*`` 权限点；具体类目级操作位仍由
    ``kb_permission.require_can`` 在校验层二次判定。
    """
    if not settings.auth_enabled:
        return principal
    if not principal.authenticated:
        raise HTTPException(status_code=401, detail="需要登录后访问（知识库）")
    if principal.is_admin or principal.manager or principal.role == "部门联络员":
        return principal
    raise HTTPException(status_code=403, detail=f"当前角色「{principal.role}」无权操作知识库")


def require_permission(request: Request) -> Principal:
    """按 ``permissions.required_for(method, path)`` 校验权限点。

    这是路由级守卫：管理端 / 知识库 / 同步路由都挂它，避免逐个端点漏挂。
    鉴权关闭时放行；未登录交给上游 401（这里也兜底 401）；管理员恒过；
    其他管理角色从 ``sys_role_permission`` 取实时权限（失败关闭）。
    """
    principal = get_current_user(request.headers.get("authorization"))
    if not settings.auth_enabled:
        return principal
    if not principal.authenticated:
        raise HTTPException(status_code=401, detail="需要登录后访问")
    if principal.is_admin:
        return principal

    code = permissions.required_for(request.method, request.url.path)
    if code is None or permissions.has(principal, code):
        return principal

    label = permissions.LABELS.get(code, code)
    oplog.log_from_request(
        request,
        oplog.ACTION_ACCESS_DENIED,
        "api",
        request.url.path,
        user_id=principal.user_id,
        detail={
            "method": request.method,
            "path": request.url.path,
            "required": code,
            "role": principal.role,
        },
    )
    raise HTTPException(
        status_code=403,
        detail=f"当前角色「{principal.role}」无「{label}」权限（{code}）",
    )
