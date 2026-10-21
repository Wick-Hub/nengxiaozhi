"""知识点管理接口（任务 2.8 / M-K-10）。

术语：**知识点 = 检索切片**（``kb_chunk``），对外编号 ``D001-C003``。

端点一览（前缀 ``/api/knowledge``）：

=====================================================  ==============================
``GET    /points``                                     列表（部门/类型/分类/文档/块类型/关键词 + 分页）
``GET    /points/{chunkId}``                           详情（含全文）
``POST   /points``                                     新建内容切片
``PATCH  /points/{chunkId}``                           编辑（正文变更 → 单点重算向量）
``DELETE /points/{chunkId}``                           删除无效切片
``POST   /points/{chunkId}/append-text``               再加工 · 添加文本内容
``POST   /points/{chunkId}/remove-content``            再加工 · 删除内容
``POST   /points/{chunkId}/insert-image``              再加工 · 插入图片
``POST   /points/export``                              批量导出（csv / md / json）
=====================================================  ==============================

鉴权口径（沿用 2.6）：进入本路由需「管理员 / 部门联络员」；类目级操作位
按 :func:`kb_permission.can_operate` 二次判定——

* 浏览 / 详情 → ``canView``
* 新建 / 编辑 / 再加工 → ``canUpload``（维护权）
* 删除 → ``canDelete``
* 导出 → ``canDownload``
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response

from app.config import settings
from app.db import kb_repo
from app.models.schemas import (
    ApiResult,
    KbPointAppendRequest,
    KbPointCreateRequest,
    KbPointExportRequest,
    KbPointImageRequest,
    KbPointRemoveRequest,
    KbPointUpdateRequest,
)
from app.security import oplog
from app.security.deps import (
    Principal,
    get_current_user,
    require_doc_operator,
    require_permission,
)
from app.security.ratelimit import rate_limit
from app.services import kb_knowledge, kb_permission

router = APIRouter(
    prefix="/knowledge",
    dependencies=[
        Depends(require_doc_operator),
        Depends(require_permission),
        Depends(rate_limit("admin")),
    ],
)


def _require_perm(principal: Principal, dept_id: int, ktype: str, action: str) -> None:
    """类目级授权校验：管理员 / 未开启鉴权直接通过，否则不足即 403。"""
    if principal.is_admin or not settings.auth_enabled:
        return
    if not kb_permission.can_operate(principal, dept_id, ktype, action):
        raise HTTPException(
            status_code=403,
            detail=(
                f"未授权：当前用户不能在此类目下执行该操作"
                f"（{action}，部门={dept_id}，类型={ktype}）"
            ),
        )


def _guard_point(principal: Principal, chunk_id: str, action: str) -> dict:
    """按知识点所属文档的（部门 × 类型）做授权校验，返回授权域。

    ⚠ JSON 只读模式下**不查库**：``point_scope`` 是 MySQL 专用查询，
    在只读模式里调用会直接连不上数据库而 500。只读模式下没有可写操作，
    也无需类目授权，直接放行让服务层给出「需要 MySQL」的可读提示。
    """
    if settings.store_label != "mysql":
        return {}
    scope = kb_repo.point_scope(chunk_id)
    if scope is None:
        raise HTTPException(status_code=404, detail=f"知识点不存在：{chunk_id}")
    _require_perm(principal, scope["deptId"], scope["ktype"], action)
    return scope


def _guard_doc(principal: Principal, raw_doc_id) -> dict:
    """按目标文档的（部门 × 类型）做授权校验，返回文档详情。

    同样跳过只读模式——写入会被服务层以 400 挡下，不必先查库。
    """
    if settings.store_label != "mysql":
        return {}
    doc_id = kb_repo.parse_public_doc_id(raw_doc_id)
    if doc_id is None:
        raise HTTPException(status_code=400, detail=f"文档编号非法：{raw_doc_id}")
    doc = kb_repo.get_doc(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"文档不存在：{raw_doc_id}")
    dept_id, err = kb_permission.resolve_dept_id(doc.get("dept"))
    if err:
        raise HTTPException(status_code=400, detail=err)
    _require_perm(principal, dept_id or 0, kb_repo.ktype_enum(doc.get("ktype")), "canUpload")
    return doc


def _wrap(res: dict) -> ApiResult:
    """服务层结果 → 统一响应封装（``error`` 走 400，便于前端直接提示）。"""
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error") or "操作失败")
    return ApiResult(ok=True, data=res.get("data"))


def _log(
    request: Request,
    action: str,
    chunk_id: str,
    detail: dict,
    principal: Principal,
) -> None:
    """写操作日志（``detail`` 是结构化 dict，与既有 5 个动作同口径）。"""
    oplog.log_from_request(
        request,
        action,
        "kb_chunk",
        chunk_id,
        user_id=principal.user_id,
        detail=detail,
    )


# ------------------------------------------------------------------ 列表 / 详情
@router.get("/points", response_model=ApiResult, summary="知识点列表（M-K-10）")
def list_points(
    dept: str | None = Query(default=None, description="按部门名称过滤"),
    ktype: str | None = Query(default=None, description="按知识类型过滤（中文名）"),
    category_id: int | None = Query(default=None, alias="categoryId", ge=0,
                                    description="按知识分类过滤"),
    doc_id: int | None = Query(default=None, alias="docId", ge=1,
                               description="按文档自增 id 过滤"),
    block_type: str | None = Query(default=None, alias="blockType",
                                   description="按块类型过滤（para/table/faq/image）"),
    q: str | None = Query(default=None, description="关键词检索（空格分隔，AND 语义）"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=kb_knowledge.MAX_PAGE_SIZE),
) -> ApiResult:
    """知识点分页列表；``data`` 为 ``{items,total,page,size,editable,filters}``。"""
    return _wrap(
        kb_knowledge.list_points(
            dept=dept,
            ktype=ktype,
            category_id=category_id,
            doc_id=doc_id,
            block_type=block_type,
            q=q,
            page=page,
            size=size,
        )
    )


@router.get("/points/{chunk_id}", response_model=ApiResult, summary="知识点详情")
def get_point(
    chunk_id: str,
    principal: Principal = Depends(get_current_user),
) -> ApiResult:
    _guard_point(principal, chunk_id, "canView")
    return _wrap(kb_knowledge.get_point(chunk_id))


# ------------------------------------------------------------------ 新建 / 编辑 / 删除
@router.post("/points", response_model=ApiResult, summary="新建内容切片")
def create_point(
    payload: KbPointCreateRequest,
    request: Request,
    principal: Principal = Depends(get_current_user),
) -> ApiResult:
    doc = _guard_doc(principal, payload.doc_id)
    result = _wrap(kb_knowledge.create_point(payload.model_dump(by_alias=True)))
    point = (result.data or {}).get("point") or {}
    _log(
        request,
        oplog.ACTION_POINT_CREATE,
        point.get("chunkId") or "",
        {"docId": doc.get("docId"), "seqNo": point.get("seqNo")},
        principal,
    )
    return result


@router.patch("/points/{chunk_id}", response_model=ApiResult, summary="编辑知识点")
def update_point(
    chunk_id: str,
    payload: KbPointUpdateRequest,
    request: Request,
    principal: Principal = Depends(get_current_user),
) -> ApiResult:
    _guard_point(principal, chunk_id, "canUpload")
    # exclude_unset：未传的键不覆盖；imageKeys: [] 仍算「显式清空」
    res = kb_knowledge.update_point(
        chunk_id, payload.model_dump(by_alias=True, exclude_unset=True)
    )
    result = _wrap(res)
    _log(
        request,
        oplog.ACTION_POINT_UPDATE,
        chunk_id,
        {"changed": (result.data or {}).get("changed")},
        principal,
    )
    return result


@router.delete("/points/{chunk_id}", response_model=ApiResult, summary="删除无效切片")
def delete_point(
    chunk_id: str,
    request: Request,
    principal: Principal = Depends(get_current_user),
) -> ApiResult:
    _guard_point(principal, chunk_id, "canDelete")
    result = _wrap(kb_knowledge.delete_point(chunk_id))
    _log(request, oplog.ACTION_POINT_DELETE, chunk_id, {"reason": "无效切片"}, principal)
    return result


# ------------------------------------------------------------------ 再加工
@router.post("/points/{chunk_id}/append-text", response_model=ApiResult,
             summary="再加工 · 添加文本内容")
def append_text(
    chunk_id: str,
    payload: KbPointAppendRequest,
    request: Request,
    principal: Principal = Depends(get_current_user),
) -> ApiResult:
    _guard_point(principal, chunk_id, "canUpload")
    result = _wrap(kb_knowledge.append_text(chunk_id, payload.text, payload.position))
    _log(
        request,
        oplog.ACTION_POINT_UPDATE,
        chunk_id,
        {"op": "append", "position": payload.position, "chars": len(payload.text)},
        principal,
    )
    return result


@router.post("/points/{chunk_id}/remove-content", response_model=ApiResult,
             summary="再加工 · 删除内容")
def remove_content(
    chunk_id: str,
    payload: KbPointRemoveRequest,
    request: Request,
    principal: Principal = Depends(get_current_user),
) -> ApiResult:
    _guard_point(principal, chunk_id, "canUpload")
    result = _wrap(
        kb_knowledge.remove_content(
            chunk_id, text=payload.text, start=payload.start, end=payload.end
        )
    )
    _log(
        request,
        oplog.ACTION_POINT_UPDATE,
        chunk_id,
        {
            "op": "remove",
            "removed": (result.data or {}).get("removed"),
            "range": (result.data or {}).get("range"),
        },
        principal,
    )
    return result


@router.post("/points/{chunk_id}/insert-image", response_model=ApiResult,
             summary="再加工 · 插入图片")
def insert_image(
    chunk_id: str,
    payload: KbPointImageRequest,
    request: Request,
    principal: Principal = Depends(get_current_user),
) -> ApiResult:
    _guard_point(principal, chunk_id, "canUpload")
    result = _wrap(kb_knowledge.insert_image(chunk_id, payload.key))
    _log(
        request,
        oplog.ACTION_POINT_UPDATE,
        chunk_id,
        {"op": "insertImage", "key": payload.key, "added": (result.data or {}).get("added")},
        principal,
    )
    return result


# ------------------------------------------------------------------ 批量导出
@router.post("/export", summary="批量导出知识点（csv/md/json）")
def export_points(
    payload: KbPointExportRequest,
    request: Request,
    principal: Principal = Depends(get_current_user),
):
    """导出选中知识点为附件下载。

    逐个知识点做 ``canDownload`` 校验（联络员只能导出授权类目），
    只要有一条越权即整体 403——避免「部分成功」导致的数据外泄。
    """
    for chunk_id in payload.chunk_ids:
        _guard_point(principal, chunk_id, "canDownload")

    res = kb_knowledge.export_points(payload.chunk_ids, payload.format)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error") or "导出失败")
    data = res["data"]
    _log(
        request,
        oplog.ACTION_POINT_EXPORT,
        ",".join(payload.chunk_ids[:10]),
        {"count": len(payload.chunk_ids), "format": payload.format},
        principal,
    )
    filename = data["filename"]
    return Response(
        content=data["content"],
        media_type=data["mediaType"],
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}"
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )
