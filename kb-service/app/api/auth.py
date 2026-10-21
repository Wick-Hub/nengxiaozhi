"""认证接口（任务 1.13 / 4.1）。

    POST /api/auth/login             凭学工号换 JWT（内部账号，可带密码）
    GET  /api/auth/me                当前身份（无 token 时为访客）
    GET  /api/auth/cas/login         CAS 单点登录入口（302 到 CAS / 内置 mock）
    GET  /api/auth/cas/callback      CAS 回调：校验 ticket → 一次性交换码 → 前端
    POST /api/auth/cas/exchange      一次性交换码换正式 JWT
    GET  /api/auth/cas/mock/login    内置 mock CAS（仅 mock 模式）：签 ticket 并回跳

响应沿用统一封装 ``{"ok": bool, "data": ..., "error": ...}``。
"""

from __future__ import annotations

import logging
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.config import settings
from app.db.session import DatabaseUnavailable
from app.models.schemas import ApiResult, CasExchangeRequest, LoginData, LoginRequest
from app.security import permissions
from app.security.auth import create_token
from app.security.deps import Principal, get_current_user
from app.security.ratelimit import rate_limit
from app.services import auth_service, sso

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth")


def _issue_session(ident: dict) -> dict:
    """把身份落成 JWT + LoginData（内部登录与 CAS 交换共用）。"""
    ttl_minutes = settings.jwt_expire_minutes or 720
    token = create_token(
        ident["userNo"],
        ident["role"],
        user_id=ident.get("userId"),
        expires_minutes=ttl_minutes,
        extra={"rid": ident.get("roleId"), "mgr": bool(ident.get("manager"))},
    )
    principal = Principal(
        user_no=ident["userNo"],
        role=ident["role"],
        user_id=ident.get("userId"),
        authenticated=True,
        role_id=ident.get("roleId"),
        manager=bool(ident.get("manager")),
    )
    granted = permissions.for_principal(principal)
    return LoginData(
        token=token,
        userNo=ident["userNo"],
        role=ident["role"],
        expiresIn=ttl_minutes * 60,
        isAdmin=principal.is_admin,
        isManager=principal.manager,
        roleId=ident.get("roleId"),
        roleType=ident.get("roleType"),
        permissions=[c for c in permissions.all_codes() if c in granted],
    ).model_dump()


@router.post(
    "/login",
    response_model=ApiResult,
    summary="登录签发 JWT",
    dependencies=[Depends(rate_limit("auth"))],
)
def login(payload: LoginRequest):
    try:
        ident = auth_service.authenticate(payload.user_no, payload.role, payload.password)
    except auth_service.AuthError as exc:
        return JSONResponse(status_code=401, content={"ok": False, "data": None, "error": str(exc)})
    except DatabaseUnavailable as exc:
        return JSONResponse(
            status_code=503,
            content={"ok": False, "data": None, "error": f"账号库不可用：{exc}"},
        )
    return ApiResult(ok=True, data=_issue_session(ident))


@router.get("/me", response_model=ApiResult, summary="当前身份")
def me(principal: Principal = Depends(get_current_user)) -> ApiResult:
    granted = permissions.for_principal(principal)
    return ApiResult(
        ok=True,
        data={
            "userNo": principal.user_no,
            "role": principal.role,
            "authenticated": principal.authenticated,
            "isAdmin": principal.is_admin,
            "isManager": principal.manager,
            "roleId": principal.role_id,
            "permissions": [c for c in permissions.all_codes() if c in granted],
            "authEnabled": settings.auth_enabled,
            "ssoEnabled": sso.enabled(),
            "ssoProvider": "mock-cas" if sso.mock_mode() else "cas",
        },
    )


# ---------------------------------------------------------------- CAS（任务 4.1）


def _service(request: Request, redirect: str) -> str:
    return sso.build_service_url(str(request.base_url), redirect)


@router.get("/cas/login", summary="CAS 单点登录入口")
def cas_login(request: Request, redirect: str = "/"):
    target = sso.safe_redirect(redirect)
    return RedirectResponse(sso.build_login_url(_service(request, target)), status_code=302)


@router.get("/cas/callback", summary="CAS 回调")
def cas_callback(request: Request, ticket: str = "", redirect: str = "/"):
    target = sso.safe_redirect(redirect)
    service = _service(request, target)
    try:
        attrs = sso.validate_ticket(ticket, service, str(request.base_url))
        ident = auth_service.resolve_sso(
            attrs["userNo"], attrs.get("roleHint") or "", attrs.get("name") or ""
        )
    except (sso.SsoError, auth_service.AuthError) as exc:
        logger.warning("CAS 登录失败：%s", exc)
        return RedirectResponse(f"{target}#cas_error={quote(str(exc))}", status_code=302)
    code = sso.issue_exchange_code(ident)
    return RedirectResponse(f"{target}#cas={quote(code)}", status_code=302)


@router.post(
    "/cas/exchange",
    response_model=ApiResult,
    summary="CAS 交换码换 JWT",
    dependencies=[Depends(rate_limit("auth"))],
)
def cas_exchange(payload: CasExchangeRequest):
    try:
        claims = sso.consume_exchange_code(payload.code)
    except sso.SsoError as exc:
        return JSONResponse(status_code=401, content={"ok": False, "data": None, "error": str(exc)})
    ident = {
        "userNo": str(claims.get("sub") or ""),
        "role": str(claims.get("role") or ""),
        "userId": claims.get("uid"),
        "roleId": claims.get("rid"),
        "manager": bool(claims.get("mgr")),
    }
    return ApiResult(ok=True, data=_issue_session(ident))


def _mock_host_allowed(request: Request, service: str) -> bool:
    """mock CAS 只把 ticket 回跳到本站/显式配置的对外基址，避免成为开放跳板。"""
    try:
        parts = urlsplit(service)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    allowed = {"localhost", "127.0.0.1", "::1", (request.url.hostname or "").lower()}
    for base in (settings.cas_service_base, settings.public_base_url):
        if base:
            allowed.add((urlsplit(base).hostname or "").lower())
    return (parts.hostname or "").lower() in allowed


@router.get("/cas/mock/login", summary="内置 mock CAS 登录（仅 mock 模式）")
def cas_mock_login(
    request: Request,
    service: str = "",
    userNo: str = "20230001",
    role: str = "",
    name: str = "",
):
    if not sso.mock_mode():
        raise HTTPException(status_code=404, detail="未启用内置 mock CAS")
    if not service or not _mock_host_allowed(request, service):
        raise HTTPException(status_code=400, detail="service 地址不合法")
    user_no = (userNo or "").strip()
    demo_name, demo_role = sso.MOCK_USERS.get(user_no, ("", "本科生"))
    role = sso.map_role_hint(role) or demo_role
    ticket = sso.issue_mock_ticket(user_no, role, name or demo_name, service)
    sep = "&" if "?" in service else "?"
    return RedirectResponse(f"{service}{sep}ticket={quote(ticket)}", status_code=302)
