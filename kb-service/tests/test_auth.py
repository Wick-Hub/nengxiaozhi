"""认证与权限测试（任务 1.13）。

覆盖 JWT 签发/校验（含篡改与过期）、RBAC 映射、FastAPI 依赖
（访客降级 / 401 / 403）、``/api/auth`` 接口、问答角色注入。

需要真实数据库的登录用例在 ``tests/test_mysql_store.py``。
"""

from __future__ import annotations

import base64
import json
import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.security import auth, rbac
from app.security.deps import Principal, get_current_user, require_admin

client = TestClient(app)


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


# ------------------------------------------------------------------ JWT
def test_token_roundtrip() -> None:
    token = auth.create_token("20230001", "本科生", user_id=7)
    payload = auth.decode_token(token)
    assert payload["sub"] == "20230001"
    assert payload["role"] == "本科生"
    assert payload["uid"] == 7
    assert payload["exp"] > payload["iat"]


def test_token_signature_tamper_rejected() -> None:
    head, body, sig = auth.create_token("admin", "管理员").split(".")
    with pytest.raises(auth.TokenError, match="签名"):
        auth.decode_token(f"{head}.{body}.{'A' * len(sig)}")


def test_token_payload_forgery_rejected() -> None:
    """篡改 payload 提权（本科生 → 管理员）必须被签名拦下。"""
    head, _, sig = auth.create_token("20230001", "本科生").split(".")
    forged = _b64e(
        json.dumps(
            {"sub": "20230001", "role": "管理员", "exp": int(time.time()) + 600},
            separators=(",", ":"),
        ).encode()
    )
    with pytest.raises(auth.TokenError, match="签名"):
        auth.decode_token(f"{head}.{forged}.{sig}")


def test_token_expired_rejected() -> None:
    token = auth.create_token("20230001", "本科生", expires_minutes=-1)
    with pytest.raises(auth.TokenError, match="过期"):
        auth.decode_token(token)


@pytest.mark.parametrize("bad", ["", "abc", "a.b", "a.b.c.d", ".."])
def test_token_malformed_rejected(bad: str) -> None:
    with pytest.raises(auth.TokenError):
        auth.decode_token(bad)


def test_token_missing_exp_rejected() -> None:
    header = _b64e(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64e(json.dumps({"sub": "x", "role": "访客"}).encode())
    signing_input = f"{header}.{body}"
    with pytest.raises(auth.TokenError, match="exp"):
        auth.decode_token(f"{signing_input}.{auth._sign(signing_input)}")


# ------------------------------------------------------------------ RBAC
def test_is_admin() -> None:
    assert rbac.is_admin("管理员")
    assert not rbac.is_admin("本科生")
    assert not rbac.is_admin(None)  # 缺省 → 访客
    assert rbac.can_access_admin("管理员")
    assert not rbac.can_access_admin("部门联络员")


@pytest.mark.parametrize(
    ("id_type", "expect"),
    [(1, "本科生"), (2, "研究生"), (3, "教职工"), (4, "离退休人员"), (5, "访客"), (99, "访客"), (None, "访客")],
)
def test_role_for_id_type(id_type: int | None, expect: str) -> None:
    assert rbac.role_for_id_type(id_type) == expect


def test_roles_align_with_meta_auth_roles() -> None:
    """RBAC 角色集合必须与 /api/meta 的 authRoles 完全一致。"""
    from app.api.meta import AUTH_ROLES

    assert set(rbac.ROLES) == set(AUTH_ROLES)


# ------------------------------------------------------------------ 依赖
def test_guest_without_token() -> None:
    p = get_current_user(None)
    assert p.role == "访客"
    assert p.authenticated is False


@pytest.mark.parametrize("header", ["Bearer not-a-jwt", "Token abc", "Bearer ", "abc"])
def test_invalid_header_falls_back_to_guest(header: str) -> None:
    p = get_current_user(header)
    assert p.authenticated is False
    assert p.role == "访客"


def test_valid_token_restores_identity() -> None:
    token = auth.create_token("T10001", "教职工")
    p = get_current_user(f"Bearer {token}")
    assert p.authenticated is True
    assert p.role == "教职工"
    assert p.user_no == "T10001"


def test_require_admin_passthrough_when_disabled(monkeypatch) -> None:
    """AUTH_ENABLED=false（默认）时管理接口不拦截，便于开发/演示。"""
    monkeypatch.setattr(settings, "auth_enabled", False)
    assert require_admin(Principal()).role == "访客"


def test_require_admin_401_without_token(monkeypatch) -> None:
    monkeypatch.setattr(settings, "auth_enabled", True)
    with pytest.raises(HTTPException) as exc:
        require_admin(Principal())
    assert exc.value.status_code == 401


def test_require_admin_403_for_non_admin(monkeypatch) -> None:
    monkeypatch.setattr(settings, "auth_enabled", True)
    with pytest.raises(HTTPException) as exc:
        require_admin(Principal(role="本科生", authenticated=True))
    assert exc.value.status_code == 403


def test_require_admin_ok(monkeypatch) -> None:
    monkeypatch.setattr(settings, "auth_enabled", True)
    p = require_admin(Principal(role="管理员", authenticated=True))
    assert p.is_admin is True


# ------------------------------------------------------------------ /api/auth
def test_me_endpoint_guest() -> None:
    resp = client.get("/api/auth/me")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["role"] == "访客"
    assert data["authenticated"] is False
    assert data["isAdmin"] is False


def test_me_endpoint_with_token() -> None:
    token = auth.create_token("admin", "管理员")
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    data = resp.json()["data"]
    assert data["role"] == "管理员"
    assert data["authenticated"] is True
    assert data["isAdmin"] is True


def test_me_endpoint_ignores_bad_token() -> None:
    resp = client.get("/api/auth/me", headers={"Authorization": "Bearer garbage"})
    assert resp.json()["data"]["authenticated"] is False


# ------------------------------------------------------------------ 管理接口守卫
def test_admin_endpoints_open_when_auth_disabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "auth_enabled", False)
    assert client.get("/api/docs").status_code == 200


def test_admin_endpoints_401_when_auth_enabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "auth_enabled", True)
    assert client.get("/api/docs").status_code == 401
    assert client.post("/api/sync").status_code == 401


def test_admin_endpoints_403_for_non_admin(monkeypatch) -> None:
    monkeypatch.setattr(settings, "auth_enabled", True)
    token = auth.create_token("20230001", "本科生")
    resp = client.get("/api/docs", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403
    assert "本科生" in resp.json()["detail"]


# ------------------------------------------------------------------ 角色注入
def test_effective_role_prefers_token() -> None:
    """带 token 时以 token 角色为准，防止前端伪造身份。"""
    from app.api.chat import _effective_role

    assert _effective_role("本科生", Principal(role="管理员", authenticated=True)) == "管理员"
    assert _effective_role("本科生", Principal()) == "本科生"
    assert _effective_role(None, Principal()) is None
