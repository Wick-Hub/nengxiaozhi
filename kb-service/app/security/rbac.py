"""权限：RBAC（任务 1.13）。

角色模型与 ``sys_role`` / ``/api/meta`` 的 ``authRoles`` 对齐：

    访客 / 本科生 / 研究生 / 教职工 / 离退休人员 / 部门联络员 / 管理员

> 注意是「**离退休人员**」（与原型 ``meta`` 一致），不是「离退休」。

规则：
- 公开接口 + 访客问答无需登录（无 token 即访客）。
- 管理接口需**管理员**（``ADMIN_ROLES``）。
- 知识库按部门授权（批2 任务 2.6，本批不做）。
"""

from __future__ import annotations

#: 全部认证角色（与 app/api/meta.py 的 AUTH_ROLES 保持一致）
ROLES: tuple[str, ...] = (
    "访客",
    "本科生",
    "研究生",
    "教职工",
    "离退休人员",
    "部门联络员",
    "管理员",
)

ADMIN_ROLES: frozenset[str] = frozenset({"管理员"})
#: 可进入管理端的角色（任务 2.9：具体能看哪块由角色权限矩阵约束）
MANAGEMENT_ROLES: frozenset[str] = frozenset({"管理员", "部门联络员"})
DEFAULT_ROLE = "访客"

#: ``sys_user.id_type`` → 身份角色名（1 本科生 / 2 研究生 / 3 教职工 / 4 离退休 / 5 访客）
ID_TYPE_ROLE: dict[int, str] = {
    1: "本科生",
    2: "研究生",
    3: "教职工",
    4: "离退休人员",
    5: "访客",
}


def is_admin(role: str | None) -> bool:
    return (role or DEFAULT_ROLE) in ADMIN_ROLES


def is_management(role: str | None) -> bool:
    return (role or DEFAULT_ROLE) in MANAGEMENT_ROLES


def can_access_admin(role: str | None) -> bool:
    """管理接口准入：当前仅管理员。"""
    return is_admin(role)


def is_valid_role(role: str | None) -> bool:
    return role in ROLES


def role_for_id_type(id_type: int | None) -> str:
    """把 ``sys_user.id_type`` 映射为角色名，未知值降级为访客。"""
    return ID_TYPE_ROLE.get(int(id_type or 0), DEFAULT_ROLE)
