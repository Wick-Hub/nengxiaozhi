"""知识库分类管理服务（任务 2.5 / M-K-01·02）。

两种存储模式的分工：

* ``KB_STORE=mysql`` —— 全功能。分类是 ``kb_category`` 表上的真实记录
  （部门 × 知识类型），支持增删改，文档通过 ``kb_doc.category_id`` 挂分类，
  删除分类前校验「有文档/有子分类」则拒绝。
* ``KB_STORE=json`` —— **只读推导**。索引里没有分类实体，按 (部门 × 知识类型)
  从 ``get_index().docs`` 组合出分类，``editable=False``，写操作返回 400 + 提示。

对外分类的稳定键是 ``deptId × ktype``（ktype 为枚举）。``categoryId`` 在 JSON
模式下不存在（用 ``deptId``+``ktype`` 拼出），因此前端**不得**把 ``categoryId``
当成跨模式唯一主键依赖——JSON 下 ``categoryId`` 为 ``None``。
"""

from __future__ import annotations

import logging

from app.config import settings
from app.core.index_store import get_index
from app.db import kb_repo
from app.db.session import DatabaseUnavailable, db_session, db_transaction

logger = logging.getLogger(__name__)


def _ok(data) -> dict:
    return {"ok": True, "data": data, "error": None}


def _fail(message: str, status: int = 400) -> dict:
    """失败结果。``status`` 供接口层映射 HTTP 状态码（400 参数 / 404 不存在 / 409 冲突）。"""
    return {"ok": False, "data": None, "error": message, "status": status}


def _mysql_mode() -> bool:
    return settings.store_label == "mysql"


def _must_mysql() -> dict | None:
    """非 MySQL 模式返回一个失败结果；MySQL 模式返回 ``None``。"""
    if _mysql_mode():
        return None
    return _fail(
        "知识库分类管理需要 MySQL 存储：请在 .env 中设置 KB_STORE=mysql 并确保数据库已启动"
        "（docker compose -f ../database/docker-compose.yml up -d）",
        status=400,
    )


# ------------------------------------------------------------------ JSON 模式（只读推导）
def _json_category_items() -> list[dict]:
    """从本地索引推导出 (部门 × 知识类型) 组合，docCount 为该组合下的文档数。"""
    idx = get_index()
    if idx.error:
        raise RuntimeError(idx.error)
    groups: dict[tuple, dict] = {}
    for d in idx.docs:
        dept = d.get("dept") or "未分类"
        ktype = d.get("ktype") or "其他"
        key = (dept, ktype)
        g = groups.setdefault(
            key,
            {
                "categoryId": None,  # JSON 模式无真实主键，前端不依赖
                "deptId": None,
                "dept": dept,
                "ktype": ktype,
                "categoryName": ktype,
                "parentId": 0,
                "sortNo": 0,
                "docCount": 0,
                "editable": False,
            },
        )
        g["docCount"] += 1
    items = list(groups.values())
    items.sort(key=lambda x: (x["dept"], x["ktype"]))
    return items


# ------------------------------------------------------------------ 查询
def list_categories(dept: str | None = None, ktype: str | None = None) -> dict:
    """分类清单；默认返回平铺数组，前端按 ``dept`` 分组成树。

    ``dept`` / ``ktype`` 为**可选**过滤（名称，非 id）。JSON 模式每次调用现推导，
    因此不维护任何缓存。
    """
    try:
        if _mysql_mode():
            items = kb_repo.list_categories()
            editable = True
        else:
            items = _json_category_items()
            editable = False
    except DatabaseUnavailable as exc:
        return _fail(f"数据库不可用：{exc}", status=503)
    except RuntimeError as exc:
        return _fail(str(exc), status=400)

    if dept:
        items = [it for it in items if it["dept"] == dept]
    if ktype:
        items = [it for it in items if it["ktype"] == ktype]

    depts = list(dict.fromkeys(it["dept"] for it in items if it["dept"]))
    ktypes = list(dict.fromkeys(it["ktype"] for it in items if it["ktype"]))
    return _ok(
        {
            "items": items,
            "total": len(items),
            "docTotal": sum(it["docCount"] for it in items),
            "editable": editable,
            "depts": depts,
            "ktypes": ktypes,
        }
    )


# ------------------------------------------------------------------ MySQL 写操作
def _resolve_dept_id(payload: dict) -> tuple[int | None, str | None]:
    """把 payload 里的 deptId/dept 解析成 ``org_id``。

    返回 ``(dept_id, error)``：dept_id 为 ``None`` 时把 error 交给调用方返回 400。
    """
    dept_id = payload.get("deptId")
    if dept_id is not None:
        try:
            return int(dept_id), None
        except (TypeError, ValueError):
            return None, f"部门 id 非法：{dept_id}"
    dept_name = (payload.get("dept") or "").strip()
    if not dept_name:
        return None, "必须提供部门（deptId 或 dept 名称）"
    if _mysql_mode():
        with db_session() as conn:
            found = kb_repo.dept_id_by_name(conn, dept_name)
        if found is None:
            return None, f"部门不存在：{dept_name}（请先同步组织机构）"
        return found, None
    return None, None


def create_category(payload: dict) -> dict:
    """新增分类（M-K-01）。已存在同名（部门×类型）时返回 409。"""
    if (blocked := _must_mysql()) is not None:
        return blocked

    dept_id, err = _resolve_dept_id(payload)
    if err:
        return _fail(err, status=400)
    ktype_raw = (payload.get("ktype") or "").strip()
    if not ktype_raw:
        return _fail("必须提供知识类型（ktype）", status=400)

    category_name = (payload.get("categoryName") or "").strip() or None
    if category_name is not None and not category_name:
        return _fail("分类名称不能为空", status=400)
    parent_id = int(payload.get("parentId") or 0)
    sort_no = int(payload.get("sortNo") or 0)

    with db_transaction() as conn:
        if not kb_repo.dept_exists(conn, dept_id):
            return _fail(f"部门不存在：deptId={dept_id}", status=400)
        existing = kb_repo.find_category(conn, dept_id, ktype_raw)
        if existing is not None:
            return _fail(f"该部门下「{kb_repo.ktype_label(ktype_raw)}」分类已存在", status=409)
        category_id = kb_repo.create_category(
            conn,
            dept_id=dept_id,
            ktype_raw=ktype_raw,
            category_name=category_name,
            parent_id=parent_id,
            sort_no=sort_no,
        )
        item = kb_repo.get_category(conn, category_id)

    return _ok(item)


def update_category(category_id: int, payload: dict) -> dict:
    """重命名/排序分类（M-K-02）。不存在返回 404。"""
    if (blocked := _must_mysql()) is not None:
        return blocked

    name = (payload.get("categoryName") or "").strip()
    if not name:
        return _fail("分类名称不能为空", status=400)
    sort_no = int(payload.get("sortNo") or 0)

    with db_transaction() as conn:
        existing = kb_repo.get_category(conn, category_id)
        if existing is None:
            return _fail(f"分类不存在：categoryId={category_id}", status=404)
        kb_repo.update_category(conn, category_id, category_name=name, sort_no=sort_no)
        item = kb_repo.get_category(conn, category_id)
    return _ok(item)


def delete_category(category_id: int) -> dict:
    """删除分类（M-K-02）。有文档或在被引为父分类时返回 409。"""
    if (blocked := _must_mysql()) is not None:
        return blocked

    with db_transaction() as conn:
        existing = kb_repo.get_category(conn, category_id)
        if existing is None:
            return _fail(f"分类不存在：categoryId={category_id}", status=404)
        # get_category 已带回 docCount（内部 category_doc_count），直接复用判断
        doc_count = existing["docCount"]
        if doc_count > 0:
            return _fail(
                f"该分类下还有 {doc_count} 篇文档，请先移走或删除后再删分类", status=409
            )
        child_count = kb_repo.category_child_count(conn, category_id)
        if child_count > 0:
            return _fail(
                f"该分类下还有 {child_count} 个子分类，请先删除子分类", status=409
            )
        kb_repo.delete_category(conn, category_id)
    return _ok({"categoryId": category_id, "deleted": True})
