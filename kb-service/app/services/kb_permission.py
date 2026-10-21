"""知识库部门授权服务（任务 2.6 / M-K-03）。

两种存储模式的分工：

* ``KB_STORE=mysql`` —— 全功能。授权是 ``kb_permission`` 表上的真实记录
  （用户 × 部门 × 知识类型 → 五操作位），支持增删改；校验函数据此判定
  「部门联络员能否在某类目下做某操作」。
* ``KB_STORE=json`` —— **只读推导**。无授权实体，``editable=False``，
  写操作返回 400 + 指引；校验函数在未开启鉴权时放行（开发/演示）。

校验规则（``can_operate``）：

* ``AUTH_ENABLED=false`` —— 放行（开发/演示阶段，沿用批1 口径）；
* 无身份（访客）或 ``user_id`` 缺失 —— 放行管理接口的「管理性」用途已由
  ``require_admin`` 在鉴权开启时兜底；这里只拦**已登录的部门联络员**的越权；
* 管理员（``is_admin``）—— 恒放行；
* 部门联络员 —— 需有命中 （用户 × 部门 × 类型） 的授权，且对应操作位为 1，
  否则 **403**；
* 其他已登录角色 —— **403**（非联络员无知识库管理权限）。

对外授权键是可操作位（camelCase）：``canUpload / canView / canDownload /
canDelete / canCreateSub``，与 ``kb_repo.PERM_FIELDS`` 一一对应。
"""

from __future__ import annotations

import logging

from app.config import settings
from app.db import kb_repo
from app.db.session import DatabaseUnavailable, db_session, db_transaction
from app.security.deps import Principal
from app.security import rbac

logger = logging.getLogger(__name__)

#: 可操作位（camelCase）→ 语义
ACTIONS = (
    "canUpload",
    "canView",
    "canDownload",
    "canDelete",
    "canCreateSub",
)

_ACTION_COL = kb_repo.PERM_FIELDS


def _ok(data) -> dict:
    return {"ok": True, "data": data, "error": None}


def _fail(message: str, status: int = 400) -> dict:
    """失败结果。``status`` 供接口层映射 HTTP 状态码（400 参数 / 404 不存在 / 409 冲突）。"""
    return {"ok": False, "data": None, "error": message, "status": status}


def _mysql_mode() -> bool:
    return settings.store_label == "mysql"


def _must_mysql() -> dict | None:
    if _mysql_mode():
        return None
    return _fail(
        "知识库部门授权需要 MySQL 存储：请在 .env 中设置 KB_STORE=mysql 并确保数据库已启动",
        status=400,
    )


def _pick_actions(payload: dict) -> dict:
    """提取五个操作位的 0/1（默认 0，未知字段忽略）。"""
    return {
        col: int(1 if payload.get(camel) else 0)
        for camel, col in _ACTION_COL.items()
    }


# ------------------------------------------------------------------ 查询
def list_permissions(user_id: int | None = None) -> dict:
    """授权清单；返回 ``items`` + ``editable`` + ``liaisons``（被授权人下拉）。"""
    try:
        if _mysql_mode():
            items = kb_repo.list_permissions(user_id=user_id)
            editable = True
            liaisons = kb_repo.list_dept_liaisons()
            depts = kb_repo.list_orgs()
        else:
            items = []
            editable = False
            liaisons = []
            depts = []
    except DatabaseUnavailable as exc:
        return _fail(f"数据库不可用：{exc}", status=503)

    return _ok(
        {
            "items": items,
            "total": len(items),
            "editable": editable,
            "liaisons": liaisons,
            "depts": depts,
            "ktypes": ["政策文件", "办事指南", "常见问题", "服务事项", "组织机构"],
            "actions": list(ACTIONS),
        }
    )


# ------------------------------------------------------------------ MySQL 写操作
def grant(payload: dict, principal: Principal) -> dict:
    """授予权限（M-K-03）。已存在同键记录时返回 409。"""
    if (blocked := _must_mysql()) is not None:
        return blocked

    user_id = payload.get("userId")
    if not user_id:
        return _fail("必须提供被授权用户（userId）", status=400)
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return _fail(f"被授权用户非法：{user_id}", status=400)

    dept_id = int(payload.get("deptId") or 0)
    ktype = (payload.get("ktype") or "*").strip()
    if not ktype:
        return _fail("知识类型不能为空", status=400)

    if not any(payload.get(a) for a in ACTIONS):
        return _fail("至少勾选一个操作位", status=400)

    with db_transaction() as conn:
        if kb_repo.get_user(conn, user_id) is None:
            return _fail(f"用户不存在：userId={user_id}", status=400)
        if dept_id and not kb_repo.dept_exists(conn, dept_id):
            return _fail(f"部门不存在：deptId={dept_id}", status=400)
        existing = kb_repo.find_permission(conn, user_id, dept_id, ktype)
        if existing is not None:
            return _fail("该用户在此部门/类型下已有授权，请用更新接口", status=409)
        perm_id = kb_repo.create_permission(
            conn,
            user_id=user_id,
            dept_id=dept_id,
            ktype=ktype,
            created_by=principal.user_id,
            **_pick_actions(payload),
        )
        item = kb_repo.get_permission(conn, perm_id)
    return _ok(item)


def update(perm_id: int, payload: dict) -> dict:
    """更新某授权记录的操作位（M-K-03）。不存在返回 404。"""
    if (blocked := _must_mysql()) is not None:
        return blocked

    with db_transaction() as conn:
        existing = kb_repo.get_permission(conn, perm_id)
        if existing is None:
            return _fail(f"授权不存在：permId={perm_id}", status=404)
        kb_repo.update_permission(conn, perm_id, **_pick_actions(payload))
        item = kb_repo.get_permission(conn, perm_id)
    return _ok(item)


def revoke(perm_id: int) -> dict:
    """撤销授权。不存在返回 404。"""
    if (blocked := _must_mysql()) is not None:
        return blocked

    with db_transaction() as conn:
        existing = kb_repo.get_permission(conn, perm_id)
        if existing is None:
            return _fail(f"授权不存在：permId={perm_id}", status=404)
        kb_repo.delete_permission(conn, perm_id)
    return _ok({"permId": perm_id, "deleted": True})


# ------------------------------------------------------------------ 校验
def can_operate(principal: Principal, dept_id: int, ktype: str, action: str) -> bool:
    """判定用户是否可对 （部门 × 类型） 执行 ``action``。

    ``action`` 是 ``ACTIONS`` 里的 camelCase 键。
    """
    if action not in _ACTION_COL:
        return False
    # 开发/演示：未开启鉴权，一律放行（沿用批1 口径）
    if not settings.auth_enabled:
        return True
    # 管理员恒放行
    if principal.is_admin:
        return True
    # 机器身份/访客：无 user_id 的用户无需做部门授权判定——
    # 访客问答走公开链路，管理接口由 require_admin 兜底；这里只拦
    # 「已登录但非管理员」且想操作知识库的用户。
    if not principal.authenticated or principal.user_id is None:
        return False
    # 只有「部门联络员」角色能被授权
    if principal.role != "部门联络员":
        return False
    # 查有效授权
    with db_session() as conn:
        perm = kb_repo.user_permission_for(conn, principal.user_id, dept_id, ktype)
    if not perm:
        return False
    return bool(perm[action])


def require_can(principal: Principal, dept_id: int, ktype: str, action: str) -> None:
    """校验用法：不满足时抛 ``PermissionError``（接口层转 403）。"""
    if not can_operate(principal, dept_id, ktype, action):
        raise PermissionError(
            f"当前用户无「{action}」权限：部门={dept_id}，类型={ktype}"
        )


def resolve_dept_id(dept: str | None, dept_id: int | None = None) -> tuple[int | None, str | None]:
    """把部门名/id 解析成 ``org_id``；返回 ``(dept_id, error)``。

    ``dept_id`` 已给则直接用；否则按名称查（仅 MySQL 模式可查）。
    """
    if dept_id is not None:
        try:
            return int(dept_id), None
        except (TypeError, ValueError):
            return None, f"部门 id 非法：{dept_id}"
    name = (dept or "").strip()
    if not name:
        return None, "缺少部门"
    if not _mysql_mode():
        return None, None
    with db_session() as conn:
        found = kb_repo.dept_id_by_name(conn, name)
    if found is None:
        return None, f"部门不存在：{name}"
    return found, None
