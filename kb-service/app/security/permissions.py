"""权限点目录与判定（任务 2.9 / M-SYS-01·03）。

两层模型与 2.6 正交：

* **2.6 部门授权** 管「能碰哪些数据」（用户 × 部门 × 知识类型）；
* **本模块** 管「能进哪些页面、能点哪些按钮」（角色 × 权限点）。

管理员角色 ``管理员`` 恒有全部权限；其他管理角色按 ``sys_role_permission``
配置。``AUTH_ENABLED=false``（开发/演示）时一律放行，保持既有行为。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.config import settings
from app.db import models as M
from app.db.session import db_session

if TYPE_CHECKING:  # pragma: no cover
    from app.security.deps import Principal

logger = logging.getLogger(__name__)

#: 权限点目录：模块 → 权限点（菜单级 / 按钮级）
GROUPS: list[dict] = [
    {
        "module": "docs",
        "name": "知识文档",
        "perms": [
            ("docs.view", "查看文档与切片"),
            ("docs.upload", "上传 / 增量同步"),
            ("docs.audit", "审核与状态变更"),
            ("docs.delete", "删除文档"),
        ],
    },
    {
        "module": "categories",
        "name": "知识分类",
        "perms": [
            ("categories.view", "查看分类"),
            ("categories.manage", "新建 / 重命名 / 删除分类"),
        ],
    },
    {
        "module": "deptperm",
        "name": "部门授权",
        "perms": [
            ("deptperm.view", "查看部门授权"),
            ("deptperm.manage", "授予 / 更新 / 撤销"),
        ],
    },
    {
        "module": "points",
        "name": "知识点",
        "perms": [
            ("points.view", "查看知识点"),
            ("points.edit", "编辑 / 再加工 / 删除"),
            ("points.export", "批量导出"),
        ],
    },
    {
        "module": "chatlogs",
        "name": "会话日志",
        "perms": [
            ("chatlogs.view", "查看会话日志"),
            ("chatlogs.export", "导出会话日志"),
        ],
    },
    {
        "module": "stats",
        "name": "统计分析",
        "perms": [
            ("stats.view", "查看统计与大屏"),
            ("stats.export", "导出统计报表"),
        ],
    },
    {
        "module": "unrecognized",
        "name": "未识别问题",
        "perms": [
            ("unrecognized.view", "查看未识别问题与追溯会话"),
            ("unrecognized.manage", "批量处理 / 导出 / 反哺知识"),
        ],
    },
    {
        "module": "feedback",
        "name": "用户反馈",
        "perms": [
            ("feedback.view", "查看点赞/点踩记录"),
            ("feedback.manage", "导出 / 标记处理"),
        ],
    },
    {
        "module": "support",
        "name": "人工客服",
        "perms": [
            ("support.view", "查看工单与会话"),
            ("support.manage", "接入 / 回复 / 转交 / 结束会话"),
        ],
    },
    {
        "module": "gov",
        "name": "导办事项",
        "perms": [
            ("gov.view", "查看事项与同步状态"),
            ("gov.manage", "维护事项 / 启停 / 同步导入"),
        ],
    },
    {
        "module": "access",
        "name": "通行分析",
        "perms": [
            ("access.view", "查看通行统计 / 下钻 / 明细"),
            ("access.manage", "同步门禁流水"),
            ("access.export", "导出通行报告与明细"),
        ],
    },
    {
        "module": "datacenter",
        "name": "数据中台",
        "perms": [
            ("datacenter.view", "查看上报批次 / 同步任务 / 组织镜像"),
            ("datacenter.manage", "执行上报 / 组织同步 / 补跑定时任务"),
        ],
    },
    {
        "module": "integrations",
        "name": "集成中心",
        "perms": [
            ("integrations.view", "查看本地模型 / MCP / 第三方智能体"),
            ("integrations.manage", "配置、测试与切换外部集成"),
        ],
    },
    {
        "module": "agents",
        "name": "智能体",
        "perms": [
            ("agents.view", "查看 / 对话测试"),
            ("agents.manage", "新建 / 编辑 / 上下架 / 删除"),
        ],
    },
    {
        "module": "channels",
        "name": "渠道分身",
        "perms": [
            ("channels.view", "查看渠道与嵌入代码"),
            ("channels.manage", "新建 / 编辑 / 启停 / 删除渠道"),
        ],
    },
    {
        "module": "apps",
        "name": "应用广场",
        "perms": [
            ("apps.view", "查看应用与角色授权"),
            ("apps.manage", "新建 / 编辑 / 排序 / 启停 / 删除应用"),
        ],
    },
    {
        "module": "gateway",
        "name": "模型网关",
        "perms": [
            ("gateway.view", "查看模型与探测"),
            ("gateway.manage", "新增 / 编辑 / 删除模型"),
        ],
    },
    {
        "module": "system",
        "name": "系统管理",
        "perms": [
            ("system.words", "词库管理"),
            ("system.logs", "操作日志"),
        ],
    },
    {
        "module": "rbac",
        "name": "角色权限",
        "perms": [
            ("rbac.view", "查看角色与管理员"),
            ("rbac.manage", "配置角色矩阵 / 管理员账号"),
        ],
    },
]

ALL_CODES: frozenset[str] = frozenset(
    code for group in GROUPS for code, _ in group["perms"]
)
LABELS: dict[str, str] = {
    code: label for group in GROUPS for code, label in group["perms"]
}


def all_codes() -> list[str]:
    """按目录顺序返回全部权限点（前端勾选与展示顺序一致）。"""
    return [code for group in GROUPS for code, _ in group["perms"]]

#: 部门联络员默认权限（知识库数据范围仍由 2.6 的部门授权二次约束）
LIAISON_DEFAULT_PERMS: tuple[str, ...] = (
    "docs.view",
    "docs.upload",
    "docs.audit",
    "docs.delete",
    "categories.view",
    "points.view",
    "points.edit",
    "points.export",
)


def catalog() -> list[dict]:
    """前端矩阵渲染用：模块分组 + 权限点 + 中文标签。"""
    return [
        {
            "module": g["module"],
            "name": g["name"],
            "perms": [{"code": c, "label": label} for c, label in g["perms"]],
        }
        for g in GROUPS
    ]


def required_for(method: str, path: str) -> str | None:
    """按请求方法 + 路径映射所需权限点。

    管理端路由只挂了一个 ``require_admin``，在这里集中映射可以避免逐个
    端点漏挂；未识别的管理路径按最严格的 ``rbac.manage`` 处理。
    """
    m = method.upper()
    p = path.rstrip("/")

    if p.startswith("/api/admin/chatlogs"):
        return "chatlogs.export" if p.endswith("/export") else "chatlogs.view"
    if p.startswith("/api/admin/stats"):
        return "stats.export" if p.endswith("/export") else "stats.view"
    if p.startswith("/api/admin/unrecognized"):
        return "unrecognized.view" if m == "GET" and not p.endswith("/export") else (
            "unrecognized.manage"
        )
    if p.startswith("/api/admin/feedback"):
        return "feedback.view" if m == "GET" and not p.endswith("/export") else (
            "feedback.manage"
        )
    if p.startswith("/api/admin/support"):
        return "support.view" if m == "GET" else "support.manage"
    if p.startswith("/api/admin/gov"):
        return "gov.view" if m == "GET" else "gov.manage"
    if p.startswith("/api/admin/data-center"):
        return "datacenter.view" if m == "GET" else "datacenter.manage"
    if p.startswith("/api/admin/integrations"):
        return "integrations.view" if m == "GET" else "integrations.manage"
    if p.startswith("/api/admin/access"):
        if p.endswith("/export") or p.endswith("/report"):
            return "access.export"
        if m != "GET":
            return "access.manage"
        return "access.view"
    if p.startswith("/api/admin/agents"):
        return "agents.view" if m == "GET" or p.endswith("/test") else "agents.manage"
    if p.startswith("/api/admin/channels"):
        return "channels.view" if m == "GET" else "channels.manage"
    if p.startswith("/api/admin/apps"):
        return "apps.view" if m == "GET" else "apps.manage"
    if p.startswith("/api/admin/models"):
        return "gateway.view" if m == "GET" or p.endswith("/probe") else "gateway.manage"
    if p.startswith("/api/admin/gateway"):
        return "gateway.view" if m == "GET" or p.endswith("/probe") else "gateway.manage"
    if p.startswith("/api/admin/kb-categories"):
        return "categories.view" if m == "GET" else "categories.manage"
    if p.startswith("/api/admin/kb-permissions"):
        return "deptperm.view" if m == "GET" else "deptperm.manage"
    if p.startswith("/api/admin/operation-logs"):
        return "system.logs"
    if p.startswith("/api/admin/sensitive-words"):
        return "system.words"
    if p.startswith("/api/admin/roles") or p.startswith("/api/admin/admins"):
        return "rbac.view" if m == "GET" else "rbac.manage"

    if p.startswith("/api/docs"):
        if m in {"GET", "HEAD"}:
            return "docs.view"
        if m == "DELETE":
            return "docs.delete"
        # 批量端点混合审核/移动/删除：按审核放行，delete 动作在端点内二次校验 docs.delete
        if p == "/api/docs/batch" or m == "PATCH":
            return "docs.audit"
        return "docs.upload"
    if p.startswith("/api/knowledge"):
        if p.endswith("/export"):
            return "points.export"
        if m in {"GET", "HEAD"}:
            return "points.view"
        return "points.edit"
    if p.startswith("/api/sync"):
        return "docs.upload"

    if p.startswith("/api/admin"):
        return "rbac.manage"
    return None


def for_principal(principal: "Principal") -> frozenset[str]:
    """当前身份拥有的权限点集合。

    - 鉴权关闭 → 全部（开发/演示）；
    - 管理员 → 全部；
    - 其他管理角色 → ``sys_role_permission`` 中按用户/角色查出的并集；
    - JSON 模式没有角色矩阵 → 空集（仅管理员可用），数据库异常同样**失败关闭**。
    """
    if not settings.auth_enabled:
        return ALL_CODES
    if principal.is_admin:
        return ALL_CODES
    if settings.store_label != "mysql":
        return frozenset()
    if principal.user_id is None and principal.role_id is None:
        return frozenset()

    try:
        with db_session() as conn:
            stmt = select(M.SysRolePermission.perm_code).join(
                M.SysUserRole, M.SysUserRole.role_id == M.SysRolePermission.role_id
            )
            if principal.user_id is not None:
                stmt = stmt.where(M.SysUserRole.user_id == principal.user_id)
            else:
                stmt = stmt.where(M.SysRolePermission.role_id == principal.role_id)
            return frozenset(conn.execute(stmt).scalars().all()) & ALL_CODES
    except Exception as exc:  # noqa: BLE001 - 鉴权失败必须失败关闭
        logger.warning("读取角色权限失败（按无权限处理）：%s", exc)
        return frozenset()


def has(principal: "Principal", code: str) -> bool:
    return code in for_principal(principal)
