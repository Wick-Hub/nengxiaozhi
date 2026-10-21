"""知识库 MySQL 仓储（任务 1.14）。

对齐 kb-engine 原型实现：
    src/store/mysql.js  → save_store / load_store / ensure_org / ensure_category
    src/store/admin.js  → list_docs / update_doc_status / delete_doc / write_doc / ingest_uploaded
    src/sync.js         → sync_knowledge_base

编号规则（与原型一致）：``kb_doc.doc_id`` 为自增整数，对外编号 ``D{doc_id:03d}``；
切片编号 ``D{doc_id:03d}-C{seq_no:03d}``。因此全量重建前必须 TRUNCATE，
让自增从 1 重新开始，保证 ``D001`` 与 ``doc_id=1`` 对齐。

向量不入 MySQL 生产表：原型用 ``kb_chunk_vec_proto``（JSON 列）模拟 Milvus，
生产环境应替换为 Milvus 集合 ``kb_chunk_vec``（HNSW）。
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, func, insert, select, text, update

from app.config import settings
from app.db import models as M
from app.db.session import db_session, get_engine
from app.db.vector_repository import get_vector_repository

logger = logging.getLogger(__name__)

# 向量读写走适配层（首版 MySQL JSON，生产可替换 Milvus）
vectors = get_vector_repository()


def _utc_now_iso() -> str:
    """``2026-10-08T07:50:21.053Z`` 风格时间戳（对齐 JS ``toISOString()``）。"""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"

# ------------------------------------------------------------------ 枚举
KTYPE_MAP = M.KTYPE_MAP
KTYPE_NAME = M.KTYPE_NAME
STATUS_MAP = M.STATUS_MAP
STATUS_NAME = M.STATUS_NAME


def ktype_enum(raw: str | None) -> str:
    """中文知识类型 → 生产枚举（未知值原样返回）。"""
    return KTYPE_MAP.get(raw or "", raw or "")


def ktype_label(enum_or_raw: str | None) -> str:
    """生产枚举 → 中文名（未知值原样返回）。"""
    v = enum_or_raw or ""
    return KTYPE_NAME.get(v, v)


def status_code(raw) -> int:
    """中文状态 / 数值 → 数值状态码（未知默认 2 已发布）。"""
    if isinstance(raw, int):
        return raw
    return STATUS_MAP.get(str(raw), 2)


def doc_public_id(row_id: int) -> str:
    """自增主键 → 对外文档编号 ``D001``。"""
    return f"D{int(row_id):03d}"


def chunk_public_id(row_id: int, seq_no: int) -> str:
    """→ 切片编号 ``D001-C002``。"""
    return f"D{int(row_id):03d}-C{int(seq_no):03d}"


def parse_public_doc_id(value: str | int) -> int | None:
    """对外编号 / 数字 → ``kb_doc.doc_id``；无法解析返回 None。"""
    if isinstance(value, int):
        return value
    s = str(value).strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if s.upper().startswith("D") and s[1:].isdigit():
        return int(s[1:])
    return None


def roles_to_str(roles) -> str:
    if isinstance(roles, str):
        return roles or "全部"
    if not roles:
        return "全部"
    return ",".join(str(r) for r in roles)


def roles_from_str(value) -> list[str]:
    if not value:
        return ["全部"]
    return [r for r in str(value).split(",") if r]


def _json_list(value) -> list[str]:
    """``kb_chunk.image_keys``（JSON 列）→ 字符串列表。

    驱动未自动反序列化时兜底 ``json.loads``；``None``/空值 → ``[]``。
    """
    if not value:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return [value] if value else []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v]
    return []


# ------------------------------------------------------------------ 基础
def ensure_org(conn, dept_name: str, sort_no: int) -> int:
    """按部门名取/建 ``sys_org`` 记录，返回 ``org_id``。"""
    row = conn.execute(
        select(M.SysOrg.org_id).where(M.SysOrg.org_code == dept_name)
    ).first()
    if row:
        return int(row[0])
    res = conn.execute(
        insert(M.SysOrg).values(
            parent_id=0, org_code=dept_name, org_name=dept_name, org_path="", sort_no=sort_no
        )
    )
    org_id = int(res.inserted_primary_key[0])
    conn.execute(
        update(M.SysOrg).where(M.SysOrg.org_id == org_id).values(org_path=f"/{org_id}/")
    )
    return org_id


def ensure_category(conn, dept_id: int, ktype_raw: str) -> int:
    """按（部门 × 知识类型）取/建 ``kb_category`` 记录。"""
    ktype = ktype_enum(ktype_raw)
    row = conn.execute(
        select(M.KbCategory.category_id).where(
            M.KbCategory.dept_id == dept_id, M.KbCategory.ktype == ktype
        )
    ).first()
    if row:
        return int(row[0])
    res = conn.execute(
        insert(M.KbCategory).values(
            dept_id=dept_id, ktype=ktype, category_name=ktype_label(ktype) or ktype_raw
        )
    )
    return int(res.inserted_primary_key[0])


def _file_meta(rel_path: str) -> tuple[int, str]:
    """读取知识库原件的大小与 SHA-256（缺失时用路径哈希兜底，避免唯一键冲突）。"""
    p = Path(settings.knowledge_dir) / rel_path
    try:
        buf = p.read_bytes()
        return len(buf), hashlib.sha256(buf).hexdigest()
    except OSError:
        logger.warning("原件缺失，改用路径哈希：%s", p)
        return 0, hashlib.sha256(rel_path.encode("utf-8")).hexdigest()


#: 允许 TRUNCATE 的表白名单（任务 1.12：DDL 不支持绑定参数，改用白名单校验标识符）
TRUNCATE_ALLOWLIST: frozenset[str] = frozenset({"kb_chunk_vec_proto", "kb_chunk", "kb_doc"})
_TRUNCATE_ORDER: tuple[str, ...] = ("kb_chunk_vec_proto", "kb_chunk", "kb_doc")


def _truncate_kb_tables() -> None:
    """清空知识三表并重置自增（TRUNCATE 隐式提交，故用 AUTOCOMMIT 连接）。

    ``TRUNCATE TABLE`` 是 DDL，**无法用绑定参数传表名**，因此这里的标识符拼接
    必须先过白名单校验；守卫测试 ``test_security.py::test_no_sql_string_interpolation``
    对拼接点要求显式 ``noqa: SQL-IDENTIFIER`` 标记。
    """
    engine = get_engine()
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        for table in _TRUNCATE_ORDER:
            if table not in TRUNCATE_ALLOWLIST:
                raise ValueError(f"拒绝 TRUNCATE 非白名单表：{table}")
            conn.execute(text(f"TRUNCATE TABLE {table}"))  # noqa: SQL-IDENTIFIER


# ------------------------------------------------------------------ 全量写入
def save_store(store: dict) -> dict:
    """把一份完整索引（store.json 结构）写入 MySQL，全量重建。

    :return: ``{"docs": n, "chunks": m}``
    """
    docs = list(store.get("docs") or [])
    chunks = list(store.get("chunks") or [])
    model = store.get("model") or settings.embed_model

    _truncate_kb_tables()

    # 文档对外编号 → 自增主键
    with db_session() as conn:
        # 部门（保持首次出现顺序，sort_no 递增）
        org_ids: dict[str, int] = {}
        for i, dept in enumerate(dict.fromkeys(d.get("dept") or "未分类" for d in docs)):
            org_ids[dept] = ensure_org(conn, dept, i)

        cat_ids: dict[tuple[str, str], int] = {}
        for d in docs:
            dept = d.get("dept") or "未分类"
            ktype = ktype_enum(d.get("ktype"))
            key = (dept, ktype)
            if key not in cat_ids:
                cat_ids[key] = ensure_category(conn, org_ids[dept], d.get("ktype") or "")

        doc_rows = []
        for d in docs:
            dept = d.get("dept") or "未分类"
            ktype = ktype_enum(d.get("ktype"))
            file_rel = d.get("file") or ""
            size, sha = _file_meta(file_rel) if file_rel else (0, "")
            # 文档可见角色 = 其切片可见角色的并集（与原型一致）
            roles = list(
                dict.fromkeys(
                    r
                    for c in chunks
                    if c.get("docId") == d.get("docId")
                    for r in (c.get("visibleRoles") or ["全部"])
                )
            )
            doc_rows.append(
                {
                    "doc_title": d.get("title") or file_rel or "未命名",
                    "category_id": cat_ids[(dept, ktype)],
                    "dept_id": org_ids[dept],
                    "ktype": ktype,
                    "file_format": (Path(file_rel).suffix.lstrip(".").lower() if file_rel else ""),
                    "file_key": file_rel,
                    "file_size": size,
                    "file_hash": sha,
                    "chunk_count": d.get("chunkCount", 0),
                    "visible_roles": roles_to_str(roles),
                    "status": status_code(d.get("status")),
                    "version_no": 1,
                    "source_type": "import",
                    "published_at": d.get("updated") or None,
                }
            )

        row_id_by_public: dict[str, int] = {}
        for d, row in zip(docs, doc_rows):
            res = conn.execute(insert(M.KbDoc).values(**row))
            row_id_by_public[d.get("docId")] = int(res.inserted_primary_key[0])

        chunk_rows, vec_ids, vec_embs = [], [], []
        for c in chunks:
            row_id = row_id_by_public.get(c.get("docId"))
            if row_id is None:
                continue  # 切片无所属文档，跳过（防御）
            chunk_id = c.get("chunkId") or chunk_public_id(row_id, c.get("seqNo", 0))
            chunk_rows.append(
                {
                    "chunk_id": chunk_id,
                    "doc_id": row_id,
                    "seq_no": c.get("seqNo", 0),
                    "block_type": c.get("blockType") or "para",
                    "title_path": c.get("titlePath") or "",
                    "content": c.get("text") or "",
                    "char_count": c.get("charCount", 0),
                    "visible_roles": roles_to_str(c.get("visibleRoles")),
                    "status": 2,
                    "vec_synced": 1,
                    "image_keys": list(c.get("imageKeys") or []) or None,
                }
            )
            vec_ids.append(chunk_id)
            vec_embs.append(list(c.get("embedding") or []))

        if chunk_rows:
            conn.execute(insert(M.KbChunk), chunk_rows)
            vectors.save(conn, vec_ids, vec_embs, model)

    logger.info("知识库已写入 MySQL：%d 文档 / %d 切片", len(doc_rows), len(chunk_rows))
    return {"docs": len(doc_rows), "chunks": len(chunk_rows)}


# ------------------------------------------------------------------ 全量读取
def load_store() -> dict:
    """从 MySQL 读取完整索引，结构对齐 ``kb-engine/data/store.json``。

    仅返回已发布（``status=2``）的文档，与原型 ``loadStoreMysql`` 一致。
    无数据时抛 :class:`RuntimeError`（提示先执行 ingest）。
    """
    with db_session() as conn:
        doc_rows = conn.execute(
            select(
                M.KbDoc.doc_id,
                M.KbDoc.doc_title,
                M.KbDoc.file_key,
                M.KbDoc.ktype,
                M.KbDoc.chunk_count,
                M.KbDoc.status,
                M.KbDoc.published_at,
                M.SysOrg.org_name,
            )
            .join(M.SysOrg, M.SysOrg.org_id == M.KbDoc.dept_id)
            .where(M.KbDoc.status == 2)
            .order_by(M.KbDoc.doc_id)
        ).all()

        if not doc_rows:
            raise RuntimeError(
                f"MySQL（{settings.db_name}）中无已发布知识，请先执行 ingest 导入知识库"
            )

        chunk_rows = conn.execute(
            select(
                M.KbChunk.chunk_id,
                M.KbChunk.doc_id,
                M.KbChunk.seq_no,
                M.KbChunk.block_type,
                M.KbChunk.title_path,
                M.KbChunk.content,
                M.KbChunk.char_count,
                M.KbChunk.visible_roles,
                M.KbChunk.image_keys,
                M.SysOrg.org_name,
                M.KbDoc.ktype,
                M.KbChunkVecProto.embedding,
                M.KbChunkVecProto.model,
            )
            .join(M.KbDoc, M.KbDoc.doc_id == M.KbChunk.doc_id)
            .join(M.SysOrg, M.SysOrg.org_id == M.KbDoc.dept_id)
            .join(M.KbChunkVecProto, M.KbChunkVecProto.chunk_id == M.KbChunk.chunk_id)
            .where(M.KbDoc.status == 2)
            .order_by(M.KbChunk.doc_id, M.KbChunk.seq_no)
        ).all()

    public_by_row = {int(r.doc_id): doc_public_id(r.doc_id) for r in doc_rows}

    docs = [
        {
            "docId": public_by_row[int(r.doc_id)],
            "file": r.file_key,
            "title": r.doc_title,
            "dept": r.org_name,
            "ktype": ktype_label(r.ktype),
            "status": STATUS_NAME[r.status] if 0 <= int(r.status) < len(STATUS_NAME) else "",
            "updated": r.published_at.strftime("%Y-%m-%d") if r.published_at else "",
            "source": "",
            "chunkCount": r.chunk_count,
        }
        for r in doc_rows
    ]

    chunks = []
    for r in chunk_rows:
        title_path = r.title_path or ""
        content = r.content or ""
        emb = r.embedding
        if isinstance(emb, (str, bytes)):  # 驱动未自动解析 JSON 时兜底
            emb = json.loads(emb)
        item = {
            "chunkId": r.chunk_id,
            "docId": public_by_row.get(int(r.doc_id), doc_public_id(r.doc_id)),
            "seqNo": r.seq_no,
            "charCount": r.char_count,
            "dept": r.org_name,
            "ktype": ktype_label(r.ktype),
            "visibleRoles": roles_from_str(r.visible_roles),
            "titlePath": title_path,
            "blockType": r.block_type,
            "text": content,
            "embedText": f"{title_path}\n{content}" if title_path else content,
            "embedding": list(emb or []),
        }
        image_keys = _json_list(r.image_keys)
        if image_keys:  # 仅在非空时输出，保持既有 store.json 形状不变
            item["imageKeys"] = image_keys
        chunks.append(item)

    return {
        "version": 1,
        "model": (chunk_rows[0].model if chunk_rows else "") or "",
        # 原型 loadStoreMysql 用当前时间戳（表示「读取时刻的快照」）
        "createdAt": _utc_now_iso(),
        "docs": docs,
        "chunks": chunks,
    }


# ------------------------------------------------------------------ 文档管理
def list_docs(
    status: int | None = None,
    ktype_raw: str | None = None,
    category_id: int | None = None,
    dept_name: str | None = None,
) -> list[dict]:
    """文档列表（管理页字段，含全部状态）。

    ``status``、``ktype``、``categoryId``、``dept`` 均为**可选**过滤，
    全部在 SQL 层下推（不取回后内存过滤）。
    """
    with db_session() as conn:
        stmt = (
            select(
                M.KbDoc.doc_id,
                M.KbDoc.doc_title,
                M.KbDoc.category_id,
                M.KbDoc.ktype,
                M.KbDoc.file_format,
                M.KbDoc.file_key,
                M.KbDoc.file_size,
                M.KbDoc.file_hash,
                M.KbDoc.chunk_count,
                M.KbDoc.visible_roles,
                M.KbDoc.status,
                M.KbDoc.version_no,
                M.KbDoc.source_type,
                M.KbDoc.published_at,
                M.KbDoc.created_at,
                M.KbDoc.updated_at,
                M.SysOrg.org_name,
            )
            .join(M.SysOrg, M.SysOrg.org_id == M.KbDoc.dept_id)
            .order_by(M.KbDoc.doc_id)
        )
        if status is not None:
            stmt = stmt.where(M.KbDoc.status == status)
        if ktype_raw:
            ktype = ktype_enum(ktype_raw)
            stmt = stmt.where(M.KbDoc.ktype == ktype)
        if category_id is not None:
            stmt = stmt.where(M.KbDoc.category_id == category_id)
        if dept_name:
            stmt = stmt.where(M.SysOrg.org_name == dept_name)
        rows = conn.execute(stmt).all()

    return [
        {
            "docId": int(r.doc_id),
            "title": r.doc_title,
            "categoryId": int(r.category_id) if r.category_id is not None else None,
            "dept": r.org_name,
            "ktype": ktype_label(r.ktype),
            "fileFormat": r.file_format,
            "fileKey": r.file_key,
            "fileSize": r.file_size,
            "fileHash": r.file_hash,
            "chunkCount": r.chunk_count,
            "visibleRoles": roles_from_str(r.visible_roles),
            "status": int(r.status),
            "statusName": STATUS_NAME[int(r.status)] if 0 <= int(r.status) < 4 else str(r.status),
            "versionNo": r.version_no,
            "sourceType": r.source_type,
            "publishedAt": r.published_at.isoformat(sep=" ") if r.published_at else None,
            "createdAt": r.created_at.isoformat(sep=" ") if r.created_at else None,
            "updatedAt": r.updated_at.isoformat(sep=" ") if r.updated_at else None,
        }
        for r in rows
    ]


def update_doc_status(doc_id: int, status: int) -> dict:
    """状态流转；置为 2（已发布）时记录发布时间。"""
    with db_session() as conn:
        res = conn.execute(
            update(M.KbDoc)
            .where(M.KbDoc.doc_id == doc_id)
            .values(status=status, published_at=func.if_(status == 2, func.now(), None))
        )
        if res.rowcount == 0:
            return {"ok": False, "error": "文档不存在"}
    return {"ok": True, "docId": doc_id, "status": status, "statusName": STATUS_NAME[status]}


def batch_update_status(doc_ids: list[int], status: int) -> dict:
    """批量状态流转（任务 2.7 / M-K-09）。

    返回 ``{ok, updated, status, docIds}``；``updated`` 为实际命中行数
    （不存在的 doc_id 不计入）。置为 2（已发布）时记录发布时间。
    """
    if not doc_ids:
        return {"ok": True, "updated": 0, "status": status, "docIds": []}
    with db_session() as conn:
        res = conn.execute(
            update(M.KbDoc)
            .where(M.KbDoc.doc_id.in_(doc_ids))
            .values(status=status, published_at=func.if_(status == 2, func.now(), None))
        )
    return {"ok": True, "updated": int(res.rowcount or 0), "status": status, "docIds": doc_ids}


def batch_move_category(doc_ids: list[int], category_id: int | None) -> dict:
    """批量移动文档到分类（任务 2.7 / M-K-09）。

    ``category_id`` 为 ``None`` 时移动到「未分类」（0）。返回实际命中行数。
    """
    if not doc_ids:
        return {"ok": True, "updated": 0, "categoryId": category_id, "docIds": []}
    target = int(category_id or 0)
    with db_session() as conn:
        if target:
            # 校验目标分类存在
            exists = conn.execute(
                select(M.KbCategory.category_id).where(
                    M.KbCategory.category_id == target
                )
            ).first()
            if not exists:
                return {"ok": False, "error": f"目标分类不存在：categoryId={target}"}
        res = conn.execute(
            update(M.KbDoc)
            .where(M.KbDoc.doc_id.in_(doc_ids))
            .values(category_id=target)
        )
    return {"ok": True, "updated": int(res.rowcount or 0), "categoryId": target, "docIds": doc_ids}


def batch_delete_docs(doc_ids: list[int]) -> dict:
    """批量删除文档（任务 2.7 / M-K-09）。逐个走完整删除（含切片/向量/引用）。"""
    deleted = 0
    with db_session() as conn:
        for doc_id in doc_ids:
            if delete_doc_on_conn(conn, doc_id):
                deleted += 1
    return {"ok": True, "deleted": deleted}


def delete_doc_on_conn(conn, doc_id: int) -> bool:
    """在给定连接/事务内删除文档及其切片、向量、引用。

    顺序：引用 → 向量 → 切片 → 文档（先删从表，避免外键约束失败）。
    """
    conn.execute(delete(M.AnswerCitation).where(M.AnswerCitation.doc_id == doc_id))
    vectors.delete_by_doc(conn, doc_id)
    conn.execute(delete(M.KbChunk).where(M.KbChunk.doc_id == doc_id))
    res = conn.execute(delete(M.KbDoc).where(M.KbDoc.doc_id == doc_id))
    return res.rowcount > 0


def delete_doc(doc_id: int) -> dict:
    with db_session() as conn:
        deleted = delete_doc_on_conn(conn, doc_id)
    return {"ok": True, "deleted": deleted, "docId": doc_id}


def write_doc(
    conn,
    *,
    file_key: str,
    file_size: int,
    file_hash: str,
    doc_meta: dict,
    chunks: list,
    embeddings: list[list[float]],
    status: int,
    doc_id: int | None = None,
    model: str | None = None,
    source_type: str = "import",
) -> dict:
    """写入/覆盖单个文档（doc_id 为空则新增，否则原地更新并 version+1）。

    :param chunks: ``Chunk`` 对象列表（需含 seq_no / block_type / title_path / text / char_count）
    """
    model = model or settings.embed_model
    dept = doc_meta.get("dept") or "未分类"
    ktype_raw = doc_meta.get("ktype") or "未分类"
    visible = roles_to_str(doc_meta.get("visible_roles") or doc_meta.get("visibleRoles"))

    max_sort = conn.execute(select(func.coalesce(func.max(M.SysOrg.sort_no), 0) + 1)).scalar()
    org_id = ensure_org(conn, dept, int(max_sort or 1))
    category_id = ensure_category(conn, org_id, ktype_raw)
    ktype = ktype_enum(ktype_raw)
    fmt = Path(file_key).suffix.lstrip(".").lower()

    if doc_id is not None:
        # 覆盖前先清掉旧切片与向量（保留 doc_id，版本 +1）
        vectors.delete_by_doc(conn, doc_id)
        conn.execute(delete(M.KbChunk).where(M.KbChunk.doc_id == doc_id))
        conn.execute(
            update(M.KbDoc)
            .where(M.KbDoc.doc_id == doc_id)
            .values(
                doc_title=doc_meta.get("title") or file_key,
                category_id=category_id,
                dept_id=org_id,
                ktype=ktype,
                file_format=fmt,
                file_key=file_key,
                file_size=file_size,
                file_hash=file_hash,
                chunk_count=len(chunks),
                visible_roles=visible,
                status=status,
                version_no=M.KbDoc.version_no + 1,
                published_at=func.if_(status == 2, func.now(), M.KbDoc.published_at),
            )
        )
    else:
        res = conn.execute(
            insert(M.KbDoc).values(
                doc_title=doc_meta.get("title") or file_key,
                category_id=category_id,
                dept_id=org_id,
                ktype=ktype,
                file_format=fmt,
                file_key=file_key,
                file_size=file_size,
                file_hash=file_hash,
                chunk_count=len(chunks),
                visible_roles=visible,
                status=status,
                version_no=1,
                source_type=source_type,
                published_at=func.if_(status == 2, func.now(), None),
            )
        )
        doc_id = int(res.inserted_primary_key[0])

    chunk_rows, vec_ids, vec_embs = [], [], []
    for c, emb in zip(chunks, embeddings):
        chunk_id = chunk_public_id(doc_id, c.seq_no)
        chunk_rows.append(
            {
                "chunk_id": chunk_id,
                "doc_id": doc_id,
                "seq_no": c.seq_no,
                "block_type": c.block_type or "para",
                "title_path": c.title_path or "",
                "content": c.text or "",
                "char_count": c.char_count,
                "visible_roles": visible,
                "status": 2,
                "vec_synced": 1,
                "image_keys": list(getattr(c, "image_keys", None) or []) or None,
            }
        )
        vec_ids.append(chunk_id)
        vec_embs.append(list(emb))

    if chunk_rows:
        conn.execute(insert(M.KbChunk), chunk_rows)
        vectors.save(conn, vec_ids, vec_embs, model)

    return {"docId": doc_id, "chunkCount": len(chunks)}


def count_knowledge() -> dict:
    """知识库计数（健康检查/对账用）。"""
    with db_session() as conn:
        return {
            "docs": conn.execute(select(func.count()).select_from(M.KbDoc)).scalar() or 0,
            "published_docs": conn.execute(
                select(func.count()).select_from(M.KbDoc).where(M.KbDoc.status == 2)
            ).scalar()
            or 0,
            "chunks": conn.execute(select(func.count()).select_from(M.KbChunk)).scalar() or 0,
            "vectors": conn.execute(
                select(func.count()).select_from(M.KbChunkVecProto)
            ).scalar()
            or 0,
        }


# ------------------------------------------------------------------ 同步/预览辅助
def list_source_docs(conn) -> list[dict]:
    """知识目录来源的文档（排除 uploads/ 上传件），供增量同步比对 file_hash。

    与原型 ``sync.js`` 的 ``WHERE file_key NOT LIKE 'uploads/%'`` 一致。
    """
    rows = conn.execute(
        select(M.KbDoc.doc_id, M.KbDoc.file_key, M.KbDoc.file_hash).where(
            ~M.KbDoc.file_key.like("uploads/%")
        )
    ).all()
    return [
        {"doc_id": int(r.doc_id), "file_key": r.file_key or "", "file_hash": r.file_hash or ""}
        for r in rows
    ]


def get_doc(doc_id: int) -> dict | None:
    """单文档详情（含切片），供管理页预览（不限于已发布）。"""
    with db_session() as conn:
        row = conn.execute(
            select(
                M.KbDoc.doc_id,
                M.KbDoc.doc_title,
                M.KbDoc.ktype,
                M.KbDoc.file_format,
                M.KbDoc.file_key,
                M.KbDoc.file_size,
                M.KbDoc.file_hash,
                M.KbDoc.chunk_count,
                M.KbDoc.visible_roles,
                M.KbDoc.status,
                M.KbDoc.version_no,
                M.KbDoc.source_type,
                M.KbDoc.published_at,
                M.KbDoc.created_at,
                M.KbDoc.updated_at,
                M.SysOrg.org_name,
            )
            .join(M.SysOrg, M.SysOrg.org_id == M.KbDoc.dept_id)
            .where(M.KbDoc.doc_id == doc_id)
        ).first()
        if row is None:
            return None
        return {
            "docId": int(row.doc_id),
            "title": row.doc_title,
            "dept": row.org_name,
            "ktype": ktype_label(row.ktype),
            "fileFormat": row.file_format,
            "fileKey": row.file_key,
            "fileSize": row.file_size,
            "fileHash": row.file_hash,
            "chunkCount": row.chunk_count,
            "visibleRoles": roles_from_str(row.visible_roles),
            "status": int(row.status),
            "statusName": STATUS_NAME[int(row.status)] if 0 <= int(row.status) < 4 else "",
            "versionNo": row.version_no,
            "sourceType": row.source_type,
            "publishedAt": row.published_at.isoformat(sep=" ") if row.published_at else None,
            "createdAt": row.created_at.isoformat(sep=" ") if row.created_at else None,
            "updatedAt": row.updated_at.isoformat(sep=" ") if row.updated_at else None,
        }


def list_chunks(doc_id: int) -> list[dict]:
    """某文档的全部切片（含未发布文档），供管理页预览。"""
    with db_session() as conn:
        rows = conn.execute(
            select(
                M.KbChunk.chunk_id,
                M.KbChunk.doc_id,
                M.KbChunk.seq_no,
                M.KbChunk.block_type,
                M.KbChunk.title_path,
                M.KbChunk.content,
                M.KbChunk.char_count,
                M.KbChunk.visible_roles,
                M.KbChunk.image_keys,
                M.SysOrg.org_name,
                M.KbDoc.ktype,
            )
            .join(M.KbDoc, M.KbDoc.doc_id == M.KbChunk.doc_id)
            .join(M.SysOrg, M.SysOrg.org_id == M.KbDoc.dept_id)
            .where(M.KbChunk.doc_id == doc_id)
            .order_by(M.KbChunk.seq_no)
        ).all()

    public = doc_public_id(doc_id)
    out = []
    for r in rows:
        title_path = r.title_path or ""
        content = r.content or ""
        item = {
            "chunkId": r.chunk_id,
            "docId": public,
            "seqNo": r.seq_no,
            "charCount": r.char_count,
            "dept": r.org_name,
            "ktype": ktype_label(r.ktype),
            "visibleRoles": roles_from_str(r.visible_roles),
            "titlePath": title_path,
            "blockType": r.block_type,
            "text": content,
            "embedText": f"{title_path}\n{content}" if title_path else content,
        }
        image_keys = _json_list(r.image_keys)
        if image_keys:
            item["imageKeys"] = image_keys
        out.append(item)
    return out


def doc_exists(doc_id: int) -> bool:
    with db_session() as conn:
        return (
            conn.execute(
                select(M.KbDoc.doc_id).where(M.KbDoc.doc_id == doc_id)
            ).first()
            is not None
        )


def find_doc_by_hash(conn, file_hash: str, exclude_doc_id: int | None = None) -> dict | None:
    """按内容哈希查重（``kb_doc.file_hash`` 是唯一键）。

    命中说明「同一份内容已经入过库」——直接 insert 会抛 IntegrityError，
    提前查出可给出可读的业务提示。
    """
    if not file_hash:
        return None
    stmt = select(M.KbDoc.doc_id, M.KbDoc.doc_title, M.KbDoc.file_key).where(
        M.KbDoc.file_hash == file_hash
    )
    if exclude_doc_id is not None:
        stmt = stmt.where(M.KbDoc.doc_id != exclude_doc_id)
    row = conn.execute(stmt).first()
    if row is None:
        return None
    return {"doc_id": int(row.doc_id), "title": row.doc_title, "file_key": row.file_key}


def list_depts() -> list[str]:
    """已入库知识的部门列表（管理页 datalist，按 doc_id 首次出现顺序）。"""
    with db_session() as conn:
        rows = conn.execute(
            select(M.SysOrg.org_name)
            .join(M.KbDoc, M.KbDoc.dept_id == M.SysOrg.org_id)
            .order_by(M.KbDoc.doc_id)
        ).all()
    return list(dict.fromkeys(r[0] for r in rows if r[0]))


# ------------------------------------------------------------------ 分类管理（任务 2.5 / M-K-01·02）
def list_categories() -> list[dict]:
    """分类清单（含每分类文档数）。扁平数组，前端可按 ``dept`` 分组成树。"""
    with db_session() as conn:
        rows = conn.execute(
            select(
                M.KbCategory.category_id,
                M.KbCategory.dept_id,
                M.KbCategory.ktype,
                M.KbCategory.category_name,
                M.KbCategory.parent_id,
                M.KbCategory.sort_no,
                M.SysOrg.org_name,
                func.count(M.KbDoc.doc_id).label("doc_count"),
            )
            .join(M.SysOrg, M.SysOrg.org_id == M.KbCategory.dept_id)
            .outerjoin(M.KbDoc, M.KbDoc.category_id == M.KbCategory.category_id)
            .group_by(
                M.KbCategory.category_id,
                M.KbCategory.dept_id,
                M.KbCategory.ktype,
                M.KbCategory.category_name,
                M.KbCategory.parent_id,
                M.KbCategory.sort_no,
                M.SysOrg.org_name,
            )
            .order_by(M.SysOrg.sort_no, M.KbCategory.sort_no, M.KbCategory.category_id)
        ).all()

    return [
        {
            "categoryId": int(r[0]),
            "deptId": int(r[1]),
            "dept": r[6],
            "ktype": ktype_label(r[2]),
            "categoryName": r[3],
            "parentId": int(r[4] or 0),
            "sortNo": int(r[5] or 0),
            "docCount": int(r[7] or 0),
        }
        for r in rows
    ]


def find_category(conn, dept_id: int, ktype_raw: str) -> int | None:
    """按 （部门 id × 知识类型） 取分类 id；不存在返回 ``None``。"""
    ktype = ktype_enum(ktype_raw)
    row = conn.execute(
        select(M.KbCategory.category_id).where(
            M.KbCategory.dept_id == dept_id, M.KbCategory.ktype == ktype
        )
    ).first()
    return int(row[0]) if row else None


def create_category(
    conn, *, dept_id: int, ktype_raw: str, category_name: str | None = None,
    parent_id: int = 0, sort_no: int = 0,
) -> int:
    """新增分类；若 （部门 id × 类型） 已存在则返回已存在记录的 id（幂等）。"""
    ktype = ktype_enum(ktype_raw)
    existing = find_category(conn, dept_id, ktype_raw)
    if existing is not None:
        return existing
    if not category_name:
        category_name = ktype_label(ktype) or ktype_raw or "未分类"
    res = conn.execute(
        insert(M.KbCategory).values(
            dept_id=dept_id,
            ktype=ktype,
            category_name=category_name,
            parent_id=parent_id,
            sort_no=sort_no,
        )
    )
    return int(res.inserted_primary_key[0])


def update_category(conn, category_id: int, *, category_name: str, sort_no: int) -> bool:
    """重命名/排序分类；返回是否确有该记录。"""
    res = conn.execute(
        update(M.KbCategory)
        .where(M.KbCategory.category_id == category_id)
        .values(category_name=category_name, sort_no=sort_no)
    )
    return bool(res.rowcount)


def category_doc_count(conn, category_id: int) -> int:
    return int(
        conn.execute(
            select(func.count()).select_from(M.KbDoc).where(M.KbDoc.category_id == category_id)
        ).scalar()
        or 0
    )


def category_child_count(conn, category_id: int) -> int:
    return int(
        conn.execute(
            select(func.count()).select_from(M.KbCategory).where(
                M.KbCategory.parent_id == category_id
            )
        ).scalar()
        or 0
    )


def get_category(conn, category_id: int) -> dict | None:
    """单个分类（含文档数）；不存在返回 ``None``。"""
    row = conn.execute(
        select(
            M.KbCategory.category_id,
            M.KbCategory.dept_id,
            M.KbCategory.ktype,
            M.KbCategory.category_name,
            M.KbCategory.parent_id,
            M.KbCategory.sort_no,
            M.SysOrg.org_name,
        )
        .join(M.SysOrg, M.SysOrg.org_id == M.KbCategory.dept_id)
        .where(M.KbCategory.category_id == category_id)
    ).first()
    if not row:
        return None
    return {
        "categoryId": int(row[0]),
        "deptId": int(row[1]),
        "dept": row[6],
        "ktype": ktype_label(row[2]),
        "categoryName": row[3],
        "parentId": int(row[4] or 0),
        "sortNo": int(row[5] or 0),
        "docCount": category_doc_count(conn, category_id),
    }


def dept_id_by_name(conn, org_name: str) -> int | None:
    """部门名称 → ``sys_org.org_id``（管理页按名字选部门时用）。"""
    row = conn.execute(
        select(M.SysOrg.org_id).where(M.SysOrg.org_name == org_name)
    ).first()
    return int(row[0]) if row else None


def dept_exists(conn, dept_id: int) -> bool:
    return conn.execute(
        select(M.SysOrg.org_id).where(M.SysOrg.org_id == dept_id)
    ).first() is not None


def list_orgs() -> list[dict]:
    """全部部门（分类管理页的部门下拉；与 ``list_depts`` 不同，这里**不要求有文档**）。"""
    with db_session() as conn:
        rows = conn.execute(
            select(M.SysOrg.org_id, M.SysOrg.org_name, M.SysOrg.org_code)
            .where(M.SysOrg.status == 1)
            .order_by(M.SysOrg.sort_no, M.SysOrg.org_id)
        ).all()
    return [
        {"deptId": int(r[0]), "dept": r[1], "orgCode": r[2]} for r in rows
    ]


def delete_category(conn, category_id: int) -> bool:
    res = conn.execute(delete(M.KbCategory).where(M.KbCategory.category_id == category_id))
    return bool(res.rowcount)


# ------------------------------------------------------------------ 部门授权（任务 2.6 / M-K-03）
PERM_FIELDS = {
    "canUpload": "can_upload",
    "canView": "can_view",
    "canDownload": "can_download",
    "canDelete": "can_delete",
    "canCreateSub": "can_create_sub",
}
_PERM_COLS_KEYS = ("perm_id", "user_id", "dept_id", "ktype", "can_upload", "can_view",
                   "can_download", "can_delete", "can_create_sub", "user_no", "real_name",
                   "dept", "created_by", "created_at", "updated_at")


def _perm_row_to_dict(r) -> dict:
    """授权行（含 join 出来的 user_no/real_name/dept 名）→ 对外 dict。"""
    return {
        "permId": int(r[0]),
        "userId": int(r[1]),
        "userNo": r[9],
        "realName": r[10],
        "deptId": int(r[2] or 0),
        "dept": r[11],
        "ktype": r[3],
        "canUpload": int(r[4] or 0),
        "canView": int(r[5] or 0),
        "canDownload": int(r[6] or 0),
        "canDelete": int(r[7] or 0),
        "canCreateSub": int(r[8] or 0),
        "createdBy": int(r[12]) if r[12] is not None else None,
        "createdAt": r[13].isoformat(sep=" ") if r[13] else None,
        "updatedAt": r[14].isoformat(sep=" ") if r[14] else None,
    }


def list_permissions(user_id: int | None = None) -> list[dict]:
    """授权清单；``user_id`` 可选过滤（外部查询某人的全部授权）。"""
    cols = (
        M.KbPermission.perm_id,
        M.KbPermission.user_id,
        M.KbPermission.dept_id,
        M.KbPermission.ktype,
        M.KbPermission.can_upload,
        M.KbPermission.can_view,
        M.KbPermission.can_download,
        M.KbPermission.can_delete,
        M.KbPermission.can_create_sub,
        M.SysUser.user_no,
        M.SysUser.real_name,
        M.SysOrg.org_name,
        M.KbPermission.created_by,
        M.KbPermission.created_at,
        M.KbPermission.updated_at,
    )
    with db_session() as conn:
        stmt = (
            select(*cols)
            .join(M.SysUser, M.SysUser.user_id == M.KbPermission.user_id)
            .outerjoin(M.SysOrg, M.SysOrg.org_id == M.KbPermission.dept_id)
            .order_by(M.KbPermission.user_id, M.KbPermission.dept_id, M.KbPermission.ktype)
        )
        if user_id is not None:
            stmt = stmt.where(M.KbPermission.user_id == user_id)
        rows = conn.execute(stmt).all()
    return [_perm_row_to_dict(r) for r in rows]


def find_permission(conn, user_id: int, dept_id: int = 0, ktype: str = "*") -> int | None:
    """按 （用户 × 部门 × 类型） 查授权 id；不存在返回 ``None``。"""
    row = conn.execute(
        select(M.KbPermission.perm_id).where(
            M.KbPermission.user_id == user_id,
            M.KbPermission.dept_id == dept_id,
            M.KbPermission.ktype == ktype,
        )
    ).first()
    return int(row[0]) if row else None


def create_permission(
    conn, *, user_id: int, dept_id: int = 0, ktype: str = "*",
    can_upload: int = 0, can_view: int = 0, can_download: int = 0,
    can_delete: int = 0, can_create_sub: int = 0, created_by: int | None = None,
) -> int:
    res = conn.execute(
        insert(M.KbPermission).values(
            user_id=user_id,
            dept_id=dept_id,
            ktype=ktype,
            can_upload=can_upload,
            can_view=can_view,
            can_download=can_download,
            can_delete=can_delete,
            can_create_sub=can_create_sub,
            created_by=created_by,
        )
    )
    return int(res.inserted_primary_key[0])


def update_permission(
    conn, perm_id: int, *, can_upload: int, can_view: int,
    can_download: int, can_delete: int, can_create_sub: int,
) -> bool:
    res = conn.execute(
        update(M.KbPermission)
        .where(M.KbPermission.perm_id == perm_id)
        .values(
            can_upload=can_upload,
            can_view=can_view,
            can_download=can_download,
            can_delete=can_delete,
            can_create_sub=can_create_sub,
        )
    )
    return bool(res.rowcount)


def delete_permission(conn, perm_id: int) -> bool:
    res = conn.execute(delete(M.KbPermission).where(M.KbPermission.perm_id == perm_id))
    return bool(res.rowcount)


def get_user(conn, user_id: int) -> dict | None:
    """按 user_id 取用户基本信息（授权管理页的「被授权人」下拉用）。"""
    row = conn.execute(
        select(M.SysUser.user_id, M.SysUser.user_no, M.SysUser.real_name)
        .where(M.SysUser.user_id == user_id)
    ).first()
    if not row:
        return None
    return {"userId": int(row[0]), "userNo": row[1], "realName": row[2]}


def list_dept_liaisons() -> list[dict]:
    """部门联络员用户清单（授权管理页「被授权人」下拉）。"""
    with db_session() as conn:
        rows = conn.execute(
            select(M.SysUser.user_id, M.SysUser.user_no, M.SysUser.real_name, M.SysUser.org_id)
            .join(M.SysUserRole, M.SysUserRole.user_id == M.SysUser.user_id)
            .join(M.SysRole, M.SysRole.role_id == M.SysUserRole.role_id)
            .where(M.SysRole.role_code == "dept_liaison")
            .order_by(M.SysUser.user_no)
        ).all()
    return [
        {
            "userId": int(r[0]),
            "userNo": r[1],
            "realName": r[2],
            "orgId": int(r[3]) if r[3] is not None else None,
        }
        for r in rows
    ]


def get_permission(conn, perm_id: int) -> dict | None:
    """单个授权（含 join 信息）；不存在返回 ``None``。"""
    cols = (
        M.KbPermission.perm_id,
        M.KbPermission.user_id,
        M.KbPermission.dept_id,
        M.KbPermission.ktype,
        M.KbPermission.can_upload,
        M.KbPermission.can_view,
        M.KbPermission.can_download,
        M.KbPermission.can_delete,
        M.KbPermission.can_create_sub,
        M.SysUser.user_no,
        M.SysUser.real_name,
        M.SysOrg.org_name,
        M.KbPermission.created_by,
        M.KbPermission.created_at,
        M.KbPermission.updated_at,
    )
    row = conn.execute(
        select(*cols)
        .join(M.SysUser, M.SysUser.user_id == M.KbPermission.user_id)
        .outerjoin(M.SysOrg, M.SysOrg.org_id == M.KbPermission.dept_id)
        .where(M.KbPermission.perm_id == perm_id)
    ).first()
    if not row:
        return None
    return _perm_row_to_dict(row)


def user_permission_for(conn, user_id: int, dept_id: int, ktype: str) -> dict | None:
    """判定某用户对 （部门 × 类型） 的有效授权。

    * 命中精确（用户 × 部门 × 类型）记录 → 返回它；
    * 否则按「特化程度」在可匹配的授权里挑最贴近的一条：
      部门精确(0分) > 全部部门(1分)；类型精确(0分) > 全部类型(1分)；
    * 都没有 → 返回 ``None``。
    """
    best = None
    for r in conn.execute(
        select(
            M.KbPermission.dept_id,
            M.KbPermission.ktype,
            M.KbPermission.can_upload,
            M.KbPermission.can_view,
            M.KbPermission.can_download,
            M.KbPermission.can_delete,
            M.KbPermission.can_create_sub,
        ).where(M.KbPermission.user_id == user_id)
    ).all():
        # 只保留能匹配的：部门匹配(精确或0) 且 类型匹配(精确或*)
        if r[0] not in (dept_id, 0):
            continue
        if r[1] not in (ktype, "*"):
            continue
        score = (0 if r[0] == dept_id else 1) * 10 + (0 if r[1] == ktype else 1)
        if best is None or score < best[0]:
            best = (score, r)
    if best is None:
        return None
    _, r = best
    return {
        "deptId": int(r[0]),
        "ktype": r[1],
        "canUpload": int(r[2] or 0),
        "canView": int(r[3] or 0),
        "canDownload": int(r[4] or 0),
        "canDelete": int(r[5] or 0),
        "canCreateSub": int(r[6] or 0),
    }


# ------------------------------------------------------------------ 知识点管理（任务 2.8 / M-K-10）
#: 知识点 = 切片（``kb_chunk``）。管理端按「部门 × 类型 × 文档」集约化浏览，
#: 并支持检索、增删改、再加工（追加文本 / 删内容 / 插图）与批量导出。
EXCERPT_LEN = 240


def _point_row_to_dict(r, *, with_content: bool = True) -> dict:
    """切片行 → 接口字典（列顺序见 :func:`_point_columns`）。"""
    content = r[6] or ""
    item = {
        "chunkId": r[0],
        "docId": doc_public_id(r[1]),
        "docTitle": r[2],
        "seqNo": int(r[3]),
        "blockType": r[4],
        "titlePath": r[5] or "",
        "charCount": int(r[7] or 0),
        "dept": r[8],
        "ktype": ktype_label(r[9]),
        "categoryId": int(r[10]) if r[10] is not None else None,
        "visibleRoles": roles_from_str(r[11]),
        "vecSynced": int(r[12] or 0),
        "createdAt": r[13].isoformat(sep=" ") if r[13] else None,
    }
    image_keys = _json_list(r[14])
    if image_keys:
        item["imageKeys"] = image_keys
    if with_content:
        item["content"] = content
        item["embedText"] = f"{item['titlePath']}\n{content}" if item["titlePath"] else content
    else:
        item["excerpt"] = content[:EXCERPT_LEN]
    return item


def _point_columns() -> tuple:
    """切片列表/详情统一列顺序（下标取值，Core 查询不物化 ORM）。"""
    return (
        M.KbChunk.chunk_id,
        M.KbChunk.doc_id,
        M.KbDoc.doc_title,
        M.KbChunk.seq_no,
        M.KbChunk.block_type,
        M.KbChunk.title_path,
        M.KbChunk.content,
        M.KbChunk.char_count,
        M.SysOrg.org_name,
        M.KbDoc.ktype,
        M.KbDoc.category_id,
        M.KbChunk.visible_roles,
        M.KbChunk.vec_synced,
        M.KbChunk.created_at,
        M.KbChunk.image_keys,
    )


def _point_filters(
    stmt,
    *,
    dept_name: str | None = None,
    ktype_raw: str | None = None,
    category_id: int | None = None,
    doc_id: int | None = None,
    block_type: str | None = None,
    q: str | None = None,
):
    """把知识点筛选条件下推到 SQL（列表与计数共用，保证口径一致）。"""
    if dept_name:
        stmt = stmt.where(M.SysOrg.org_name == dept_name)
    if ktype_raw:
        stmt = stmt.where(M.KbDoc.ktype == ktype_enum(ktype_raw))
    if category_id is not None:
        stmt = stmt.where(M.KbDoc.category_id == category_id)
    if doc_id is not None:
        stmt = stmt.where(M.KbChunk.doc_id == doc_id)
    if block_type:
        stmt = stmt.where(M.KbChunk.block_type == block_type)
    for term in (q or "").split():
        like = f"%{term}%"
        # 同一词命中「正文」或「标题路径」即可；多个词之间是 AND（与检索层一致）
        stmt = stmt.where(
            M.KbChunk.content.like(like) | M.KbChunk.title_path.like(like)
        )
    return stmt


def list_points(
    *,
    dept_name: str | None = None,
    ktype_raw: str | None = None,
    category_id: int | None = None,
    doc_id: int | None = None,
    block_type: str | None = None,
    q: str | None = None,
    page: int = 1,
    size: int = 20,
) -> dict:
    """知识点分页列表；返回 ``{items, total, page, size}``。

    ``q`` 空格分隔多词、AND 语义；列表只带 ``excerpt``（240 字），
    全文由 :func:`get_point` 提供——避免一次拉回几十万字正文。
    """
    page = max(1, int(page or 1))
    size = max(1, min(200, int(size or 20)))
    with db_session() as conn:
        base = (
            select(*_point_columns())
            .join(M.KbDoc, M.KbDoc.doc_id == M.KbChunk.doc_id)
            .join(M.SysOrg, M.SysOrg.org_id == M.KbDoc.dept_id)
        )
        base = _point_filters(
            base,
            dept_name=dept_name,
            ktype_raw=ktype_raw,
            category_id=category_id,
            doc_id=doc_id,
            block_type=block_type,
            q=q,
        )
        count_stmt = select(func.count()).select_from(M.KbChunk).join(
            M.KbDoc, M.KbDoc.doc_id == M.KbChunk.doc_id
        ).join(M.SysOrg, M.SysOrg.org_id == M.KbDoc.dept_id)
        count_stmt = _point_filters(
            count_stmt,
            dept_name=dept_name,
            ktype_raw=ktype_raw,
            category_id=category_id,
            doc_id=doc_id,
            block_type=block_type,
            q=q,
        )
        total = int(conn.execute(count_stmt).scalar() or 0)
        rows = conn.execute(
            base.order_by(M.KbChunk.doc_id, M.KbChunk.seq_no)
            .limit(size)
            .offset((page - 1) * size)
        ).all()
    return {
        "items": [_point_row_to_dict(r, with_content=False) for r in rows],
        "total": total,
        "page": page,
        "size": size,
    }


def get_point(chunk_id: str) -> dict | None:
    """单条知识点详情（含全文）。"""
    with db_session() as conn:
        row = conn.execute(
            select(*_point_columns())
            .join(M.KbDoc, M.KbDoc.doc_id == M.KbChunk.doc_id)
            .join(M.SysOrg, M.SysOrg.org_id == M.KbDoc.dept_id)
            .where(M.KbChunk.chunk_id == chunk_id)
        ).first()
    return _point_row_to_dict(row) if row is not None else None


def point_scope(chunk_id: str) -> dict | None:
    """知识点的授权域（部门 id + 类型枚举），供接口层做 2.6 校验。"""
    with db_session() as conn:
        row = conn.execute(
            select(M.KbChunk.chunk_id, M.KbDoc.doc_id, M.KbDoc.dept_id, M.KbDoc.ktype)
            .join(M.KbDoc, M.KbDoc.doc_id == M.KbChunk.doc_id)
            .where(M.KbChunk.chunk_id == chunk_id)
        ).first()
    if row is None:
        return None
    return {
        "chunkId": row[0],
        "docId": int(row[1]),
        "deptId": int(row[2] or 0),
        "ktype": row[3],
    }


def max_chunk_seq(doc_id: int) -> int:
    """某文档现有切片的最大序号（无切片返回 0）。"""
    with db_session() as conn:
        return int(
            conn.execute(
                select(func.max(M.KbChunk.seq_no)).where(M.KbChunk.doc_id == doc_id)
            ).scalar()
            or 0
        )


def insert_point(
    *,
    doc_id: int,
    seq_no: int,
    block_type: str,
    title_path: str,
    content: str,
    char_count: int,
    visible_roles: str,
    image_keys: list[str] | None,
) -> str:
    """新增切片，返回 ``chunk_id``（``D001-C003``）。"""
    chunk_id = chunk_public_id(doc_id, seq_no)
    with db_session() as conn:
        conn.execute(
            insert(M.KbChunk).values(
                chunk_id=chunk_id,
                doc_id=doc_id,
                seq_no=seq_no,
                block_type=block_type,
                title_path=title_path,
                content=content,
                char_count=char_count,
                visible_roles=visible_roles,
                status=2,
                vec_synced=0,
                image_keys=image_keys or None,
            )
        )
    return chunk_id


def update_point_fields(chunk_id: str, fields: dict) -> bool:
    """更新切片字段（只写传入的列）；``vec_synced`` 由调用方显式置位。"""
    if not fields:
        return False
    with db_session() as conn:
        res = conn.execute(
            update(M.KbChunk).where(M.KbChunk.chunk_id == chunk_id).values(**fields)
        )
    return int(res.rowcount or 0) > 0


def delete_point(chunk_id: str) -> dict:
    """删除单条切片及其向量（文档 ``chunk_count`` 由调用方重算）。"""
    scope = point_scope(chunk_id)
    if scope is None:
        return {"ok": False, "error": f"知识点不存在：{chunk_id}"}
    with db_session() as conn:
        vectors.delete(conn, [chunk_id])
        res = conn.execute(delete(M.KbChunk).where(M.KbChunk.chunk_id == chunk_id))
    return {
        "ok": True,
        "deleted": int(res.rowcount or 0),
        "chunkId": chunk_id,
        "docId": scope["docId"],
    }


def save_point_vector(chunk_id: str, embedding: list[float], model: str | None = None) -> int:
    """单点写入/覆盖向量，并把 ``vec_synced`` 置 1。"""
    with db_session() as conn:
        n = vectors.save(conn, [chunk_id], [embedding], model)
        conn.execute(
            update(M.KbChunk).where(M.KbChunk.chunk_id == chunk_id).values(vec_synced=1)
        )
    return n


def drop_point_vector(chunk_id: str) -> int:
    """单点删除向量，并把 ``vec_synced`` 置 0（待重算）。"""
    with db_session() as conn:
        n = vectors.delete(conn, [chunk_id])
        conn.execute(
            update(M.KbChunk).where(M.KbChunk.chunk_id == chunk_id).values(vec_synced=0)
        )
    return n


def recount_doc_chunks(doc_id: int) -> int:
    """按实际切片数回写 ``kb_doc.chunk_count``，返回新值。"""
    with db_session() as conn:
        n = int(
            conn.execute(
                select(func.count()).select_from(M.KbChunk).where(M.KbChunk.doc_id == doc_id)
            ).scalar()
            or 0
        )
        conn.execute(
            update(M.KbDoc).where(M.KbDoc.doc_id == doc_id).values(chunk_count=n)
        )
    return n


def point_block_types() -> list[str]:
    """库内出现过的块类型（供前端下拉）。"""
    with db_session() as conn:
        rows = conn.execute(
            select(M.KbChunk.block_type).distinct().order_by(M.KbChunk.block_type)
        ).all()
    return [r[0] for r in rows if r[0]]
