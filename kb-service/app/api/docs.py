"""文档管理接口：列表 / 上传 / 改状态 / 删除 / 切片明细 / 预览对比。

两种存储模式的分工（与原型一致）：

* ``KB_STORE=mysql`` —— 管理端全功能。列表读 ``kb_doc``（**含草稿/待审核/已下线**），
  上传/改状态/删除/同步直接落库。原型的管理端就是 MySQL-only 的。
* ``KB_STORE=json`` —— 只读模式。列表/切片由本地索引推导（索引里只有已发布知识），
  写操作返回 **400 + 可执行的提示**，而不是静默失败。

任务 1.11 追加的只读接口（两种模式都可用）：

* ``GET /api/docs/{id}`` —— 文档详情
* ``GET /api/docs/{id}/chunks?q=`` —— 切片明细（带关键词检索与命中区间）
* ``GET /api/docs/{id}/preview?q=`` —— 预览页一次性数据（原文 + 切片 + 检索）
* ``GET /api/docs/{id}/text`` —— 提取后的原文纯文本
* ``GET /api/docs/{id}/raw`` —— 原件字节流（inline 预览 / attachment 下载）

任务 1.10 追加的原图接口（图片型文档与内嵌图片）：

* ``GET /api/docs/{id}/images`` —— 该文档相关图片清单（原件本身 + 内嵌图）
* ``GET /api/docs/{id}/image?key=`` —— 单张原图字节流；``key`` 必须属于该文档
"""

from __future__ import annotations

import base64
import binascii
import posixpath
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse

from app.config import settings
from app.core.index_store import clear_index, get_index
from app.db import kb_repo
from app.db.session import DatabaseUnavailable
from app.models.schemas import ApiResult, DocBatchRequest, DocStatusRequest, DocUploadRequest
from app.security import oplog, permissions
from app.security.deps import (
    Principal,
    get_current_user,
    require_admin,
    require_doc_operator,
    require_permission,
)
from app.security.ratelimit import rate_limit
from app.services import doc_preview, kb_admin, kb_permission

# 知识库接口：鉴权（任务 2.6）+ 限流（任务 1.12）。
# AUTH_ENABLED=true 时管理员 / 部门联络员可进入；联络员仅能在授权类目
# 下操作（操作位在接口内部按 kb_permission.can_operate 细判，未授权 403）。
# 默认关闭以便开发/演示。
router = APIRouter(
    prefix="/docs",
    dependencies=[
        Depends(require_doc_operator),
        Depends(require_permission),
        Depends(rate_limit("admin")),
    ],
)

STATUS_NAME = {0: "草稿", 1: "待审核", 2: "已发布", 3: "已下线"}
# JSON 索引里的中文状态 → 数值状态码（管理页按数值筛选/配色）
STATUS_CODE = {"草稿": 0, "待审核": 1, "已审核": 2, "已发布": 2, "已下线": 3}

MYSQL_REQUIRED = (
    "该操作需要 MySQL 存储：请在 .env 中设置 KB_STORE=mysql 并确保数据库已启动"
    "（docker compose -f ../database/docker-compose.yml up -d）"
)


def _mysql_mode() -> bool:
    return settings.store_label == "mysql"


def _to_item(d: dict) -> dict:
    """JSON 索引文档 → 管理页字段（admin.js 依赖 fileKey/fileFormat/status/statusName）。"""
    file = d.get("file") or ""
    name = posixpath.basename(file.replace("\\", "/")) if file else ""
    fmt = Path(name).suffix.lstrip(".").lower() if name else None
    raw_status = d.get("status")
    if isinstance(raw_status, int):
        code = raw_status
    else:
        code = STATUS_CODE.get(str(raw_status), 2)
    return {
        "docId": str(d.get("docId", "")),
        "file": file,
        "fileKey": name,
        "fileFormat": fmt,
        "fileSize": d.get("fileSize"),
        "title": d.get("title") or name,
        "dept": d.get("dept"),
        "ktype": d.get("ktype"),
        "status": code,
        "statusName": STATUS_NAME.get(code, str(raw_status or "")),
        "updated": d.get("updated"),
        "updatedAt": d.get("updated"),
        "source": d.get("source"),
        "chunkCount": d.get("chunkCount", 0),
    }


def _require_mysql() -> None:
    if not _mysql_mode():
        raise HTTPException(status_code=400, detail=MYSQL_REQUIRED)


def _parse_doc_id(doc_id: str) -> int:
    parsed = kb_repo.parse_public_doc_id(doc_id)
    if parsed is None:
        raise HTTPException(status_code=400, detail=f"文档编号非法：{doc_id}")
    return parsed


def _require_perm(principal: Principal, dept_id: int, ktype: str, action: str) -> None:
    """任务 2.6：授权类目校验。管理员 / 未开启鉴权时直接通过；
    部门联络员需在授权内，否则 403。供写/删/预览/下载接口调用。"""
    if principal.is_admin or not settings.auth_enabled:
        return
    if not kb_permission.can_operate(principal, dept_id, ktype, action):
        raise HTTPException(
            status_code=403,
            detail=f"未授权：当前用户不能在此类目下执行该操作（{action}，部门={dept_id}，类型={ktype}）",
        )


# ------------------------------------------------------------------ 列表
@router.get("", response_model=ApiResult, summary="文档列表")
def list_docs(
    status: int | None = Query(default=None, ge=0, le=3, description="按状态过滤"),
    dept: str | None = Query(default=None, description="按部门名称过滤"),
    ktype: str | None = Query(default=None, description="按知识类型过滤（中文名）"),
    category_id: int | None = Query(default=None, alias="categoryId", ge=0,
                                    description="按分类过滤"),
) -> ApiResult:
    """文档列表。``data`` **保持为数组**（向后兼容既有前端与测试）。

    筛选在服务层/SQL 层下推；「分类/类型下拉」数据由
    ``GET /api/admin/kb-folders``（prefix + depts/ktypes）与
    ``GET /api/admin/kb-categories`` 提供，不在本接口重复下发。
    """
    if _mysql_mode():
        try:
            docs = kb_admin.list_docs(
                status=status, ktype=ktype, category_id=category_id, dept=dept
            )
        except DatabaseUnavailable as exc:
            return ApiResult(ok=False, data=[], error=f"数据库不可用：{exc}")
    else:
        idx = get_index()
        if idx.error:
            return ApiResult(ok=False, data=[], error=idx.error)
        docs = [_to_item(d) for d in idx.docs]
        if dept:
            docs = [d for d in docs if d.get("dept") == dept]
        if ktype:
            docs = [d for d in docs if d.get("ktype") == ktype]
        if status is not None:
            docs = [d for d in docs if str(d.get("status")) == str(status)]
    return ApiResult(ok=True, data=docs)


# ------------------------------------------------------------------ 上传
@router.post("", response_model=ApiResult, summary="上传入库")
def upload_doc(
    payload: DocUploadRequest,
    request: Request,
    principal: Principal = Depends(get_current_user),
) -> ApiResult:
    _require_mysql()

    filename = (payload.filename or "").strip()
    dept = (payload.dept or "").strip()
    ktype = (payload.ktype or "").strip()
    title = (payload.title or "").strip() or None
    raw = payload.raw_base64

    if not filename:
        raise HTTPException(status_code=400, detail="缺少文件名")
    if not dept:
        raise HTTPException(status_code=400, detail="缺少部门")
    if not ktype:
        raise HTTPException(status_code=400, detail="缺少知识类型")
    if not raw:
        raise HTTPException(status_code=400, detail="缺少文件内容")
    if not 0 <= payload.status <= 3:
        raise HTTPException(status_code=400, detail="状态非法（0草稿/1待审核/2已发布/3已下线）")

    # 任务 2.6：部门联络员只能向「授权类目」上传
    dept_id, err = kb_permission.resolve_dept_id(dept)
    if err:
        raise HTTPException(status_code=400, detail=err)
    ktype_enum = kb_repo.ktype_enum(ktype)
    _require_perm(principal, dept_id or 0, ktype_enum, "canUpload")

    try:
        file_buf = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"文件内容 base64 解码失败：{exc}") from exc
    if not file_buf:
        raise HTTPException(status_code=400, detail="文件内容为空")

    result = kb_admin.ingest_uploaded(
        file_buf=file_buf,
        filename=filename,
        dept=dept,
        ktype=ktype,
        status=payload.status,
        visible_roles=payload.visible_roles or ["全部"],
        title=title,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "入库失败")

    clear_index()  # 知识库变更，索引缓存失效
    oplog.log_from_request(
        request,
        oplog.ACTION_DOC_UPLOAD,
        "kb_doc",
        result.get("docId", ""),
        detail={
            "filename": filename,
            "dept": dept,
            "ktype": ktype,
            "status": payload.status,
        },
    )
    return ApiResult(ok=True, data=result)


# ------------------------------------------------------------------ 状态流转
@router.patch("/{doc_id}", response_model=ApiResult, summary="修改状态 0/1/2/3")
def update_status(
    doc_id: str,
    payload: DocStatusRequest,
    request: Request,
    principal: Principal = Depends(get_current_user),
) -> ApiResult:
    _require_mysql()
    row_id = _parse_doc_id(doc_id)

    # 任务 2.6：改状态属文档管理，纳入 canDelete 授权位
    doc = kb_repo.get_doc(row_id)
    if doc is not None:
        dept_id, err = kb_permission.resolve_dept_id(doc.get("dept"))
        if err:
            raise HTTPException(status_code=400, detail=err)
        _require_perm(principal, dept_id or 0, kb_repo.ktype_enum(doc.get("ktype")), "canDelete")

    result = kb_admin.update_doc_status(row_id, payload.status)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error") or "文档不存在")

    clear_index()
    oplog.log_from_request(
        request,
        oplog.ACTION_DOC_STATUS,
        "kb_doc",
        row_id,
        detail={"status": payload.status},
    )
    return ApiResult(ok=True, data=result)


# ------------------------------------------------------------------ 批量操作（任务 2.7 / M-K-09）
@router.post("/batch", response_model=ApiResult, summary="文档批量操作（M-K-09）")
def batch_docs(
    payload: DocBatchRequest,
    request: Request,
    principal: Principal = Depends(get_current_user),
) -> ApiResult:
    """批量审核/删除/移动。

    ``action``：
    * ``publish`` —— 审核通过，置为 2（已发布），立即可检索；
    * ``reject`` —— 驳回，置为 0（草稿），不再可检索；
    * ``delete`` —— 批量删除（含切片/向量/引用）；
    * ``move`` —— 批量移动分类（需 ``categoryId``）。

    `docIds` 接受**对外编号**（``D001``）或自增 ``doc_id``，接口层统一解析。
    批量状态下统计实际命中数，不存在的 doc_id 忽略。
    """
    _require_mysql()
    if not payload.doc_ids:
        raise HTTPException(status_code=400, detail="docIds 不能为空")
    doc_ids = [_parse_doc_id(str(d)) for d in payload.doc_ids]

    if payload.action in ("publish", "reject"):
        status = 2 if payload.action == "publish" else 0
        # 任务 2.6：部门联络员只能操作授权类目
        for row_id in doc_ids:
            doc = kb_repo.get_doc(row_id)
            if doc is not None:
                dept_id, err = kb_permission.resolve_dept_id(doc.get("dept"))
                if err:
                    raise HTTPException(status_code=400, detail=err)
                _require_perm(principal, dept_id or 0, kb_repo.ktype_enum(doc.get("ktype")),
                              "canDelete")
        result = kb_repo.batch_update_status(doc_ids, status)
        oplog.log_from_request(
            request,
            oplog.ACTION_DOC_STATUS,
            "kb_doc",
            ", ".join(str(d) for d in doc_ids),
            detail={"action": payload.action, "status": status, "updated": result["updated"]},
        )
    elif payload.action == "delete":
        # 路由级权限只按 docs.audit 放行批量端点，删除需单独校验 docs.delete
        if settings.auth_enabled and not permissions.has(principal, "docs.delete"):
            raise HTTPException(status_code=403, detail="当前角色无「删除文档」权限（docs.delete）")
        for row_id in doc_ids:
            doc = kb_repo.get_doc(row_id)
            if doc is not None:
                dept_id, err = kb_permission.resolve_dept_id(doc.get("dept"))
                if err:
                    raise HTTPException(status_code=400, detail=err)
                _require_perm(principal, dept_id or 0, kb_repo.ktype_enum(doc.get("ktype")),
                              "canDelete")
        result = kb_repo.batch_delete_docs(doc_ids)
        oplog.log_from_request(
            request,
            oplog.ACTION_DOC_DELETE,
            "kb_doc",
            ", ".join(str(d) for d in doc_ids),
            detail={"action": "delete", "deleted": result["deleted"]},
        )
    elif payload.action == "move":
        # 任务 2.6：移动同样改变文档归属，按源文档做一次授权校验
        # （与 publish/reject/delete 同口径，避免联络员把文档挪出授权类目）
        for row_id in doc_ids:
            doc = kb_repo.get_doc(row_id)
            if doc is not None:
                dept_id, err = kb_permission.resolve_dept_id(doc.get("dept"))
                if err:
                    raise HTTPException(status_code=400, detail=err)
                _require_perm(principal, dept_id or 0, kb_repo.ktype_enum(doc.get("ktype")),
                              "canDelete")
        result = kb_repo.batch_move_category(doc_ids, payload.category_id)
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error") or "移动失败")
        oplog.log_from_request(
            request,
            oplog.ACTION_DOC_STATUS,
            "kb_doc",
            ", ".join(str(d) for d in doc_ids),
            detail={"action": "move", "categoryId": payload.category_id,
                    "updated": result["updated"]},
        )
    else:  # pragma: no cover - Literal 已收敛，防御性兜底
        raise HTTPException(status_code=400, detail=f"不支持的操作：{payload.action}")

    clear_index()
    return ApiResult(ok=True, data=result)


# ------------------------------------------------------------------ 删除
@router.delete("/{doc_id}", response_model=ApiResult, summary="删除文档")
def delete_doc(
    doc_id: str,
    request: Request,
    principal: Principal = Depends(get_current_user),
) -> ApiResult:
    _require_mysql()
    row_id = _parse_doc_id(doc_id)

    # 任务 2.6：只能删除「授权类目」下的文档
    doc = kb_repo.get_doc(row_id)
    dept_id, err = kb_permission.resolve_dept_id(doc.get("dept") if doc else None)
    if err:
        raise HTTPException(status_code=400, detail=err)
    if doc:
        ktype_enum = kb_repo.ktype_enum(doc.get("ktype"))
        _require_perm(principal, dept_id or 0, ktype_enum, "canDelete")

    result = kb_admin.delete_doc(row_id)
    clear_index()
    oplog.log_from_request(request, oplog.ACTION_DOC_DELETE, "kb_doc", row_id)
    return ApiResult(ok=True, data=result)


# ------------------------------------------------------------------ 预览公共逻辑
def _locate_doc(doc_id: str) -> dict | None:
    """按对外编号取文档条目（MySQL 读 ``kb_doc``，JSON 索引读 ``docs``）。

    :raises DatabaseUnavailable: MySQL 模式下数据库不可用
    :raises HTTPException: 编号非法（400）
    """
    if _mysql_mode():
        return kb_repo.get_doc(_parse_doc_id(doc_id))
    idx = get_index()
    if idx.error:
        return None
    for d in idx.docs:
        if str(d.get("docId")) == str(doc_id):
            return _to_item(d)
    return None


def _locate_chunks(doc_id: str) -> list[dict]:
    """取文档的全部切片（含未发布文档；JSON 索引里只有已发布知识）。"""
    if _mysql_mode():
        return kb_repo.list_chunks(_parse_doc_id(doc_id))
    idx = get_index()
    return [c.to_dict() for c in idx.chunks if str(c.doc_id) == str(doc_id)]


def _db_error(exc: Exception) -> ApiResult:
    return ApiResult(ok=False, data=None, error=f"数据库不可用：{exc}")


# ------------------------------------------------------------------ 切片明细
@router.get("/{doc_id}/chunks", response_model=ApiResult, summary="文档切片明细")
def list_chunks(doc_id: str, q: str | None = Query(default=None, description="关键词检索（空格分隔多个）")) -> ApiResult:
    """切片明细。

    * 不带 ``q`` —— 返回**列表**（与 1.14 的契约一致，向后兼容）；
    * 带 ``q`` —— 仍返回**列表**，但只保留命中的切片，并给每条补上
      ``matches`` / ``titleMatches`` 命中区间（``[start, end]``，供前端高亮）。
    """
    if _mysql_mode():
        _parse_doc_id(doc_id)  # 编号非法时直接 400
    try:
        chunks = _locate_chunks(doc_id)
    except DatabaseUnavailable as exc:
        return _db_error(exc)

    if q is None or not q.strip():
        return ApiResult(ok=True, data=chunks)
    return ApiResult(ok=True, data=doc_preview.search_chunks(chunks, q)["items"])


# ------------------------------------------------------------------ 文档详情
@router.get("/{doc_id}/preview", response_model=ApiResult, summary="预览数据（原文 + 切片 + 检索）")
def preview_doc(
    doc_id: str,
    q: str | None = Query(default=None, description="关键词检索（空格分隔多个，AND 语义）"),
    principal: Principal = Depends(get_current_user),
) -> ApiResult:
    """预览页一次性所需数据：原文纯文本、原件元信息、切片列表与检索结果。

    原文与切片**同源于解析层**，因此左栏（原文）与右栏（切片）可直接对照。
    """
    try:
        doc = _locate_doc(doc_id)
        chunks = _locate_chunks(doc_id)
    except DatabaseUnavailable as exc:
        return _db_error(exc)

    if doc is None:
        raise HTTPException(status_code=404, detail=f"文档不存在：{doc_id}")

    # 任务 2.6：预览 = 查看，需授权类目内的 canView
    dept_id, err = kb_permission.resolve_dept_id(doc.get("dept"))
    if err:
        raise HTTPException(status_code=400, detail=err)
    _require_perm(principal, dept_id or 0, kb_repo.ktype_enum(doc.get("ktype")), "canView")
    return ApiResult(ok=True, data=doc_preview.build_preview(doc, chunks, q))


@router.get("/{doc_id}/text", response_model=ApiResult, summary="提取原文纯文本")
def get_text(doc_id: str) -> ApiResult:
    """提取后的原文纯文本（文本类格式原样解码，二进制格式经解析层还原）。"""
    try:
        doc = _locate_doc(doc_id)
    except DatabaseUnavailable as exc:
        return _db_error(exc)

    if doc is None:
        raise HTTPException(status_code=404, detail=f"文档不存在：{doc_id}")

    file_key = doc_preview.original_key(doc)
    title = str(doc.get("title") or "").strip() or None
    result = doc_preview.extract_text(file_key, title=title)
    return ApiResult(
        ok=result["ok"],
        data={
            "docId": doc_id,
            "fileKey": file_key,
            "text": result["text"],
            "chars": result["chars"],
            "lines": result["lines"],
            "format": result["format"],
            "source": result["source"],
            "cached": result["cached"],
        },
        error=result["error"],
    )


# ------------------------------------------------------------------ 原图（任务 1.10）
@router.get("/{doc_id}/images", response_model=ApiResult, summary="文档图片清单")
def list_images(doc_id: str) -> ApiResult:
    """该文档相关图片清单。

    * **原件本身** —— 图片型文档（``.png/.jpg/…``）的 ``role=original``；
    * **内嵌图片** —— docx/pptx 抽取出的原图（``role=embedded``），并带
      ``chunkSeqs`` 标出引用它的切片序号，便于「图 ↔ 切片」对照。
    """
    try:
        doc = _locate_doc(doc_id)
        chunks = _locate_chunks(doc_id)
    except DatabaseUnavailable as exc:
        return _db_error(exc)

    if doc is None:
        raise HTTPException(status_code=404, detail=f"文档不存在：{doc_id}")

    images = doc_preview.collect_images(doc, chunks)
    return ApiResult(
        ok=True,
        data={
            "docId": doc_id,
            "total": len(images),
            "isImageDoc": doc_preview.is_image_key(doc_preview.original_key(doc)),
            "images": images,
        },
    )


@router.get("/{doc_id}/image", summary="单张原图字节流")
def get_image(
    doc_id: str,
    key: str = Query(..., description="图片对象键（取自 /images 的 key 字段）"),
) -> FileResponse:
    """原图字节流。

    ``key`` **必须属于该文档**（原件本身或该文档切片的 ``imageKeys``），
    否则 404 —— 避免用任意路径把服务器上其它文件当图片读出来。
    图片按内容哈希寻址，内容不可变，故允许客户端长缓存。
    """
    try:
        doc = _locate_doc(doc_id)
        chunks = _locate_chunks(doc_id)
    except DatabaseUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"数据库不可用：{exc}") from exc

    if doc is None:
        raise HTTPException(status_code=404, detail=f"文档不存在：{doc_id}")

    try:
        path = doc_preview.resolve_doc_image(doc, chunks, key)
    except doc_preview.PreviewError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

    info = doc_preview.describe_image(key)
    response = FileResponse(path, media_type=info["mime"] or "application/octet-stream")
    response.headers["X-Content-Type-Options"] = "nosniff"
    # 内容寻址（文件名即内容哈希前 16 位）→ 同一 key 内容永不变，可放心长缓存
    response.headers["Cache-Control"] = "private, max-age=86400, immutable"
    return response


# ------------------------------------------------------------------ 原文预览/下载
@router.get("/{doc_id}/raw", summary="原文预览/下载（原件字节流）")
def get_raw(
    doc_id: str,
    request: Request,
    download: bool = Query(default=False, description="true 强制下载（attachment）"),
    principal: Principal = Depends(get_current_user),
) -> FileResponse:
    """原件字节流。

    * ``Content-Disposition`` 默认 ``inline``（浏览器可直接预览 PDF/图片/文本），
      命中「不可内联」的格式或显式 ``?download=1`` 时改为 ``attachment``；
    * 一律带 ``X-Content-Type-Options: nosniff``，避免浏览器按内容嗅探出可执行类型。
    """
    try:
        doc = _locate_doc(doc_id)
    except DatabaseUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"数据库不可用：{exc}") from exc

    if doc is None:
        raise HTTPException(status_code=404, detail=f"文档不存在：{doc_id}")

    # 任务 2.6：下载需授权类目内的 canDownload / canView
    dept_id, err = kb_permission.resolve_dept_id(doc.get("dept"))
    if err:
        raise HTTPException(status_code=400, detail=err)
    ktype_enum = kb_repo.ktype_enum(doc.get("ktype"))
    _require_perm(principal, dept_id or 0, ktype_enum, "canDownload")

    file_key = doc_preview.original_key(doc)
    info = doc_preview.describe_original(file_key)
    if not info["exists"]:
        raise HTTPException(status_code=404, detail=info["error"] or f"原件不存在：{file_key}")

    path = doc_preview.resolve_original(file_key)
    disposition = "inline" if (info["inline"] and not download) else "attachment"
    filename = info["fileName"] or "download"
    response = FileResponse(
        path,
        media_type=info["mime"],
        filename=filename,
        content_disposition_type=disposition,
    )
    # Starlette 的 filename 参数走 ASCII 回退；这里补 RFC 5987 编码，保证中文名不乱码
    response.headers["Content-Disposition"] = f"{disposition}; filename*=UTF-8''{quote(filename)}"
    response.headers["X-Content-Type-Options"] = "nosniff"

    oplog.log_from_request(
        request,
        oplog.ACTION_DOC_DOWNLOAD,
        "kb_doc",
        doc_id,
        detail={"fileKey": file_key, "disposition": disposition, "size": info["size"]},
    )
    return response


# ------------------------------------------------------------------ 文档详情（放最后，避免吞掉更具体的路径）
@router.get("/{doc_id}", response_model=ApiResult, summary="文档详情")
def get_doc_detail(doc_id: str) -> ApiResult:
    """单文档详情（含草稿/待审核/已下线），供预览页头部信息展示。"""
    try:
        doc = _locate_doc(doc_id)
    except DatabaseUnavailable as exc:
        return _db_error(exc)

    if doc is None:
        raise HTTPException(status_code=404, detail=f"文档不存在：{doc_id}")
    return ApiResult(ok=True, data=doc)
