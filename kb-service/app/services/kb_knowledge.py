"""知识点管理服务（任务 2.8 / M-K-10）。

需求要点（采购内容及要求 §「知识点管理」）：

* 对**完成向量化处理的知识点**按知识分类集约化管理；
* 检索 / 新增 / 编辑 / 删除；
* 精细化再加工：添加文本内容、插入图片、删除内容、删除无效切片、新建内容切片；
* 批量导出。

术语约定：本项目的「知识点」= 检索切片（``kb_chunk``）。切片编号对外为
``D001-C003``（文档号 + 序号），与原型一致。

存储模式分工（与 1.14 / 2.5 / 2.7 同一口径）：

* ``KB_STORE=mysql`` —— 全功能（CRUD + 再加工 + 导出）；
* ``KB_STORE=json`` —— **只读**，列表由本地索引推导（``editable=False``），
  写操作返回 400 + 可执行提示，而不是静默失败。

**向量一致性**：切片正文一变，旧向量立即失效。本模块的写路径统一走
:func:`_reembed`——重算成功置 ``vec_synced=1``；重算失败**不回滚业务改动**，
而是置 ``vec_synced=0`` 并在返回里给出 ``warning``。理由：丢内容比丢向量严重，
且 ``vec_synced`` 本身就是「待对账」的标记位，运维可据此重算。
"""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime

from app.config import settings
from app.core.embedder import get_embedder
from app.core.index_store import get_index
from app.db import kb_repo
from app.services import doc_preview

logger = logging.getLogger(__name__)

#: 块类型（``kb_chunk.block_type``）。``para`` 段落 / ``table`` 整表 / ``faq`` 问答对 / ``image`` 图片
BLOCK_TYPES: tuple[str, ...] = ("para", "table", "faq", "image")

#: 单条知识点正文上限。切片是「一段可检索内容」，误把整篇文档贴进来会
#: 让检索彻底失真——这里在写入口直接挡住，而不是等检索质量下降再排查。
MAX_CONTENT = 200_000

#: 列表分页上限
MAX_PAGE_SIZE = 200


# ------------------------------------------------------------------ 内部工具
def _mysql_mode() -> bool:
    return settings.store_label == "mysql"


def _ok(data) -> dict:
    return {"ok": True, "data": data, "error": None}


def _fail(error: str) -> dict:
    return {"ok": False, "data": None, "error": error}


def _must_mysql(action: str) -> dict | None:
    """非 MySQL 模式返回失败结果；MySQL 模式返回 ``None``。"""
    if _mysql_mode():
        return None
    return _fail(
        f"{action}需要 MySQL 存储：请在 .env 中设置 KB_STORE=mysql 并确保数据库已启动"
        "（docker compose -f ../database/docker-compose.yml up -d）"
    )


def embed_text(title_path: str, content: str) -> str:
    """切片向量化文本 —— 必须与 ``kb_repo.load_store`` 的 ``embedText`` 同口径。

    两边不一致会导致「同一内容在库内与内存索引里向量不同」，检索打分漂移，
    且这种偏差极难被发现。
    """
    return f"{title_path}\n{content}" if title_path else content


def _reembed(chunk_id: str, title_path: str, content: str) -> dict:
    """单点重算向量；失败只降级不抛错（返回 ``{synced, warning}``）。"""
    try:
        emb = get_embedder().encode_one(embed_text(title_path, content))
        kb_repo.save_point_vector(chunk_id, emb, settings.embed_model)
        return {"synced": True, "warning": None}
    except Exception as exc:  # noqa: BLE001 - 向量失败不应回滚内容改动
        logger.warning("知识点 %s 重算向量失败：%s", chunk_id, exc)
        kb_repo.drop_point_vector(chunk_id)
        return {"synced": False, "warning": f"正文已保存，但向量重算失败：{exc}"}


def _after_write(doc_id: int | None = None) -> dict:
    """写后收尾：回写 ``chunk_count`` 并让内存索引失效。

    索引是进程级缓存，不失效的话助手在重启前仍按旧切片作答——「改完没生效」
    是这类后台最常见的问题。
    """
    from app.core.index_store import clear_index

    count = kb_repo.recount_doc_chunks(doc_id) if doc_id else None
    clear_index()
    return {"chunkCount": count}


def _clip(text: str) -> str:
    return (text or "").strip()


def _check_content(content: str) -> str | None:
    """正文校验；返回错误消息或 ``None``。"""
    if not content.strip():
        return "正文不能为空"
    if len(content) > MAX_CONTENT:
        return f"正文过长（{len(content)} 字），单条知识点上限 {MAX_CONTENT} 字"
    return None


# ------------------------------------------------------------------ 列表 / 详情
def _json_points(
    *,
    dept: str | None = None,
    ktype: str | None = None,
    doc_id: int | None = None,
    block_type: str | None = None,
    q: str | None = None,
    page: int = 1,
    size: int = 20,
) -> dict:
    """JSON 模式：从本地索引推导（只读）。"""
    idx = get_index()
    if idx.error:
        return {"items": [], "total": 0, "page": page, "size": size, "editable": False}

    titles = {d.get("docId"): d.get("title") or "" for d in idx.docs}
    terms = [t for t in (q or "").split() if t]
    items: list[dict] = []
    # ``idx.chunks`` 是 ``Chunk`` 数据类（不是 dict），统一走 to_dict() 取字段
    for chunk in idx.chunks:
        c = chunk.to_dict()
        if dept and c.get("dept") != dept:
            continue
        if ktype and c.get("ktype") != ktype:
            continue
        if doc_id is not None and c.get("docId") != kb_repo.doc_public_id(doc_id):
            continue
        if block_type and c.get("blockType") != block_type:
            continue
        text = c.get("text") or ""
        title_path = c.get("titlePath") or ""
        haystack = f"{title_path}\n{text}"
        if terms and not all(t in haystack for t in terms):
            continue
        items.append(
            {
                "chunkId": c.get("chunkId"),
                "docId": c.get("docId"),
                "docTitle": titles.get(c.get("docId"), ""),
                "seqNo": c.get("seqNo"),
                "blockType": c.get("blockType"),
                "titlePath": title_path,
                "charCount": c.get("charCount") or len(text),
                "dept": c.get("dept"),
                "ktype": c.get("ktype"),
                "categoryId": None,
                "visibleRoles": c.get("visibleRoles") or ["全部"],
                "vecSynced": 1,
                "createdAt": None,
                "excerpt": text[: kb_repo.EXCERPT_LEN],
                **({"imageKeys": c["imageKeys"]} if c.get("imageKeys") else {}),
            }
        )
    total = len(items)
    start = max(0, (page - 1) * size)
    return {
        "items": items[start : start + size],
        "total": total,
        "page": page,
        "size": size,
        "editable": False,
    }


def list_points(
    *,
    dept: str | None = None,
    ktype: str | None = None,
    category_id: int | None = None,
    doc_id: int | None = None,
    block_type: str | None = None,
    q: str | None = None,
    page: int = 1,
    size: int = 20,
) -> dict:
    """知识点分页列表（含筛选下拉的可选值）。"""
    page = max(1, int(page or 1))
    size = max(1, min(MAX_PAGE_SIZE, int(size or 20)))

    if not _mysql_mode():
        data = _json_points(
            dept=dept, ktype=ktype, doc_id=doc_id, block_type=block_type,
            q=q, page=page, size=size,
        )
    else:
        data = kb_repo.list_points(
            dept_name=dept,
            ktype_raw=ktype,
            category_id=category_id,
            doc_id=doc_id,
            block_type=block_type,
            q=q,
            page=page,
            size=size,
        )
        data["editable"] = True

    data["filters"] = _filter_options()
    return _ok(data)


def _filter_options() -> dict:
    """筛选下拉可选值（失败不阻断列表——筛选项缺了仍可浏览）。"""
    opts = {"depts": [], "ktypes": [], "blockTypes": list(BLOCK_TYPES)}
    if not _mysql_mode():
        idx = get_index()
        opts["depts"] = sorted({d.get("dept") for d in idx.docs if d.get("dept")})
        opts["ktypes"] = sorted({d.get("ktype") for d in idx.docs if d.get("ktype")})
        opts["blockTypes"] = sorted({c.block_type for c in idx.chunks if c.block_type})
        return opts
    try:
        opts["depts"] = kb_repo.list_depts()
        types = kb_repo.point_block_types()
        if types:
            opts["blockTypes"] = types
        from app.db import models as M

        opts["ktypes"] = [kb_repo.ktype_label(v) for v in M.KTYPE_MAP.values()]
    except Exception as exc:  # noqa: BLE001 - 下拉值非关键路径
        logger.warning("知识点筛选可选值加载失败：%s", exc)
    return opts


def get_point(chunk_id: str) -> dict:
    """单条知识点（含全文）。"""
    if not _mysql_mode():
        idx = get_index()
        for chunk in idx.chunks:
            if chunk.chunk_id == chunk_id:
                c = chunk.to_dict()
                text = c.get("text") or ""
                titles = {d.get("docId"): d.get("title") or "" for d in idx.docs}
                return _ok(
                    {
                        "chunkId": c.get("chunkId"),
                        "docId": c.get("docId"),
                        "docTitle": titles.get(c.get("docId"), ""),
                        "seqNo": c.get("seqNo"),
                        "blockType": c.get("blockType"),
                        "titlePath": c.get("titlePath") or "",
                        "charCount": c.get("charCount") or len(text),
                        "dept": c.get("dept"),
                        "ktype": c.get("ktype"),
                        "categoryId": None,
                        "visibleRoles": c.get("visibleRoles") or ["全部"],
                        "vecSynced": 1,
                        "createdAt": None,
                        "content": text,
                        "editable": False,
                        **({"imageKeys": c["imageKeys"]} if c.get("imageKeys") else {}),
                    }
                )
        return _fail(f"知识点不存在：{chunk_id}")

    point = kb_repo.get_point(chunk_id)
    if point is None:
        return _fail(f"知识点不存在：{chunk_id}")
    point["editable"] = True
    return _ok(point)


# ------------------------------------------------------------------ 新建
def create_point(payload: dict) -> dict:
    """新建内容切片（序号取当前最大值 + 1）。"""
    blocked = _must_mysql("新建知识点")
    if blocked:
        return blocked

    raw_doc = payload.get("docId", payload.get("doc_id"))
    doc_id = kb_repo.parse_public_doc_id(raw_doc)
    if doc_id is None:
        return _fail(f"文档编号非法：{raw_doc}")

    content = payload.get("content") or ""
    err = _check_content(content)
    if err:
        return _fail(err)

    block_type = (payload.get("blockType") or payload.get("block_type") or "para").strip()
    if block_type not in BLOCK_TYPES:
        return _fail(f"块类型非法：{block_type}（可选 {'/'.join(BLOCK_TYPES)}）")

    doc = kb_repo.get_doc(doc_id)
    if doc is None:
        return _fail(f"文档不存在：{raw_doc}")

    title_path = (payload.get("titlePath") or payload.get("title_path") or "").strip()
    roles = payload.get("visibleRoles", payload.get("visible_roles"))
    visible_roles = kb_repo.roles_to_str(roles) if roles else "全部"
    image_keys = payload.get("imageKeys", payload.get("image_keys")) or None

    seq_no = kb_repo.max_chunk_seq(doc_id) + 1
    chunk_id = kb_repo.insert_point(
        doc_id=doc_id,
        seq_no=seq_no,
        block_type=block_type,
        title_path=title_path,
        content=content,
        char_count=len(content),
        visible_roles=visible_roles,
        image_keys=image_keys,
    )
    vec = _reembed(chunk_id, title_path, content)
    extra = _after_write(doc_id)

    point = kb_repo.get_point(chunk_id) or {}
    return _ok(
        {
            "point": point,
            "seqNo": seq_no,
            "vecSynced": vec["synced"],
            "warning": vec["warning"],
            **extra,
        }
    )


# ------------------------------------------------------------------ 编辑
def update_point(chunk_id: str, payload: dict) -> dict:
    """编辑知识点字段（正文变化时单点重算向量）。

    ``payload`` 已由接口层 ``model_dump(exclude_unset=True)`` 过滤——**未传的键
    不覆盖**。``imageKeys: []`` 是合法输入（清空图片），因此不能用「假值跳过」。
    """
    blocked = _must_mysql("编辑知识点")
    if blocked:
        return blocked

    scope = kb_repo.point_scope(chunk_id)
    if scope is None:
        return _fail(f"知识点不存在：{chunk_id}")
    current = kb_repo.get_point(chunk_id)
    if current is None:
        return _fail(f"知识点不存在：{chunk_id}")

    fields: dict = {}
    if "content" in payload:
        content = payload["content"] or ""
        err = _check_content(content)
        if err:
            return _fail(err)
        fields["content"] = content
        fields["char_count"] = len(content)
    if "titlePath" in payload:
        fields["title_path"] = (payload["titlePath"] or "").strip()
    if "blockType" in payload:
        block_type = (payload["blockType"] or "").strip()
        if block_type not in BLOCK_TYPES:
            return _fail(f"块类型非法：{block_type}（可选 {'/'.join(BLOCK_TYPES)}）")
        fields["block_type"] = block_type
    if "imageKeys" in payload:
        fields["image_keys"] = payload["imageKeys"] or None
    if "visibleRoles" in payload:
        fields["visible_roles"] = kb_repo.roles_to_str(payload["visibleRoles"])

    if not fields:
        return _fail("没有需要更新的字段")

    if not kb_repo.update_point_fields(chunk_id, fields):
        return _fail(f"知识点不存在：{chunk_id}")

    # 正文或标题路径变了 → 向量必须重算（向量文本 = 标题路径 + 正文）
    touched = {"content", "title_path"} & set(fields)
    vec = {"synced": bool(current.get("vecSynced")), "warning": None}
    if touched and payload.get("reembed", True):
        title_path = fields.get("title_path", current.get("titlePath") or "")
        content = fields.get("content", current.get("content") or "")
        vec = _reembed(chunk_id, title_path, content)

    extra = _after_write(scope["docId"])
    return _ok(
        {
            "point": kb_repo.get_point(chunk_id) or {},
            "changed": sorted(fields),
            "vecSynced": vec["synced"],
            "warning": vec["warning"],
            **extra,
        }
    )


# ------------------------------------------------------------------ 再加工
def append_text(chunk_id: str, text: str, position: str = "end") -> dict:
    """再加工 · 添加文本内容。"""
    blocked = _must_mysql("追加内容")
    if blocked:
        return blocked
    if position not in ("end", "start"):
        return _fail(f"position 非法：{position}（可选 start/end）")

    current = kb_repo.get_point(chunk_id)
    if current is None:
        return _fail(f"知识点不存在：{chunk_id}")
    body = current.get("content") or ""
    merged = f"{text}{body}" if position == "start" else f"{body}{text}"
    err = _check_content(merged)
    if err:
        return _fail(err)

    kb_repo.update_point_fields(
        chunk_id, {"content": merged, "char_count": len(merged)}
    )
    vec = _reembed(chunk_id, current.get("titlePath") or "", merged)
    extra = _after_write(kb_repo.parse_public_doc_id(current.get("docId")))
    return _ok(
        {
            "point": kb_repo.get_point(chunk_id) or {},
            "appended": len(text),
            "position": position,
            "vecSynced": vec["synced"],
            "warning": vec["warning"],
            **extra,
        }
    )


def remove_content(
    chunk_id: str, *, text: str | None = None, start: int | None = None, end: int | None = None
) -> dict:
    """再加工 · 删除内容（按片段文本或字符区间）。"""
    blocked = _must_mysql("删除内容")
    if blocked:
        return blocked

    current = kb_repo.get_point(chunk_id)
    if current is None:
        return _fail(f"知识点不存在：{chunk_id}")
    body = current.get("content") or ""

    if text:
        pos = body.find(text)
        if pos < 0:
            return _fail("未找到要删除的内容（按片段删除需与正文完全一致）")
        left, right = pos, pos + len(text)
    elif start is not None and end is not None:
        if end <= start:
            return _fail(f"区间非法：start={start} 必须小于 end={end}")
        left, right = max(0, start), min(len(body), end)
        if left >= right:
            return _fail(f"区间超出正文范围（正文 {len(body)} 字）")
    else:
        return _fail("需提供 text 或 start/end 之一")

    removed = body[left:right]
    merged = body[:left] + body[right:]
    if not merged.strip():
        return _fail("删除后正文为空：如需移除整条知识点，请使用删除知识点")

    kb_repo.update_point_fields(
        chunk_id, {"content": merged, "char_count": len(merged)}
    )
    vec = _reembed(chunk_id, current.get("titlePath") or "", merged)
    extra = _after_write(kb_repo.parse_public_doc_id(current.get("docId")))
    return _ok(
        {
            "point": kb_repo.get_point(chunk_id) or {},
            "removed": len(removed),
            "range": [left, right],
            "vecSynced": vec["synced"],
            "warning": vec["warning"],
            **extra,
        }
    )


def insert_image(chunk_id: str, key: str) -> dict:
    """再加工 · 插入图片。

    只允许引用**该文档已有**的图片对象键（原件本身或内嵌图）。这样既避免
    凭空指向不存在的对象，也复用了 ``doc_preview`` 的两层路径穿越防护。
    """
    blocked = _must_mysql("插入图片")
    if blocked:
        return blocked

    current = kb_repo.get_point(chunk_id)
    if current is None:
        return _fail(f"知识点不存在：{chunk_id}")
    doc_id = kb_repo.parse_public_doc_id(current.get("docId"))
    if doc_id is None:
        return _fail("无法解析所属文档")

    doc = kb_repo.get_doc(doc_id)
    chunks = kb_repo.list_chunks(doc_id)
    try:
        images = doc_preview.collect_images(doc or {}, chunks)
    except Exception as exc:  # noqa: BLE001 - 图片元信息失败不阻断主流程
        return _fail(f"读取文档图片清单失败：{exc}")

    norm = doc_preview.normalize_key(key)
    allowed = {i["key"] for i in images}
    if norm not in allowed:
        return _fail(f"图片不属于该文档：{norm}（请先在文档内上传该图片）")

    keys = list(current.get("imageKeys") or [])
    if norm in keys:
        return _ok(
            {
                "point": current,
                "added": False,
                "imageKeys": keys,
                "message": "该图片已在知识点中",
            }
        )
    keys.append(norm)
    kb_repo.update_point_fields(chunk_id, {"image_keys": keys})
    return _ok(
        {
            "point": kb_repo.get_point(chunk_id) or {},
            "added": True,
            "imageKeys": keys,
        }
    )


# ------------------------------------------------------------------ 删除
def delete_point(chunk_id: str) -> dict:
    """删除无效切片（同时删向量，回写文档切片数）。"""
    blocked = _must_mysql("删除知识点")
    if blocked:
        return blocked
    res = kb_repo.delete_point(chunk_id)
    if not res.get("ok"):
        return _fail(res.get("error") or f"知识点不存在：{chunk_id}")
    extra = _after_write(res.get("docId"))
    return _ok({"deleted": res["deleted"], "chunkId": chunk_id, **extra})


# ------------------------------------------------------------------ 导出
_EXPORT_COLUMNS = (
    ("chunkId", "知识点编号"),
    ("docId", "文档编号"),
    ("docTitle", "文档标题"),
    ("seqNo", "序号"),
    ("blockType", "块类型"),
    ("titlePath", "标题路径"),
    ("dept", "部门"),
    ("ktype", "知识类型"),
    ("charCount", "字符数"),
    ("content", "正文"),
)


def export_points(chunk_ids: list[str], fmt: str = "csv") -> dict:
    """批量导出知识点，返回 ``{filename, mediaType, content(bytes)}``。

    CSV 用 ``utf-8-sig``（Excel 直接双击不乱码），MD 按文档分节，
    JSON 保留完整字段——三种格式覆盖「给业务看 / 给编辑用 / 给系统对接」。
    """
    if fmt not in ("csv", "md", "json"):
        return _fail(f"导出格式非法：{fmt}（可选 csv/md/json）")

    points: list[dict] = []
    for cid in chunk_ids:
        point = kb_repo.get_point(cid) if _mysql_mode() else None
        if point is None and not _mysql_mode():
            found = get_point(cid)
            point = found.get("data") if found.get("ok") else None
        if point is None:
            continue
        points.append(point)
    if not points:
        return _fail("没有可导出的知识点（编号不存在或已删除）")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if fmt == "json":
        body = json.dumps(points, ensure_ascii=False, indent=2).encode("utf-8")
        return _ok({"filename": f"knowledge-points-{stamp}.json",
                    "mediaType": "application/json; charset=utf-8", "content": body})

    if fmt == "md":
        lines = [f"# 知识点导出（{len(points)} 条）", ""]
        for p in points:
            lines.append(f"## {p['chunkId']} · {p.get('docTitle') or ''}")
            lines.append("")
            lines.append(f"- 文档：{p.get('docId')}（序号 {p.get('seqNo')}）")
            lines.append(f"- 分类：{p.get('dept') or '-'} / {p.get('ktype') or '-'}")
            lines.append(f"- 块类型：{p.get('blockType')}　字符数：{p.get('charCount')}")
            if p.get("titlePath"):
                lines.append(f"- 标题路径：{p['titlePath']}")
            lines.append("")
            lines.append(p.get("content") or "")
            lines.append("")
        body = "\n".join(lines).encode("utf-8")
        return _ok({"filename": f"knowledge-points-{stamp}.md",
                    "mediaType": "text/markdown; charset=utf-8", "content": body})

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow([label for _, label in _EXPORT_COLUMNS])
    for p in points:
        writer.writerow([p.get(key, "") if p.get(key) is not None else "" for key, _ in _EXPORT_COLUMNS])
    body = buf.getvalue().encode("utf-8-sig")
    return _ok({"filename": f"knowledge-points-{stamp}.csv",
                "mediaType": "text/csv; charset=utf-8", "content": body})
