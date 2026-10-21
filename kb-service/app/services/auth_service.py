"""认证服务（任务 1.13 / 2.9）：内部账号解析与演示数据 seed。

「先内部账号，后续接 CAS」：本阶段以 ``sys_user`` 为账号源，凭学工号换 JWT。
管理账号在系统管理中创建时必须设置密码（PBKDF2）；历史演示账号
``password_hash IS NULL`` 仍沿用免密登录，便于开发与演示。
"""

from __future__ import annotations

import logging

from sqlalchemy import func, insert, select, update

from app.config import settings
from app.db import models as M
from app.db import rbac_repo
from app.db.session import DatabaseUnavailable, db_transaction
from app.security import passwords, permissions, rbac
from app.services import sso

logger = logging.getLogger(__name__)

#: 演示角色 (role_code, role_name, role_type, is_public, remark)
SEED_ROLES: list[tuple[str, str, int, int, str]] = [
    ("visitor", "访客", 1, 1, "无需登录的公开身份"),
    ("undergrad", "本科生", 1, 0, ""),
    ("graduate", "研究生", 1, 0, ""),
    ("teacher", "教职工", 1, 0, ""),
    ("retired", "离退休人员", 1, 0, ""),
    ("dept_liaison", "部门联络员", 2, 0, "本部门知识维护（批2 任务 2.6）"),
    ("admin", "管理员", 2, 0, "管理后台全部权限"),
]

#: 演示账号 (user_no, real_name, id_type, role_code)
SEED_USERS: list[tuple[str, str, int, str]] = [
    ("20230001", "本科生演示账号", 1, "undergrad"),
    ("20240001", "研究生演示账号", 2, "graduate"),
    ("T10001", "教职工演示账号", 3, "teacher"),
    ("admin", "系统管理员", 3, "admin"),
]


class AuthError(Exception):
    """无法确定身份（未登记且未提供有效角色）。"""


def _resolve_role(rows: list[tuple[str, int, int]]) -> tuple[str | None, int | None, int | None]:
    """从绑定角色里挑一个：管理员优先 → 其他管理角色 → 已知身份角色。

    自定义管理角色（任务 2.9 新建）不在 ``rbac.ROLES`` 里，不能按
    ``is_valid_role`` 过滤掉，否则登录后会掉回 ``id_type`` 身份。
    """
    for name, rid, rtype in rows:
        if rbac.is_admin(name):
            return name, rid, rtype
    for name, rid, rtype in rows:
        if int(rtype) == 2:
            return name, rid, rtype
    known = [(name, rid, rtype) for name, rid, rtype in rows if rbac.is_valid_role(name)]
    if known:
        name, rid, rtype = known[0]
        return name, rid, rtype
    return None, None, None


def authenticate(
    user_no: str, role: str | None = None, password: str | None = None
) -> dict:
    """按学工号解析身份。

    - 已在 ``sys_user`` 登记：用其绑定角色（管理员优先），否则按 ``id_type`` 映射；
      设置了密码的账号必须提供正确密码，停用账号拒绝登录。
    - 未登记：内部演示阶段允许按请求角色放行（仅普通身份；管理角色必须先在
      系统管理中创建账号）。
    """
    user_no = (user_no or "").strip()
    if not user_no:
        raise AuthError("学工号不能为空")

    with db_transaction() as conn:
        # 注意：db_transaction 返回 Core Connection，ORM select(Entity) 不会实体化，
        # 必须显式列出需要的列（本项目数据层统一按此约定书写）。
        row = conn.execute(
            select(
                M.SysUser.user_id,
                M.SysUser.id_type,
                M.SysUser.user_status,
                M.SysUser.password_hash,
            ).where(M.SysUser.user_no == user_no)
        ).first()

        if row is None:
            if not rbac.is_valid_role(role):
                raise AuthError(f"未登记的学工号「{user_no}」，请提供有效角色")
            if rbac.is_management(role):
                raise AuthError("管理角色需先由管理员在「系统管理 · 角色权限」中创建账号")
            logger.info("未登记学工号 %s 以角色「%s」登录（内部演示阶段）", user_no, role)
            return {
                "userNo": user_no,
                "role": role,
                "userId": None,
                "roleId": None,
                "roleType": 1,
                "manager": False,
                "permissions": [],
            }

        user_id, id_type, user_status, password_hash = (
            int(row[0]),
            row[1],
            row[2],
            row[3],
        )
        if int(user_status if user_status is not None else 1) != 1:
            raise AuthError("账号已停用，请联系管理员")
        if password_hash:
            if not password:
                raise AuthError("该账号需要密码登录")
            if not passwords.verify_password(password, password_hash):
                raise AuthError("学工号或密码错误")

        identity = _identity_for_user(conn, user_id, user_no, id_type)
        conn.execute(
            update(M.SysUser)
            .where(M.SysUser.user_id == user_id)
            .values(last_login_at=func.now())
        )
        return identity


def _identity_for_user(
    conn, user_id: int, user_no: str, id_type: int, fallback_role: str = ""
) -> dict:
    """按本地绑定角色组装身份；无绑定角色时回退到普通身份角色。"""
    bound_rows = conn.execute(
        select(M.SysRole.role_name, M.SysRole.role_id, M.SysRole.role_type)
        .join(M.SysUserRole, M.SysUserRole.role_id == M.SysRole.role_id)
        .where(M.SysUserRole.user_id == user_id)
        .order_by(M.SysRole.role_id)
    ).all()
    resolved, role_id, role_type = _resolve_role(
        [(str(name), int(rid), int(rtype)) for name, rid, rtype in bound_rows]
    )
    if resolved is None:
        # 上游身份属性只用于普通身份；管理角色必须来自本地绑定（防提权）
        if fallback_role and rbac.is_valid_role(fallback_role) and not rbac.is_management(fallback_role):
            resolved = fallback_role
        else:
            resolved = rbac.role_for_id_type(id_type)
        role_id, role_type = None, 1

    manager = any(int(rtype) == 2 for _, _, rtype in bound_rows)
    if rbac.is_admin(resolved):
        granted = permissions.all_codes()
    elif manager:
        owned = set(rbac_repo.permissions_for_user(conn, user_id))
        granted = [code for code in permissions.all_codes() if code in owned]
    else:
        granted = []

    return {
        "userNo": user_no,
        "role": resolved,
        "userId": user_id,
        "roleId": role_id,
        "roleType": role_type,
        "manager": manager,
        "permissions": granted,
    }


def resolve_sso(user_no: str, role_hint: str = "", real_name: str = "") -> dict:
    """CAS 身份落地：已登记账号按本地绑定角色，未登记按普通身份属性放行。

    统一身份认证是权威账号源，不校验本地密码；停用账号仍拒绝登录。
    数据库不可用时退化为「仅身份、无权限」的普通角色（不授予管理权限）。
    """
    user_no = (user_no or "").strip()
    if not user_no:
        raise AuthError("统一身份认证未返回学工号")
    mapped = sso.map_role_hint(role_hint)
    if mapped and rbac.is_management(mapped):
        mapped = ""  # 上游属性不得直接授予管理角色，必须本地绑定

    try:
        with db_transaction() as conn:
            row = conn.execute(
                select(
                    M.SysUser.user_id,
                    M.SysUser.id_type,
                    M.SysUser.user_status,
                ).where(M.SysUser.user_no == user_no)
            ).first()
            if row is None:
                logger.info("CAS 用户 %s 未登记，按身份属性「%s」放行", user_no, mapped or "访客")
                return {
                    "userNo": user_no,
                    "role": mapped or rbac.DEFAULT_ROLE,
                    "userId": None,
                    "roleId": None,
                    "roleType": 1,
                    "manager": False,
                    "permissions": [],
                }
            user_id, id_type, user_status = int(row[0]), row[1], row[2]
            if int(user_status if user_status is not None else 1) != 1:
                raise AuthError("账号已停用，请联系管理员")
            identity = _identity_for_user(
                conn,
                user_id,
                user_no,
                id_type,
                mapped if settings.cas_trust_role_attribute else "",
            )
            conn.execute(
                update(M.SysUser)
                .where(M.SysUser.user_id == user_id)
                .values(last_login_at=func.now())
            )
            return identity
    except DatabaseUnavailable:
        logger.warning("账号库不可用，CAS 用户 %s 仅按身份属性登录", user_no)
        return {
            "userNo": user_no,
            "role": mapped or rbac.DEFAULT_ROLE,
            "userId": None,
            "roleId": None,
            "roleType": 1,
            "manager": False,
            "permissions": [],
        }


def ensure_seed(conn) -> dict:
    """幂等写入演示角色 / 账号 / 绑定关系，返回新建计数。

    角色按 ``role_code`` upsert：已存在但 ``role_name`` 变更时同步更新
    （例如「离退休」→「离退休人员」的口径统一）。
    """
    created = {"roles": 0, "roles_updated": 0, "users": 0, "bindings": 0}

    for code, name, rtype, is_public, remark in SEED_ROLES:
        row = conn.execute(
            select(M.SysRole.role_id, M.SysRole.role_name).where(M.SysRole.role_code == code)
        ).first()
        if row is None:
            conn.execute(
                insert(M.SysRole).values(
                    role_code=code,
                    role_name=name,
                    role_type=rtype,
                    is_public=is_public,
                    remark=remark,
                )
            )
            created["roles"] += 1
        elif row[1] != name:
            conn.execute(
                update(M.SysRole)
                .where(M.SysRole.role_code == code)
                .values(role_name=name, role_type=rtype, is_public=is_public, remark=remark)
            )
            created["roles_updated"] += 1

    role_id_by_code = dict(
        conn.execute(select(M.SysRole.role_code, M.SysRole.role_id)).all()
    )

    for user_no, real_name, id_type, role_code in SEED_USERS:
        row = conn.execute(
            select(M.SysUser.user_id).where(M.SysUser.user_no == user_no)
        ).first()
        if row:
            uid = row[0]
        else:
            res = conn.execute(
                insert(M.SysUser).values(
                    user_no=user_no, real_name=real_name, id_type=id_type, user_status=1
                )
            )
            uid = res.inserted_primary_key[0]
            created["users"] += 1

        rid = role_id_by_code.get(role_code)
        if rid and not conn.execute(
            select(M.SysUserRole.id).where(
                M.SysUserRole.user_id == uid, M.SysUserRole.role_id == rid
            )
        ).first():
            conn.execute(insert(M.SysUserRole).values(user_id=uid, role_id=rid))
            created["bindings"] += 1

    # 部门联络员的默认权限点只在首次 seed 时写入：已有配置（哪怕被清空）不覆盖。
    created["permissions"] = 0
    liaison_id = role_id_by_code.get("dept_liaison")
    if liaison_id:
        existing = conn.execute(
            select(func.count())
            .select_from(M.SysRolePermission)
            .where(M.SysRolePermission.role_id == liaison_id)
        ).scalar() or 0
        if not existing:
            conn.execute(
                insert(M.SysRolePermission),
                [
                    {"role_id": liaison_id, "perm_code": code}
                    for code in permissions.LIAISON_DEFAULT_PERMS
                ],
            )
            created["permissions"] = len(permissions.LIAISON_DEFAULT_PERMS)

    return created


def count_accounts() -> dict:
    """账号计数（健康检查 / 验收用）。"""
    with db_transaction() as conn:
        return {
            "roles": conn.execute(select(func.count()).select_from(M.SysRole)).scalar() or 0,
            "users": conn.execute(select(func.count()).select_from(M.SysUser)).scalar() or 0,
        }
