"""知识索引加载与缓存（任务 1.14）。

两种存储模式：

* ``KB_STORE=json``（默认）：读取 ``settings.store_path``；若不存在则回退到原型索引
  ``kb-engine/data/store.json``，便于迁移期直接复用已入库的切片。
* ``KB_STORE=mysql``：经 :mod:`app.db.kb_repo` 从 ``kb_doc`` / ``kb_chunk`` /
  ``kb_chunk_vec_proto`` 读取**仅已发布**（``status=2``）的知识，结构对齐 store.json。
  数据库不可用时返回带 ``error`` 的空快照，服务保持可用而不会崩。

索引加载带进程内缓存，知识库变更后调用 :func:`clear_index` 失效。
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path

from app.config import settings
from app.core.chunker import Chunk

logger = logging.getLogger(__name__)


@dataclass
class IndexSnapshot:
    chunks: list[Chunk] = field(default_factory=list)
    docs: list[dict] = field(default_factory=list)
    model: str = ""
    created_at: str = ""
    source: str = "json"
    path: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def candidate_paths() -> list[Path]:
    """索引文件候选路径（服务自带 → 原型索引）。"""
    return [
        Path(settings.store_path),
        settings.base_dir.parent / "kb-engine" / "data" / "store.json",
    ]


def _load_json(path: Path) -> IndexSnapshot:
    data = json.loads(path.read_text(encoding="utf-8"))
    chunks = [Chunk.from_dict(c) for c in data.get("chunks", [])]
    return IndexSnapshot(
        chunks=chunks,
        docs=list(data.get("docs", [])),
        model=data.get("model", ""),
        created_at=data.get("createdAt", ""),
        source="json",
        path=str(path),
    )


def _load_mysql() -> IndexSnapshot:
    """从 MySQL 读取已发布知识（仅 status=2），结构与 JSON 模式一致。

    数据库不可用 / 无数据时返回带 ``error`` 的空快照，不抛异常，
    以免拖垮 ``/api/chat`` 等依赖索引的接口。
    """
    # 延迟导入：JSON 模式下无需加载 SQLAlchemy，避免无谓依赖
    from app.db.kb_repo import load_store
    from app.db.session import DatabaseUnavailable

    try:
        data = load_store()
    except DatabaseUnavailable as exc:
        logger.error("MySQL 不可用，索引为空：%s", exc)
        return IndexSnapshot(source="mysql", error=f"MySQL 不可用：{exc}")
    except RuntimeError as exc:  # 库中无已发布知识
        logger.warning("MySQL 中无已发布知识：%s", exc)
        return IndexSnapshot(source="mysql", error=str(exc))
    except Exception as exc:  # noqa: BLE001 - 任何读取异常都降级为空索引
        logger.exception("MySQL 索引读取失败")
        return IndexSnapshot(source="mysql", error=f"MySQL 索引读取失败：{exc}")

    chunks = [Chunk.from_dict(c) for c in data.get("chunks", [])]
    target = f"mysql://{settings.db_host}:{settings.db_port}/{settings.db_name}"
    logger.info("索引已加载：%s（%d 文档 / %d 切片）", target, len(data.get("docs", [])), len(chunks))
    return IndexSnapshot(
        chunks=chunks,
        docs=list(data.get("docs", [])),
        model=data.get("model", ""),
        created_at=data.get("createdAt", ""),
        source="mysql",
        path=target,
    )


_lock = threading.Lock()
_cache: IndexSnapshot | None = None


def load_index(force: bool = False) -> IndexSnapshot:
    """加载索引（带缓存）。文件缺失或读取失败时返回带 error 的空快照。"""
    global _cache
    with _lock:
        if _cache is not None and not force:
            return _cache

        if settings.store_label == "mysql":
            _cache = _load_mysql()
            return _cache

        target = next((p for p in candidate_paths() if p.is_file()), None)
        if target is None:
            tried = "、".join(str(p) for p in candidate_paths())
            logger.warning("未找到知识库索引，已尝试：%s", tried)
            _cache = IndexSnapshot(error=f"未找到知识库索引（已尝试：{tried}）")
            return _cache

        try:
            _cache = _load_json(target)
            logger.info(
                "索引已加载：%s（%d 文档 / %d 切片）",
                target,
                len(_cache.docs),
                len(_cache.chunks),
            )
        except Exception as exc:  # noqa: BLE001 - 索引损坏时保持服务可用
            logger.exception("索引加载失败：%s", target)
            _cache = IndexSnapshot(error=f"索引加载失败：{exc}", path=str(target))
        return _cache


def get_index() -> IndexSnapshot:
    return load_index(force=False)


def clear_index() -> None:
    """知识库变更后调用，使缓存失效。"""
    global _cache
    with _lock:
        _cache = None


def index_stats() -> dict:
    idx = get_index()
    return {
        "docs": len(idx.docs),
        "chunks": len(idx.chunks),
        "model": idx.model,
        "created_at": idx.created_at,
        "source": idx.source,
        "path": idx.path,
        "error": idx.error,
    }
