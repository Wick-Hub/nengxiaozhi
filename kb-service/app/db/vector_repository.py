"""向量读写适配层（任务 1.14）。

原型把 512 维向量直接塞进 ``kb_chunk_vec_proto``（JSON 列）以模拟 Milvus。
本模块把这层封装成稳定的窄接口，使上层（``kb_repo`` / 检索）不直接依赖
具体存储：

* :class:`MySqlJsonVectorRepository` —— 当前实现，向量以 JSON 数组存 MySQL。
* 后续接 Milvus 时，只需实现同一组方法并在 :func:`get_vector_repository`
  中按配置切换，调用方零改动。

接口约定（全部在调用方给定的事务/连接内执行，本层不自行提交）：

    save(conn, chunk_ids, embeddings, model)   批量写入（先删后插，幂等）
    load(conn, chunk_ids) -> dict[str, list[float]]
    delete_by_doc(conn, doc_id) -> int         删除某文档的全部向量
    count(conn) -> int
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import delete, func, insert, select

from app.config import settings
from app.db import models as M

logger = logging.getLogger(__name__)


class MySqlJsonVectorRepository:
    """``kb_chunk_vec_proto``（JSON 列）实现。"""

    backend = "mysql-json"

    # ---------------------------------------------------------------- 写
    def save(
        self,
        conn,
        chunk_ids: list[str],
        embeddings: list[list[float]],
        model: str | None = None,
    ) -> int:
        """批量写入向量；同 chunk_id 已存在时先删再插（幂等）。"""
        if not chunk_ids:
            return 0
        if len(chunk_ids) != len(embeddings):
            raise ValueError(
                f"chunk_ids 与 embeddings 数量不一致：{len(chunk_ids)} vs {len(embeddings)}"
            )
        model = model or settings.embed_model
        conn.execute(delete(M.KbChunkVecProto).where(M.KbChunkVecProto.chunk_id.in_(chunk_ids)))
        rows = [
            {
                "chunk_id": cid,
                "embedding": list(emb),
                "dim": len(emb),
                "model": model,
            }
            for cid, emb in zip(chunk_ids, embeddings)
        ]
        conn.execute(insert(M.KbChunkVecProto), rows)
        return len(rows)

    # ---------------------------------------------------------------- 读
    def load(self, conn, chunk_ids: list[str]) -> dict[str, list[float]]:
        """按 chunk_id 批量取向量；缺失的键不出现在结果中。"""
        if not chunk_ids:
            return {}
        rows = conn.execute(
            select(M.KbChunkVecProto.chunk_id, M.KbChunkVecProto.embedding).where(
                M.KbChunkVecProto.chunk_id.in_(chunk_ids)
            )
        ).all()
        out: dict[str, list[float]] = {}
        for cid, emb in rows:
            if isinstance(emb, (str, bytes)):  # 驱动未自动解析 JSON 时兜底
                emb = json.loads(emb)
            out[str(cid)] = list(emb or [])
        return out

    # ---------------------------------------------------------------- 删
    def delete_by_doc(self, conn, doc_id: int) -> int:
        """删除某文档的全部切片向量（先按 doc_id 查 chunk_id，再删向量）。"""
        res = conn.execute(
            delete(M.KbChunkVecProto).where(
                M.KbChunkVecProto.chunk_id.in_(
                    select(M.KbChunk.chunk_id).where(M.KbChunk.doc_id == doc_id)
                )
            )
        )
        return int(res.rowcount or 0)

    def delete(self, conn, chunk_ids: list[str]) -> int:
        if not chunk_ids:
            return 0
        res = conn.execute(
            delete(M.KbChunkVecProto).where(M.KbChunkVecProto.chunk_id.in_(chunk_ids))
        )
        return int(res.rowcount or 0)

    # ---------------------------------------------------------------- 统计
    def count(self, conn) -> int:
        return int(
            conn.execute(select(func.count()).select_from(M.KbChunkVecProto)).scalar() or 0
        )

    def stats(self, conn) -> dict:
        row = conn.execute(
            select(
                func.count(),
                func.min(M.KbChunkVecProto.dim),
                func.max(M.KbChunkVecProto.dim),
                func.max(M.KbChunkVecProto.model),
            ).select_from(M.KbChunkVecProto)
        ).one()
        return {
            "backend": self.backend,
            "vectors": int(row[0] or 0),
            "dim_min": int(row[1] or 0),
            "dim_max": int(row[2] or 0),
            "model": row[3] or "",
        }


_repo: MySqlJsonVectorRepository | None = None


def get_vector_repository() -> MySqlJsonVectorRepository:
    """返回向量仓储单例（无状态，进程内复用即可）。"""
    global _repo
    if _repo is None:
        _repo = MySqlJsonVectorRepository()
    return _repo
