"""知识目录增量同步（对应 kb-engine/src/sync.js）。

按 ``file_hash`` 比对，只重算新增/变化的文档，并删除库中已不存在的文档：

    added    知识目录里新出现、库里没有的文档
    updated  文件哈希变化（或上次解析失败后重试成功）的文档
    deleted  库里存在但知识目录里已消失的文档
    skipped  哈希一致，直接跳过（不做解析/向量化）

上传件（``file_key LIKE 'uploads/%'``）不参与同步：它们不在知识目录里，
不删是刻意的——否则每次同步都会清掉用户上传的知识。
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from app.config import settings
from app.core.parser import SUPPORTED_EXT
from app.core.parser import image as image_parser
from app.db import kb_repo
from app.db.session import db_transaction
from app.services.kb_admin import parse_doc

logger = logging.getLogger(__name__)

# 与原型 walk() 一致的忽略规则
_SKIP_PREFIXES = (".", "00-")
_SKIP_BASENAME_PREFIXES = ("~$",)


def iter_source_files(root: str | Path | None = None) -> list[Path]:
    """遍历知识目录，返回待同步文件（与原型 ``walk`` 规则一致）。

    规则：跳过以 ``.`` 或 ``00-`` 开头的条目；只收 **depth >= 1** 的文件
    （根目录下的散文件不视为知识源）；只收受支持的扩展名；跳过 Office 临时文件。
    """
    base = Path(root or settings.knowledge_dir)
    if not base.is_dir():
        return []

    out: list[Path] = []

    def walk(d: Path, depth: int) -> None:
        try:
            entries = sorted(d.iterdir())
        except OSError as exc:
            logger.warning("目录不可读，跳过：%s（%s）", d, exc)
            return
        for entry in entries:
            if entry.name.startswith(_SKIP_PREFIXES):
                continue
            if entry.is_dir():
                walk(entry, depth + 1)
            elif entry.is_file() and depth >= 1:
                if entry.suffix.lower() not in SUPPORTED_EXT:
                    continue
                if entry.name.startswith(_SKIP_BASENAME_PREFIXES):
                    continue
                out.append(entry)

    walk(base, 0)
    return out


def _rel(path: Path, root: Path) -> str:
    """统一成正斜杠相对路径（与库中 file_key 存储格式一致）。"""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def sync_knowledge_base(root: str | Path | None = None, txn=None) -> dict:
    """执行增量同步，返回 ``{added, updated, deleted, skipped, errors, warnings}``。"""
    base = Path(root or settings.knowledge_dir)
    files = iter_source_files(base)
    summary: dict = {
        "added": 0,
        "updated": 0,
        "deleted": 0,
        "skipped": 0,
        "errors": [],
        "warnings": [],
    }

    if not files and not base.is_dir():
        summary["errors"].append(f"知识目录不存在：{base}")
        return summary

    with (txn or db_transaction)() as conn:
        db_rows = kb_repo.list_source_docs(conn)

        # 安全阀：源目录一个文件都没扫到、而库里却有知识时，**拒绝执行**。
        # 否则一次 KNOWLEDGE_DIR 配置错误就会把整个知识库删空
        # （原型 sync.js 在此场景下会静默删光，属高危行为，这里刻意收紧）。
        if not files and db_rows:
            summary["errors"].append(
                f"知识目录未扫描到任何文件（{base}），但库中已有 {len(db_rows)} 篇知识；"
                f"为避免误删已中止同步，请检查 KNOWLEDGE_DIR 配置"
            )
            return summary

        by_key = {r["file_key"]: r for r in db_rows}
        seen: set[str] = set()

        for f in files:
            key = _rel(f, base)
            seen.add(key)
            try:
                buf = f.read_bytes()
            except OSError as exc:
                summary["errors"].append(f"{key}：读取失败 {exc}")
                continue

            digest = hashlib.sha256(buf).hexdigest()
            row = by_key.get(key)
            if row and row["file_hash"] == digest:
                summary["skipped"] += 1
                continue

            parsed = parse_doc(f, image_sink=image_parser.save_original)
            if not parsed["ok"]:
                summary["errors"].append(f"{key}：{parsed['error']}")
                continue
            for warn in parsed.get("warnings") or []:
                summary["warnings"].append(f"{key}：{warn}")

            status = kb_repo.status_code(parsed["doc_meta"].get("status"))
            try:
                kb_repo.write_doc(
                    conn,
                    file_key=key,
                    file_size=len(buf),
                    file_hash=digest,
                    doc_meta=parsed["doc_meta"],
                    chunks=parsed["chunks"],
                    embeddings=parsed["embeddings"],
                    status=status,
                    doc_id=row["doc_id"] if row else None,
                    source_type="import",
                )
            except Exception as exc:  # noqa: BLE001 - 单文档失败不中断整体同步
                logger.exception("同步写入失败：%s", key)
                summary["errors"].append(f"{key}：写入失败 {exc}")
                continue

            if row:
                summary["updated"] += 1
            else:
                summary["added"] += 1

        for key, row in by_key.items():
            if key in seen:
                continue
            try:
                kb_repo.delete_doc_on_conn(conn, row["doc_id"])
                summary["deleted"] += 1
            except Exception as exc:  # noqa: BLE001
                logger.exception("同步删除失败：%s", key)
                summary["errors"].append(f"{key}：删除失败 {exc}")

    logger.info(
        "增量同步完成：新增 %d ｜ 更新 %d ｜ 删除 %d ｜ 跳过 %d ｜ 错误 %d",
        summary["added"],
        summary["updated"],
        summary["deleted"],
        summary["skipped"],
        len(summary["errors"]),
    )
    return summary
