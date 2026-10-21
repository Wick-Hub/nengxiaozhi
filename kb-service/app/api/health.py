"""健康检查。

返回运行状态与索引计数；向量后端状态用于识别「哈希降级」这一非生产可用状态。
该探测不触发模型加载（仅报告已加载的后端）。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.config import settings
from app.core.embedder import get_embedder
from app.core.index_store import index_stats
from app.models.schemas import HealthResponse

router = APIRouter()


@router.get("/health", response_model=HealthResponse, summary="健康检查")
def health() -> HealthResponse:
    emb = get_embedder()
    backend = emb.loaded_backend
    stats = index_stats()
    return HealthResponse(
        ok=stats["error"] is None,
        store=settings.store_label,
        llm=settings.llm_label,
        version=settings.app_version,
        docs=stats["docs"],
        chunks=stats["chunks"],
        embed_backend=backend or "not_loaded",
        embed_is_fallback=backend == "hash",
    )
