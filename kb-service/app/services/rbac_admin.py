"""角色权限与管理账号服务（任务 2.9 / M-SYS-01·03）。

- ``KB_STORE=mysql``：``sys_role_permission`` 承载角色矩阵，``sys_user`` 承载
  管理账号（新账号必须设密码；历史演示账号 ``password_hash IS NULL`` 仍可免密登录）。
- ``KB_STORE=json``：没有角色/账号表，目录照常返回（前端可展示），
  清单为空、``editable=False``，写操作 400 + 指引。

保护规则：
- 内置角色 ``admin``（管理员）恒有全部权限，矩阵不可改；
- 内置角色不可重命名/删除（``dept_liaison`` 的权限点可调整）；
- 至少保留一个启用状态的管理员，且不能停用/降级当前登录账号。
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.db import models as M
from app.db import rbac_repo
from app.db.session import DatabaseUnavailable, db_session, db_transaction
from app.security import passwords, permissions

logger = logging.getLogger(__name__)

BUILTIN_CODES = frozenset({"admin", "dept_liaison"})
ADMIN_CODE = "admin"
BUILTIN_ROLE_NAMES = {"admin": "管理员", "dept_liaison": "部门联络员"}
MIN_PASSWORD_LEN = 8
MAX_PASSWORD_LEN = 128

_ROLE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
_USER_NO_RE = re.compile(r"^[A-Za-z0-9_.-]{2,64}$")
_PERM_ORDER = {
    code: idx for idx, code in enumerate(c for g in permissions.GROUPS for c, _ in g["perms"])
}


def _ok(data) -> dict:
    return {"ok": True, "data": data, "error": None}


def _fail(message: str, status: int = 400) -> dict:
    return {"ok": False, "data": None, "error": message, "status": status}


def _mysql_mode() -> bool:
    return settings.store_label == "mysql"


def _must_mysql() -> dict | None:
    if _mysql_mode():
        return None
    return _fail(
        "角色权限维护需要 MySQL 存储：请在 .env 中设置 KB_STORE=mysql 并确保数据库已启动"
        "（docker compose -f ../database/docker-compose.yml up -d）",
        status=400,
    )


def _sorted_perms(codes) -> list[str]:
    return sorted(set(codes), key=lambda c: _PERM_ORDER.get(c, 999))


def _clean_permissions(raw) -> tuple[list[str] | None, str | None]:
    if raw is None:
        return [], None
    if not isinstance(raw, (list, tuple)):
        return None, "权限点必须是数组"
    unknown = [str(c) for c in raw if str(c) not in permissions.ALL_CODES]
    if unknown:
        return None, f"未知权限点：{'、'.join(unknown)}"
    return _sorted_perms(str(c) for c in raw), None


def _clean_role_code(raw: str | None) -> tuple[str | None, str | None]:
    code = (raw or "").strip()
    if not code:
        return None, "角色编码不能为空"
    if not _ROLE_CODE_RE.match(code):
        return None, "角色编码需以小写字母开头，仅含小写字母/数字/下划线，长度 2–32"
    return code, None


def _clean_role_name(raw: str | None) -> tuple[str | None, str | None]:
    name = (raw or "").strip()
    if not name:
        return None, "角色名称不能为空"
    if len(name) > 32:
        return None, "角色名称不能超过 32 个字符"
    if any(ch in name for ch in "\r\n\t"):
        return None, "角色名称不能包含换行或制表符"
    return name, None


def _clean_remark(raw: str | None) -> str:
    return (raw or "").strip()[:255]


def _clean_password(raw: str | None) -> tuple[str | None, str | None]:
    password = raw or ""
    if len(password) < MIN_PASSWORD_LEN:
        return None, f"密码至少 {MIN_PASSWORD_LEN} 位"
    if len(password) > MAX_PASSWORD_LEN:
        return None, f"密码不能超过 {MAX_PASSWORD_LEN} 位"
    return password, None


def _clean_user_no(raw: str | None) -> tuple[str | None, str | None]:
    user_no = (raw or "").strip()
    if not user_no:
        return None, "学工号不能为空"
    if not _USER_NO_RE.match(user_no):
        return None, "学工号仅支持字母/数字/下划线/点/短横线，长度 2–64"
    return user_no, None


def _clean_real_name(raw: str | None) -> tuple[str | None, str | None]:
    name = (raw or "").strip()
    if not name:
        return None, "姓名不能为空"
    if len(name) > 64:
        return None, "姓名不能超过 64 个字符"
    return name, None


def _role_item(conn, role_id: int) -> dict | None:
    row = rbac_repo.get_role(conn, role_id)
    if row is None or row["roleType"] != 2:
        return None
    perms = _sorted_perms(rbac_repo.permissions_for_role(conn, role_id))
    builtin = row["roleCode"] in BUILTIN_CODES
    locked = row["roleCode"] == ADMIN_CODE
    if locked:
        perms = _sorted_perms(permissions.ALL_CODES)
    return {
        "roleId": row["roleId"],
        "roleCode": row["roleCode"],
        "roleName": row["roleName"],
        "remark": row["remark"],
        "permissions": perms,
        "permCount": len(perms),
        "builtin": builtin,
        "locked": locked,
    }


# ------------------------------------------------------------------ 角色
def list_roles() -> dict:
    data = {"items": [], "catalog": permissions.catalog(), "editable": _mysql_mode()}
    if not _mysql_mode():
        data["note"] = "JSON 模式无角色矩阵表：切换到 MySQL 后可配置角色与权限点。"
        return _ok(data)

    try:
        with db_session() as conn:
            data["items"] = [
                _role_item(conn, item["roleId"])
                for item in rbac_repo.list_management_roles(conn)
            ]
    except DatabaseUnavailable as exc:
        return _fail(f"数据库不可用：{exc}", status=503)
    except Exception as exc:  # noqa: BLE001 - 旧库缺表时给可读提示
        logger.exception("角色清单查询失败")
        return _fail(f"角色清单查询失败：{exc}", status=503)
    return _ok(data)


def create_role(payload: dict) -> dict:
    if (blocked := _must_mysql()) is not None:
        return blocked

    code, err = _clean_role_code(payload.get("roleCode"))
    if err:
        return _fail(err)
    name, err = _clean_role_name(payload.get("roleName"))
    if err:
        return _fail(err)
    perms, err = _clean_permissions(payload.get("permissions"))
    if err:
        return _fail(err)
    remark = _clean_remark(payload.get("remark"))

    try:
        with db_transaction() as conn:
            if rbac_repo.find_role(conn, code=code) is not None:
                return _fail(f"角色编码已存在：{code}", status=409)
            if rbac_repo.find_role(conn, name=name) is not None:
                return _fail(f"角色名称已存在：{name}", status=409)
            role_id = rbac_repo.create_role(conn, code=code, name=name, remark=remark)
            rbac_repo.set_role_permissions(conn, role_id, perms)
            item = _role_item(conn, role_id)
    except IntegrityError:
        return _fail(f"角色编码或名称已存在：{code}", status=409)
    except DatabaseUnavailable as exc:
        return _fail(f"数据库不可用：{exc}", status=503)
    return _ok(item)


def update_role(role_id: int, payload: dict) -> dict:
    if (blocked := _must_mysql()) is not None:
        return blocked

    fields: dict = {}
    perms: list[str] | None = None

    if "roleName" in payload and payload["roleName"] is not None:
        name, err = _clean_role_name(payload["roleName"])
        if err:
            return _fail(err)
        fields["role_name"] = name
    if "remark" in payload and payload["remark"] is not None:
        fields["remark"] = _clean_remark(payload["remark"])
    if "permissions" in payload and payload["permissions"] is not None:
        perms, err = _clean_permissions(payload["permissions"])
        if err:
            return _fail(err)

    if not fields and perms is None:
        return _fail("没有可更新的字段")

    try:
        with db_transaction() as conn:
            existing = rbac_repo.get_role(conn, role_id)
            if existing is None or existing["roleType"] != 2:
                return _fail(f"角色不存在：roleId={role_id}", status=404)

            code = existing["roleCode"]
            if code in BUILTIN_CODES:
                if "role_name" in fields and fields["role_name"] != existing["roleName"]:
                    return _fail("内置角色不可重命名")
            if code == ADMIN_CODE and perms is not None:
                return _fail("管理员角色始终拥有全部权限，无需配置")

            if "role_name" in fields:
                dup = rbac_repo.find_role(conn, name=fields["role_name"])
                if dup is not None and dup["roleId"] != role_id:
                    return _fail(f"角色名称已存在：{fields['role_name']}", status=409)

            rbac_repo.update_role(conn, role_id, **fields)
            if perms is not None:
                rbac_repo.set_role_permissions(conn, role_id, perms)
            item = _role_item(conn, role_id)
    except IntegrityError:
        return _fail("角色名称已存在", status=409)
    except DatabaseUnavailable as exc:
        return _fail(f"数据库不可用：{exc}", status=503)
    return _ok(item)


def delete_role(role_id: int) -> dict:
    if (blocked := _must_mysql()) is not None:
        return blocked

    try:
        with db_transaction() as conn:
            existing = rbac_repo.get_role(conn, role_id)
            if existing is None or existing["roleType"] != 2:
                return _fail(f"角色不存在：roleId={role_id}", status=404)
            if existing["roleCode"] in BUILTIN_CODES:
                return _fail("内置角色不可删除")
            bound = rbac_repo.role_bound_accounts(conn, role_id)
            if bound:
                return _fail(f"该角色仍绑定 {bound} 个账号，请先调整账号角色", status=409)
            rbac_repo.delete_role(conn, role_id)
    except DatabaseUnavailable as exc:
        return _fail(f"数据库不可用：{exc}", status=503)
    return _ok({"roleId": role_id, "deleted": True})


# ------------------------------------------------------------------ 管理员账号
def _active_admin_count(conn, *, exclude_user_id: int | None = None) -> int:
    stmt = (
        select(func.count(func.distinct(M.SysUser.user_id)))
        .select_from(M.SysUser)
        .join(M.SysUserRole, M.SysUserRole.user_id == M.SysUser.user_id)
        .join(M.SysRole, M.SysRole.role_id == M.SysUserRole.role_id)
        .where(M.SysRole.role_code == ADMIN_CODE, M.SysUser.user_status == 1)
    )
    if exclude_user_id is not None:
        stmt = stmt.where(M.SysUser.user_id != exclude_user_id)
    return int(conn.execute(stmt).scalar() or 0)


def _is_admin_account(item: dict | None) -> bool:
    return bool(item and any(r["roleCode"] == ADMIN_CODE for r in item["roles"]))


def _load_admin(conn, user_id: int) -> dict | None:
    return next((a for a in rbac_repo.list_admins(conn) if a["userId"] == user_id), None)


def _management_role_ids(conn, role_ids: list[int]) -> tuple[list[dict] | None, str | None]:
    ids = list(dict.fromkeys(int(r) for r in role_ids))
    if not ids:
        return None, "至少选择一个管理角色"
    rows = conn.execute(
        select(M.SysRole.role_id, M.SysRole.role_code, M.SysRole.role_name)
        .where(M.SysRole.role_id.in_(ids), M.SysRole.role_type == 2)
        .order_by(M.SysRole.role_id)
    ).all()
    found = {int(r[0]) for r in rows}
    missing = [str(i) for i in ids if i not in found]
    if missing:
        return None, f"管理角色不存在：{', '.join(missing)}"
    return [
        {"roleId": int(r[0]), "roleCode": r[1], "roleName": r[2]} for r in rows
    ], None


def _clean_role_ids(raw) -> tuple[list[int] | None, str | None]:
    if raw is None:
        return None, "至少选择一个管理角色"
    if not isinstance(raw, (list, tuple)) or not raw:
        return None, "至少选择一个管理角色"
    try:
        return [int(r) for r in raw], None
    except (TypeError, ValueError):
        return None, "角色 ID 必须是整数"


def list_admins() -> dict:
    data = {"items": [], "roles": [], "editable": _mysql_mode()}
    if not _mysql_mode():
        data["note"] = "JSON 模式无账号表：切换到 MySQL 后可维护管理员账号。"
        return _ok(data)

    try:
        with db_session() as conn:
            data["items"] = rbac_repo.list_admins(conn)
            data["roles"] = [
                {"roleId": r["roleId"], "roleCode": r["roleCode"], "roleName": r["roleName"]}
                for r in rbac_repo.list_management_roles(conn)
            ]
            data["activeAdmins"] = _active_admin_count(conn)
    except DatabaseUnavailable as exc:
        return _fail(f"数据库不可用：{exc}", status=503)
    except Exception as exc:  # noqa: BLE001
        logger.exception("管理员清单查询失败")
        return _fail(f"管理员清单查询失败：{exc}", status=503)
    return _ok(data)


def create_admin(payload: dict) -> dict:
    if (blocked := _must_mysql()) is not None:
        return blocked

    user_no, err = _clean_user_no(payload.get("userNo"))
    if err:
        return _fail(err)
    real_name, err = _clean_real_name(payload.get("realName"))
    if err:
        return _fail(err)
    password, err = _clean_password(payload.get("password"))
    if err:
        return _fail(err)
    role_ids, err = _clean_role_ids(payload.get("roleIds"))
    if err:
        return _fail(err)
    raw_status = payload.get("status")
    status = 1 if raw_status is None or int(raw_status) == 1 else 0

    try:
        with db_transaction() as conn:
            roles, err = _management_role_ids(conn, role_ids)
            if err:
                return _fail(err)

            existing = rbac_repo.find_user_by_no(conn, user_no)
            if existing is not None:
                if rbac_repo.user_management_roles(conn, existing["userId"]):
                    return _fail(f"账号「{user_no}」已是管理员", status=409)
                user_id = existing["userId"]
                rbac_repo.update_user(
                    conn,
                    user_id,
                    real_name=real_name,
                    password_hash=passwords.hash_password(password),
                    user_status=status,
                )
            else:
                user_id = rbac_repo.create_user(
                    conn,
                    user_no=user_no,
                    real_name=real_name,
                    password_hash=passwords.hash_password(password),
                )
                if not status:
                    rbac_repo.update_user(conn, user_id, user_status=0)

            rbac_repo.set_user_management_roles(
                conn, user_id, [r["roleId"] for r in roles]
            )
            item = _load_admin(conn, user_id)
    except IntegrityError:
        return _fail(f"学工号已存在：{user_no}", status=409)
    except DatabaseUnavailable as exc:
        return _fail(f"数据库不可用：{exc}", status=503)
    return _ok(item)


def update_admin(
    user_id: int, payload: dict, *, actor_user_id: int | None = None
) -> dict:
    if (blocked := _must_mysql()) is not None:
        return blocked

    fields: dict = {}
    role_ids: list[int] | None = None

    if "realName" in payload and payload["realName"] is not None:
        real_name, err = _clean_real_name(payload["realName"])
        if err:
            return _fail(err)
        fields["real_name"] = real_name
    if "status" in payload and payload["status"] is not None:
        fields["user_status"] = 1 if int(payload["status"]) == 1 else 0
    if payload.get("password"):
        password, err = _clean_password(payload["password"])
        if err:
            return _fail(err)
        fields["password_hash"] = passwords.hash_password(password)
    if "roleIds" in payload and payload["roleIds"] is not None:
        role_ids, err = _clean_role_ids(payload["roleIds"])
        if err:
            return _fail(err)

    if not fields and role_ids is None:
        return _fail("没有可更新的字段")

    try:
        with db_transaction() as conn:
            target = _load_admin(conn, user_id)
            if target is None:
                return _fail(f"管理员不存在：userId={user_id}", status=404)

            roles: list[dict] | None = None
            if role_ids is not None:
                roles, err = _management_role_ids(conn, role_ids)
                if err:
                    return _fail(err)

            losing_admin = bool(
                roles is not None
                and _is_admin_account(target)
                and not any(r["roleCode"] == ADMIN_CODE for r in roles)
            )
            disabling = fields.get("user_status") == 0
            if disabling and actor_user_id == user_id:
                return _fail("不能停用当前登录账号")
            if losing_admin:
                if actor_user_id == user_id:
                    return _fail("不能移除当前登录账号的管理员角色")
                if _active_admin_count(conn, exclude_user_id=user_id) < 1:
                    return _fail("至少保留一个启用状态的管理员")
            if disabling and _is_admin_account(target):
                if _active_admin_count(conn, exclude_user_id=user_id) < 1:
                    return _fail("至少保留一个启用状态的管理员")

            rbac_repo.update_user(conn, user_id, **fields)
            if roles is not None:
                rbac_repo.set_user_management_roles(
                    conn, user_id, [r["roleId"] for r in roles]
                )
            item = _load_admin(conn, user_id)
    except IntegrityError:
        return _fail("账号信息冲突，请刷新后重试", status=409)
    except DatabaseUnavailable as exc:
        return _fail(f"数据库不可用：{exc}", status=503)
    return _ok(item)


def delete_admin(user_id: int, *, actor_user_id: int | None = None) -> dict:
    """移除账号的全部管理角色（保留身份账号本身，不影响其普通问答）。"""
    if (blocked := _must_mysql()) is not None:
        return blocked

    try:
        with db_transaction() as conn:
            target = _load_admin(conn, user_id)
            if target is None:
                return _fail(f"管理员不存在：userId={user_id}", status=404)
            if actor_user_id == user_id:
                return _fail("不能移除当前登录账号")
            if _is_admin_account(target) and _active_admin_count(conn, exclude_user_id=user_id) < 1:
                return _fail("至少保留一个启用状态的管理员")
            rbac_repo.set_user_management_roles(conn, user_id, [])
    except DatabaseUnavailable as exc:
        return _fail(f"数据库不可用：{exc}", status=503)
    return _ok({"userId": user_id, "removed": True})
