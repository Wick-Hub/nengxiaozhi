"""元数据接口：部门 / 知识类型 / 状态 / 角色。

部门列表与原型一致：**从已入库文档中推导**（管理页的部门输入框 datalist）。
数据库/索引不可用时回落到内置清单，保证管理页始终可渲染。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.config import settings
from app.core.index_store import get_index
from app.models.schemas import ApiResult

router = APIRouter()

# 内置回落清单（知识目录的标准部门）
FALLBACK_DEPTS = [
    "信网中心",
    "财务处",
    "学工部",
    "后勤处",
    "招生办",
    "国资处",
    "图书馆",
    "保卫处",
]
KTYPES = ["政策文件", "办事指南", "常见问题", "服务事项", "组织机构"]
STATUSES = [
    {"value": 0, "label": "草稿"},
    {"value": 1, "label": "待审核"},
    {"value": 2, "label": "已发布"},
    {"value": 3, "label": "已下线"},
]
# 知识可见角色字典，与原型 server.js 的 `['全部', ...ROLES]` 对齐。
# 「全部」必须排在首位：管理页据此把第一项（全部）默认勾选。
ROLES = ["全部", "访客", "本科生", "研究生", "教职工", "离退休人员"]
# 认证/授权角色（任务 1.13 JWT + RBAC 使用），不属于知识可见范围
AUTH_ROLES = ["访客", "本科生", "研究生", "教职工", "离退休人员", "部门联络员", "管理员"]


def _depts() -> list[str]:
    """从已入库文档推导部门（对齐原型 `[...new Set(docs.map(d => d.dept))]`）。"""
    try:
        if settings.store_label == "mysql":
            from app.db import kb_repo

            found = kb_repo.list_depts()
        else:
            found = list(dict.fromkeys(d.get("dept") for d in get_index().docs if d.get("dept")))
    except Exception:  # noqa: BLE001 - 元数据接口不应因数据源故障而失败
        found = []
    return found or FALLBACK_DEPTS


@router.get("/meta", response_model=ApiResult, summary="元数据字典")
def get_meta() -> ApiResult:
    return ApiResult(
        ok=True,
        data={
            "depts": _depts(),
            "ktypes": KTYPES,
            "statuses": STATUSES,
            "roles": ROLES,
            "authRoles": AUTH_ROLES,
        },
    )
