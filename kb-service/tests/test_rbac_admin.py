"""角色权限（任务 2.9 / M-SYS-01·03）测试。

覆盖：
1. 密码哈希（PBKDF2）与常量时间校验；
2. 权限点目录、路径 → 权限点映射、身份 → 权限集合；
3. JSON 模式的只读空态与写操作指引；
4. 真实 MySQL：角色矩阵 CRUD、管理员账号 CRUD、密码登录、
   权限可见性、越权 403 + 操作日志留痕。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, text

from app.config import settings
from app.db import models as M
from app.db.session import db_session, db_transaction, ping
from app.main import app
from app.security import auth, passwords, permissions
from app.security.deps import Principal
from app.services import rbac_admin

client = TestClient(app)


# ------------------------------------------------------------------ 密码
def test_password_hash_roundtrip() -> None:
    stored = passwords.hash_password("Campus@2026")
    assert stored.startswith("pbkdf2_sha256$")
    assert stored != passwords.hash_password("Campus@2026")  # 随机盐
    assert passwords.verify_password("Campus@2026", stored)
    assert not passwords.verify_password("campus@2026", stored)
    assert not passwords.verify_password("Campus@2026", None)
    assert not passwords.verify_password("Campus@2026", "bad-format")


def test_password_hash_rejects_empty() -> None:
    with pytest.raises(ValueError):
        passwords.hash_password("")


# ------------------------------------------------------------------ 权限目录与映射
def test_permission_catalog_covers_all_modules() -> None:
    modules = [g["module"] for g in permissions.GROUPS]
    assert modules == [
        "docs", "categories", "deptperm", "points",
        "chatlogs", "stats", "unrecognized", "feedback", "support", "gov", "access",
        "datacenter", "integrations", "agents", "channels", "apps", "gateway", "system", "rbac",
    ]
    assert len(permissions.all_codes()) == len(set(permissions.all_codes())) == 42
    assert "docs.view" in permissions.ALL_CODES
    assert set(permissions.LIAISON_DEFAULT_PERMS) <= permissions.ALL_CODES
    assert permissions.LABELS["system.words"] == "词库管理"


@pytest.mark.parametrize(
    ("method", "path", "expect"),
    [
        ("GET", "/api/docs", "docs.view"),
        ("GET", "/api/docs/1/text", "docs.view"),
        ("POST", "/api/docs", "docs.upload"),
        ("PATCH", "/api/docs/1", "docs.audit"),
        ("POST", "/api/docs/batch", "docs.audit"),
        ("DELETE", "/api/docs/1", "docs.delete"),
        ("POST", "/api/sync", "docs.upload"),
        ("GET", "/api/admin/chatlogs", "chatlogs.view"),
        ("GET", "/api/admin/chatlogs/export", "chatlogs.export"),
        ("GET", "/api/admin/stats/overview", "stats.view"),
        ("GET", "/api/admin/stats/rankings", "stats.view"),
        ("GET", "/api/admin/stats/export", "stats.export"),
        ("GET", "/api/admin/unrecognized", "unrecognized.view"),
        ("GET", "/api/admin/unrecognized/12", "unrecognized.view"),
        ("GET", "/api/admin/unrecognized/export", "unrecognized.manage"),
        ("POST", "/api/admin/unrecognized/batch", "unrecognized.manage"),
        ("POST", "/api/admin/unrecognized/feedback", "unrecognized.manage"),
        ("GET", "/api/admin/operation-logs", "system.logs"),
        ("DELETE", "/api/admin/sensitive-words/3", "system.words"),
        ("GET", "/api/admin/roles", "rbac.view"),
        ("POST", "/api/admin/admins", "rbac.manage"),
        ("POST", "/api/admin/agents/3/test", "agents.view"),
        ("POST", "/api/admin/agents", "agents.manage"),
        ("GET", "/api/admin/channels", "channels.view"),
        ("PATCH", "/api/admin/channels/1", "channels.manage"),
        ("GET", "/api/admin/feedback", "feedback.view"),
        ("GET", "/api/admin/feedback/export", "feedback.manage"),
        ("PATCH", "/api/admin/feedback/3", "feedback.manage"),
        ("GET", "/api/admin/apps", "apps.view"),
        ("POST", "/api/admin/apps", "apps.manage"),
        ("DELETE", "/api/admin/apps/1", "apps.manage"),
        ("GET", "/api/admin/gov/items", "gov.view"),
        ("POST", "/api/admin/gov/items", "gov.manage"),
        ("POST", "/api/admin/gov/sync", "gov.manage"),
        ("GET", "/api/admin/access/overview", "access.view"),
        ("GET", "/api/admin/access/drilldown", "access.view"),
        ("GET", "/api/admin/access/events", "access.view"),
        ("POST", "/api/admin/access/sync", "access.manage"),
        ("GET", "/api/admin/access/export", "access.export"),
        ("GET", "/api/admin/access/report", "access.export"),
        ("GET", "/api/admin/integrations/overview", "integrations.view"),
        ("POST", "/api/admin/integrations/local-models", "integrations.manage"),
        ("POST", "/api/admin/integrations/local-models/1/test", "integrations.manage"),
        ("PATCH", "/api/admin/integrations/mcp-servers/2", "integrations.manage"),
        ("DELETE", "/api/admin/integrations/agent-apps/3", "integrations.manage"),
        ("GET", "/api/gov/items", None),
        ("GET", "/api/admin/models", "gateway.view"),
        ("POST", "/api/admin/gateway/models/x/probe", "gateway.view"),
        ("GET", "/api/knowledge/points", "points.view"),
        ("POST", "/api/knowledge/points/export", "points.export"),
        ("PATCH", "/api/knowledge/points/D001-C001", "points.edit"),
        ("GET", "/api/admin/unknown", "rbac.manage"),
        ("GET", "/api/meta", None),
    ],
)
def test_required_for(method: str, path: str, expect: str | None) -> None:
    assert permissions.required_for(method, path) == expect


def test_for_principal_auth_disabled_and_admin(monkeypatch) -> None:
    monkeypatch.setattr(settings, "auth_enabled", False)
    assert permissions.for_principal(Principal()) == permissions.ALL_CODES

    monkeypatch.setattr(settings, "auth_enabled", True)
    admin = Principal(user_no="admin", role="管理员", authenticated=True, manager=True)
    assert permissions.for_principal(admin) == permissions.ALL_CODES


def test_for_principal_json_mode_is_empty(monkeypatch) -> None:
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "kb_store", "json")
    liaison = Principal(user_no="D001", role="部门联络员", user_id=10, authenticated=True, manager=True)
    assert permissions.for_principal(liaison) == frozenset()


# ------------------------------------------------------------------ JSON 模式
def test_json_mode_is_read_only(monkeypatch) -> None:
    monkeypatch.setattr(settings, "kb_store", "json")

    roles = rbac_admin.list_roles()
    assert roles["ok"] and roles["data"]["editable"] is False
    assert roles["data"]["items"] == []
    assert roles["data"]["catalog"]

    admins = rbac_admin.list_admins()
    assert admins["ok"] and admins["data"]["editable"] is False
    assert admins["data"]["items"] == []

    blocked = rbac_admin.create_role({"roleCode": "x_role", "roleName": "X"})
    assert blocked["ok"] is False and blocked["status"] == 400
    assert "MySQL" in blocked["error"]


def test_role_validation_messages(monkeypatch) -> None:
    monkeypatch.setattr(settings, "kb_store", "mysql")
    assert "小写字母开头" in rbac_admin.create_role({"roleCode": "Bad Code", "roleName": "X"})["error"]
    assert "不能为空" in rbac_admin.create_role({"roleCode": "ok_code", "roleName": ""})["error"]
    bad = rbac_admin.create_role(
        {"roleCode": "ok_code", "roleName": "X", "permissions": ["no.such.perm"]}
    )
    assert bad["ok"] is False and "未知权限点" in bad["error"]


# ------------------------------------------------------------------ 真实 MySQL
def _live_mysql(monkeypatch) -> bool:
    ok, _ = ping()
    if not ok:
        return False
    try:
        with db_session() as conn:
            tables = {str(r[0]) for r in conn.execute(text("SHOW TABLES")).all()}
            columns = {
                str(r[0])
                for r in conn.execute(text("SHOW COLUMNS FROM sys_user")).all()
            }
    except Exception:  # noqa: BLE001
        return False
    if "sys_role_permission" not in tables:
        return False
    if "password_hash" not in columns:
        return False
    monkeypatch.setattr(settings, "kb_store", "mysql")
    return True


@pytest.fixture
def mysql_rbac(monkeypatch):
    if not _live_mysql(monkeypatch):
        pytest.skip("需要已迁移的 MySQL（tools/migrate_rbac_2_9.py）")
    monkeypatch.setattr(settings, "auth_enabled", True)
    yield


def test_mysql_role_and_admin_lifecycle(mysql_rbac) -> None:
    suffix = uuid.uuid4().hex[:8]
    code = f"rbac_test_{suffix}"
    role_name = f"验收角色{suffix}"
    user_no = f"RBAC{suffix.upper()}"
    password = "Campus@2026"

    role_id = None
    user_id = None
    try:
        created = rbac_admin.create_role(
            {"roleCode": code, "roleName": role_name, "permissions": ["chatlogs.view"]}
        )
        assert created["ok"], created
        role_id = created["data"]["roleId"]
        assert created["data"]["permissions"] == ["chatlogs.view"]

        dup = rbac_admin.create_role({"roleCode": code, "roleName": f"{role_name}2"})
        assert dup["ok"] is False and dup["status"] == 409

        updated = rbac_admin.update_role(role_id, {"permissions": ["docs.view"]})
        assert updated["ok"] and updated["data"]["permissions"] == ["docs.view"]

        role_id_code = next(
            r for r in rbac_admin.list_roles()["data"]["items"] if r["roleId"] == role_id
        )
        assert role_id_code["permCount"] == 1 and role_id_code["builtin"] is False

        admin = rbac_admin.create_admin(
            {
                "userNo": user_no,
                "realName": "验收管理员",
                "password": password,
                "roleIds": [role_id],
            }
        )
        assert admin["ok"], admin
        user_id = admin["data"]["userId"]
        assert admin["data"]["hasPassword"] is True

        # 登录：密码正确 → 拿到管理 token 与实时权限
        login = client.post(
            "/api/auth/login", json={"userNo": user_no, "password": password}
        )
        assert login.status_code == 200, login.text
        data = login.json()["data"]
        assert data["isManager"] is True and data["isAdmin"] is False
        assert data["permissions"] == ["docs.view"]
        headers = {"Authorization": f"Bearer {data['token']}"}

        me = client.get("/api/auth/me", headers=headers).json()["data"]
        assert me["permissions"] == ["docs.view"] and me["roleId"] == role_id

        # 有权限的模块可通过；未授权模块 403
        assert client.get("/api/docs", headers=headers).status_code == 200
        denied = client.get("/api/admin/operation-logs", headers=headers)
        assert denied.status_code == 403, denied.text
        assert "操作日志" in denied.json()["detail"]

        logs = client.get(
            "/api/admin/operation-logs", headers={"Authorization": f"Bearer {auth.create_token('admin', '管理员')}"}
        )
        assert logs.status_code == 200
        items = logs.json()["data"]["items"]
        assert any(
            it["action"] == "ACCESS_DENIED"
            and it["userId"] == user_id
            and it["detail"].get("required") == "system.logs"
            for it in items
        ), "越权请求应写入 ACCESS_DENIED 操作日志"

        # 密码错误 / 缺少密码
        assert client.post(
            "/api/auth/login", json={"userNo": user_no, "password": "wrong-pass"}
        ).status_code == 401
        assert client.post("/api/auth/login", json={"userNo": user_no}).status_code == 401

        # 角色矩阵实时生效：撤掉 docs.view 后同一 token 立即被 403
        rbac_admin.update_role(role_id, {"permissions": ["chatlogs.view"]})
        assert client.get("/api/docs", headers=headers).status_code == 403
        assert client.get("/api/admin/chatlogs", headers=headers).status_code == 200

        # 自我保护：不能移除/停用当前账号
        assert rbac_admin.delete_admin(user_id, actor_user_id=user_id)["ok"] is False
        assert rbac_admin.update_admin(
            user_id, {"status": 0}, actor_user_id=user_id
        )["ok"] is False

        # 内置角色保护
        admin_role = next(r for r in rbac_admin.list_roles()["data"]["items"] if r["roleCode"] == "admin")
        assert rbac_admin.update_role(
            admin_role["roleId"], {"permissions": ["docs.view"]}
        )["ok"] is False
        assert rbac_admin.delete_role(admin_role["roleId"])["ok"] is False

        # 角色仍被账号绑定时不允许删除
        bound = rbac_admin.delete_role(role_id)
        assert bound["ok"] is False and bound["status"] == 409
    finally:
        if user_id is not None or role_id is not None:
            with db_transaction() as conn:
                if user_id is not None:
                    conn.execute(delete(M.SysUserRole).where(M.SysUserRole.user_id == user_id))
                    conn.execute(delete(M.OperationLog).where(M.OperationLog.user_id == user_id))
                    conn.execute(delete(M.SysUser).where(M.SysUser.user_id == user_id))
                if role_id is not None:
                    conn.execute(delete(M.SysRolePermission).where(M.SysRolePermission.role_id == role_id))
                    conn.execute(delete(M.SysUserRole).where(M.SysUserRole.role_id == role_id))
                    conn.execute(delete(M.SysRole).where(M.SysRole.role_id == role_id))


def test_mysql_management_role_requires_registered_account(mysql_rbac) -> None:
    """未登记学工号不能以管理角色登录（堵住演示阶段的提权口子）。"""
    suffix = uuid.uuid4().hex[:8]
    resp = client.post(
        "/api/auth/login",
        json={"userNo": f"NOUSER{suffix}", "role": "管理员"},
    )
    assert resp.status_code == 401
    assert "创建账号" in resp.json()["error"]


def test_mysql_seed_admin_login_still_passwordless(mysql_rbac) -> None:
    """历史演示账号 admin 未设置密码时仍可免密登录（保持既有验收口径）。"""
    with db_session() as conn:
        row = conn.execute(
            text("SELECT password_hash FROM sys_user WHERE user_no = 'admin'")
        ).first()
    if row is None:
        pytest.skip("sys_user 未 seed")
    if row[0]:
        pytest.skip("admin 已设置密码")
    resp = client.post("/api/auth/login", json={"userNo": "admin"})
    assert resp.status_code == 200 and resp.json()["data"]["isAdmin"] is True
