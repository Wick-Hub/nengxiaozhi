"""角色权限与管理账号仓储（任务 2.9）。

所有函数接收 Core Connection，字段对外一律 camelCase；SQL 过滤/排序下推，
不在内存里做全表过滤。
"""

from __future__ import annotations

from sqlalchemy import delete, func, insert, select, update

from app.db import models as M


# ------------------------------------------------------------------ 角色
def list_management_roles(conn) -> list[dict]:
    """管理角色（``role_type=2``）+ 各自权限点。"""
    rows = conn.execute(
        select(
            M.SysRole.role_id,
            M.SysRole.role_code,
            M.SysRole.role_name,
            M.SysRole.remark,
        )
        .where(M.SysRole.role_type == 2)
        .order_by(M.SysRole.role_id)
    ).all()
    ids = [int(r[0]) for r in rows]
    perms: dict[int, list[str]] = {i: [] for i in ids}
    if ids:
        for role_id, code in conn.execute(
            select(M.SysRolePermission.role_id, M.SysRolePermission.perm_code)
            .where(M.SysRolePermission.role_id.in_(ids))
            .order_by(M.SysRolePermission.perm_code)
        ).all():
            perms.setdefault(int(role_id), []).append(str(code))
    return [
        {
            "roleId": int(role_id),
            "roleCode": code,
            "roleName": name,
            "remark": remark or "",
            "permissions": perms.get(int(role_id), []),
            "permCount": len(perms.get(int(role_id), [])),
            "builtin": code in {"admin", "dept_liaison"},
        }
        for role_id, code, name, remark in rows
    ]


def get_role(conn, role_id: int) -> dict | None:
    row = conn.execute(
        select(
            M.SysRole.role_id,
            M.SysRole.role_code,
            M.SysRole.role_name,
            M.SysRole.role_type,
            M.SysRole.remark,
        ).where(M.SysRole.role_id == role_id)
    ).first()
    if row is None:
        return None
    return {
        "roleId": int(row[0]),
        "roleCode": row[1],
        "roleName": row[2],
        "roleType": int(row[3]),
        "remark": row[4] or "",
    }


def find_role(conn, *, code: str | None = None, name: str | None = None) -> dict | None:
    stmt = select(M.SysRole.role_id, M.SysRole.role_code, M.SysRole.role_name, M.SysRole.role_type)
    if code is not None:
        stmt = stmt.where(M.SysRole.role_code == code)
    elif name is not None:
        stmt = stmt.where(M.SysRole.role_name == name)
    else:
        return None
    row = conn.execute(stmt).first()
    if row is None:
        return None
    return {
        "roleId": int(row[0]),
        "roleCode": row[1],
        "roleName": row[2],
        "roleType": int(row[3]),
    }


def create_role(conn, *, code: str, name: str, remark: str = "") -> int:
    res = conn.execute(
        insert(M.SysRole).values(
            role_code=code, role_name=name, role_type=2, is_public=0, remark=remark
        )
    )
    return int(res.inserted_primary_key[0])


def update_role(conn, role_id: int, **fields) -> None:
    if fields:
        conn.execute(update(M.SysRole).where(M.SysRole.role_id == role_id).values(**fields))


def delete_role(conn, role_id: int) -> None:
    conn.execute(delete(M.SysRolePermission).where(M.SysRolePermission.role_id == role_id))
    conn.execute(delete(M.SysRole).where(M.SysRole.role_id == role_id))


def set_role_permissions(conn, role_id: int, codes: list[str]) -> None:
    conn.execute(delete(M.SysRolePermission).where(M.SysRolePermission.role_id == role_id))
    seen: list[str] = []
    for code in codes:
        if code not in seen:
            seen.append(code)
    if seen:
        conn.execute(
            insert(M.SysRolePermission),
            [{"role_id": role_id, "perm_code": code} for code in seen],
        )


def permissions_for_role(conn, role_id: int) -> list[str]:
    return [
        str(r[0])
        for r in conn.execute(
            select(M.SysRolePermission.perm_code)
            .where(M.SysRolePermission.role_id == role_id)
            .order_by(M.SysRolePermission.perm_code)
        ).all()
    ]


def role_bound_accounts(conn, role_id: int) -> int:
    return int(
        conn.execute(
            select(func.count())
            .select_from(M.SysUserRole)
            .where(M.SysUserRole.role_id == role_id)
        ).scalar()
        or 0
    )


# ------------------------------------------------------------------ 管理员账号
def list_admins(conn) -> list[dict]:
    """所有绑定过管理角色的账号（一人多角色时合并为一行）。"""
    rows = conn.execute(
        select(
            M.SysUser.user_id,
            M.SysUser.user_no,
            M.SysUser.real_name,
            M.SysUser.user_status,
            M.SysUser.last_login_at,
            M.SysUser.password_hash,
            M.SysRole.role_id,
            M.SysRole.role_code,
            M.SysRole.role_name,
        )
        .join(M.SysUserRole, M.SysUserRole.user_id == M.SysUser.user_id)
        .join(M.SysRole, M.SysRole.role_id == M.SysUserRole.role_id)
        .where(M.SysRole.role_type == 2)
        .order_by(M.SysUser.user_id, M.SysRole.role_id)
    ).all()

    out: dict[int, dict] = {}
    for uid, user_no, real_name, status, last_login, pwd_hash, rid, rcode, rname in rows:
        uid = int(uid)
        item = out.setdefault(
            uid,
            {
                "userId": uid,
                "userNo": user_no,
                "realName": real_name or "",
                "status": int(status),
                "lastLoginAt": str(last_login) if last_login else None,
                "hasPassword": bool(pwd_hash),
                "roles": [],
                "roleIds": [],
            },
        )
        item["roles"].append({"roleId": int(rid), "roleCode": rcode, "roleName": rname})
        item["roleIds"].append(int(rid))
    return list(out.values())


def get_user(conn, user_id: int) -> dict | None:
    row = conn.execute(
        select(
            M.SysUser.user_id,
            M.SysUser.user_no,
            M.SysUser.real_name,
            M.SysUser.id_type,
            M.SysUser.user_status,
            M.SysUser.password_hash,
        ).where(M.SysUser.user_id == user_id)
    ).first()
    if row is None:
        return None
    return {
        "userId": int(row[0]),
        "userNo": row[1],
        "realName": row[2] or "",
        "idType": int(row[3]),
        "status": int(row[4]),
        "hasPassword": bool(row[5]),
    }


def find_user_by_no(conn, user_no: str) -> dict | None:
    row = conn.execute(
        select(M.SysUser.user_id).where(M.SysUser.user_no == user_no)
    ).first()
    return get_user(conn, int(row[0])) if row else None


def create_user(
    conn, *, user_no: str, real_name: str, id_type: int = 3, password_hash: str
) -> int:
    res = conn.execute(
        insert(M.SysUser).values(
            user_no=user_no,
            real_name=real_name,
            id_type=id_type,
            user_status=1,
            password_hash=password_hash,
        )
    )
    return int(res.inserted_primary_key[0])


def update_user(conn, user_id: int, **fields) -> None:
    if fields:
        conn.execute(update(M.SysUser).where(M.SysUser.user_id == user_id).values(**fields))


def set_user_management_roles(conn, user_id: int, role_ids: list[int]) -> None:
    """只替换「管理角色」绑定，保留身份角色（如本科生）。"""
    mgmt_ids = [
        int(r[0])
        for r in conn.execute(
            select(M.SysRole.role_id).where(M.SysRole.role_type == 2)
        ).all()
    ]
    if mgmt_ids:
        conn.execute(
            delete(M.SysUserRole).where(
                M.SysUserRole.user_id == user_id,
                M.SysUserRole.role_id.in_(mgmt_ids),
            )
        )
    for rid in role_ids:
        exists = conn.execute(
            select(func.count())
            .select_from(M.SysUserRole)
            .where(M.SysUserRole.user_id == user_id, M.SysUserRole.role_id == rid)
        ).scalar()
        if not exists:
            conn.execute(insert(M.SysUserRole).values(user_id=user_id, role_id=rid))


def user_management_roles(conn, user_id: int) -> list[dict]:
    rows = conn.execute(
        select(M.SysRole.role_id, M.SysRole.role_code, M.SysRole.role_name)
        .join(M.SysUserRole, M.SysUserRole.role_id == M.SysRole.role_id)
        .where(M.SysUserRole.user_id == user_id, M.SysRole.role_type == 2)
        .order_by(M.SysRole.role_id)
    ).all()
    return [
        {"roleId": int(rid), "roleCode": code, "roleName": name}
        for rid, code, name in rows
    ]


def permissions_for_user(conn, user_id: int) -> list[str]:
    return [
        str(r[0])
        for r in conn.execute(
            select(M.SysRolePermission.perm_code)
            .join(M.SysUserRole, M.SysUserRole.role_id == M.SysRolePermission.role_id)
            .where(M.SysUserRole.user_id == user_id)
            .order_by(M.SysRolePermission.perm_code)
        ).all()
    ]
