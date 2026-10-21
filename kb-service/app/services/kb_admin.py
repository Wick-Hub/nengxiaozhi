"""知识库管理服务（对应 kb-engine/src/store/admin.js）。

职责：把「解析 → 切片 → 向量化 → 事务入库」编排起来，供上传接口与增量同步复用。
SQL 细节全部下沉到 :mod:`app.db.kb_repo`，本层不写裸 SQL。

与原型的一处**有意差异**：原型在 ``ingestUploaded`` 的 ``finally`` 里删掉 staging
原件，导致 ``file_key`` 指向不存在的文件（原文预览无从取件）。本实现**保留原件**
到 ``uploads/``，以支撑任务 1.11 的原文预览/下载。

任务 1.10 追加：内嵌图片经 ``image.save_original`` 落盘（内容寻址，天然去重），
对象键写进 ``kb_chunk.image_keys``；图片型文档自身即原图（对象键 = 上传件 key）。
解析过程中的**非致命问题**（OCR 引擎缺失、图片无法解码等）以 ``warnings`` 返回，
不阻断入库——入库成功但「没提取到文字」这件事必须让调用方看得见。
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from pathlib import Path

from app.config import settings
from app.core.chunker import blocks_to_chunks
from app.core.embedder import get_embedder
from app.core.parser import SUPPORTED_EXT, parse_file
from app.core.parser import image as image_parser
from app.db import kb_repo
from app.db.session import db_transaction

logger = logging.getLogger(__name__)

# 与原型 ingestUploaded 的 safeName 规则一致：保留中英文/数字/._-，其余替换为 _
_UNSAFE_RE = re.compile(r"[^\w.\u4e00-\u9fff-]", re.UNICODE)

DEFAULT_SOURCE = "示例数据（待替换为学校真实文件）"


# ------------------------------------------------------------------ 解析
def parse_doc(
    file_path: str | Path,
    overrides: dict | None = None,
    *,
    fallback: dict | None = None,
    image_sink=None,
    self_image_key: str | None = None,
    default_title: str | None = None,
) -> dict:
    """解析 → 切片 → 向量化，返回 ``{ok, doc_meta, chunks, embeddings, warnings}``。

    对齐原型 ``parseDoc``：overrides 优先于文件元数据，缺省回落到「未分类」。

    :param fallback: 元数据兜底（同步时按目录名推导 dept/ktype，供 XLSX/JSON
        等无 frontmatter 的格式使用），优先级低于文件元数据
    :param image_sink: 内嵌图片落盘回调（``None`` 则不抽取内嵌图片）
    :param self_image_key: 图片型文件自身的对象键（上传件为 ``uploads/<safe_name>``）
    :param default_title: 标题兜底（上传件用**原始文件名**，避免时间戳前缀进标题）
    """
    overrides = overrides or {}
    fallback = fallback or {}
    path = Path(file_path)
    try:
        parsed = parse_file(
            path, image_sink=image_sink, self_image_key=self_image_key
        )
    except Exception as exc:  # noqa: BLE001 - 解析失败要以业务错误返回，不是 500
        logger.warning("解析失败：%s（%s）", path, exc)
        return {"ok": False, "error": f"文件解析失败：{exc}"}

    meta = parsed.meta or {}
    roles = overrides.get("visibleRoles") or overrides.get("visible_roles")
    doc_meta = {
        "title": overrides.get("title") or meta.get("title") or default_title or path.stem,
        "dept": overrides.get("dept") or meta.get("dept") or fallback.get("dept") or "未分类",
        "ktype": overrides.get("ktype") or meta.get("ktype") or fallback.get("ktype") or "未分类",
        "visible_roles": (
            ",".join(roles)
            if isinstance(roles, (list, tuple)) and roles
            else (meta.get("visible_roles") or "全部")
        ),
        "status": meta.get("status") or "",
        "source": meta.get("source") or DEFAULT_SOURCE,
    }

    chunks = blocks_to_chunks(parsed.blocks, doc_meta, path)
    if not chunks:
        return {"ok": False, "error": "文件解析后无有效内容"}

    warnings: list[str] = []
    image_meta = meta.get("image")
    if isinstance(image_meta, dict):
        if image_meta.get("warning"):
            warnings.append(str(image_meta["warning"]))
        if image_meta.get("ocrOk"):
            logger.info(
                "图片已 OCR：%s（engine=%s，%d 行）",
                path.name,
                image_meta.get("ocrEngine"),
                image_meta.get("ocrLineCount", 0),
            )
    if any(c.image_keys for c in chunks):
        logger.info(
            "已关联原图：%s（%d 张）",
            path.name,
            len({k for c in chunks for k in c.image_keys}),
        )

    embeddings = get_embedder().encode([c.embed_text for c in chunks])
    return {
        "ok": True,
        "doc_meta": doc_meta,
        "chunks": chunks,
        "embeddings": embeddings,
        "warnings": warnings,
    }


def supported_formats() -> list[str]:
    """受支持的扩展名（排序后，供错误提示与管理页 ``accept``）。"""
    return sorted(SUPPORTED_EXT)


# ------------------------------------------------------------------ 上传入库
def safe_upload_name(filename: str) -> str:
    """生成 ``<毫秒时间戳>-<安全文件名>``，与原型命名规则一致。"""
    return f"{int(time.time() * 1000)}-{_UNSAFE_RE.sub('_', Path(filename).name)}"


def ingest_uploaded(
    *,
    file_buf: bytes,
    filename: str,
    dept: str,
    ktype: str,
    status: int = 0,
    visible_roles: list[str] | None = None,
    title: str | None = None,
    txn=None,
) -> dict:
    """上传入库：落盘原件 → 解析 → 切片 → 向量化 → 单文档事务写入。

    只影响该文档，不触碰其他知识（与原型一致）。

    :param txn: 事务上下文工厂（测试可注入回滚版本），默认 :func:`db_transaction`
    :return: 成功 ``{ok:True, docId, chunkCount, title, dept, ktype, warnings}``；
             失败 ``{ok:False, error}``
    """
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXT:
        supported = " ".join(supported_formats())
        return {"ok": False, "error": f"不支持的文件类型 {ext}（支持 {supported}）"}
    if kb_repo.ktype_enum(ktype) not in kb_repo.KTYPE_NAME:
        return {"ok": False, "error": f"未知知识类型 {ktype}"}

    upload_dir = Path(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = safe_upload_name(filename)
    staging_path = upload_dir / safe_name
    staging_path.write_bytes(file_buf)
    file_key = f"uploads/{safe_name}"

    try:
        parsed = parse_doc(
            staging_path,
            {"title": title, "dept": dept, "ktype": ktype, "visibleRoles": visible_roles},
            image_sink=image_parser.save_original,
            self_image_key=file_key,
            default_title=Path(filename).stem,
        )
        if not parsed["ok"]:
            staging_path.unlink(missing_ok=True)
            return parsed

        with (txn or db_transaction)() as conn:
            # 查重：kb_doc.file_hash 是唯一键，重复内容直接 insert 会抛 IntegrityError。
            # 提前拦下并给出可读提示（原型此处会抛出原始 SQL 错误）。
            dup = kb_repo.find_doc_by_hash(conn, hashlib.sha256(file_buf).hexdigest())
            if dup:
                staging_path.unlink(missing_ok=True)
                return {
                    "ok": False,
                    "error": (
                        f"该文件内容已存在（与「{dup['title']}」/ doc_id={dup['doc_id']} "
                        f"的 SHA-256 相同），请勿重复上传"
                    ),
                }

            result = kb_repo.write_doc(
                conn,
                file_key=file_key,
                file_size=len(file_buf),
                file_hash=hashlib.sha256(file_buf).hexdigest(),
                doc_meta=parsed["doc_meta"],
                chunks=parsed["chunks"],
                embeddings=parsed["embeddings"],
                status=status,
                source_type="upload",
            )
    except Exception as exc:  # noqa: BLE001 - 入库失败要回滚并给业务错误
        logger.exception("上传入库失败：%s", filename)
        staging_path.unlink(missing_ok=True)
        return {"ok": False, "error": f"入库失败：{exc}"}

    warnings = list(parsed.get("warnings") or [])
    logger.info(
        "已入库：%s → doc_id=%s（%d 切片%s）",
        filename,
        result["docId"],
        result["chunkCount"],
        f"，{len(warnings)} 条告警" if warnings else "",
    )
    return {
        "ok": True,
        "docId": result["docId"],
        "chunkCount": result["chunkCount"],
        "title": parsed["doc_meta"]["title"],
        "dept": parsed["doc_meta"]["dept"],
        "ktype": parsed["doc_meta"]["ktype"],
        "warnings": warnings,
    }


# ------------------------------------------------------------------ 便捷包装
def list_docs(
    status: int | None = None,
    ktype: str | None = None,
    category_id: int | None = None,
    dept: str | None = None,
) -> list[dict]:
    return kb_repo.list_docs(
        status=status, ktype_raw=ktype, category_id=category_id, dept_name=dept
    )


def update_doc_status(doc_id: int, status: int) -> dict:
    return kb_repo.update_doc_status(doc_id, status)


def delete_doc(doc_id: int) -> dict:
    return kb_repo.delete_doc(doc_id)
