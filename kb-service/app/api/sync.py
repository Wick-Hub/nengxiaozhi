"""增量同步接口（对应原型 POST /api/sync）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from app.config import settings
from app.core.index_store import clear_index
from app.db.session import DatabaseUnavailable
from app.models.schemas import ApiResult
from app.security import oplog
from app.security.deps import require_admin, require_permission
from app.security.ratelimit import rate_limit
from app.services.kb_sync import sync_knowledge_base

# 管理端接口：鉴权（任务 1.13）+ 限流（任务 1.12）
router = APIRouter(
    dependencies=[
        Depends(require_admin),
        Depends(require_permission),
        Depends(rate_limit("admin")),
    ]
)

MYSQL_REQUIRED = (
    "增量同步需要 MySQL 存储：请在 .env 中设置 KB_STORE=mysql 并确保数据库已启动"
    "（docker compose -f ../database/docker-compose.yml up -d）"
)


@router.post("/sync", response_model=ApiResult, summary="增量同步知识目录")
def sync(request: Request) -> ApiResult:
    """按 ``file_hash`` 比对知识目录与库，做增/改/删。

    上传件（``file_key LIKE 'uploads/%'``）不参与同步，避免误删用户上传的知识。
    """
    if settings.store_label != "mysql":
        raise HTTPException(status_code=400, detail=MYSQL_REQUIRED)

    try:
        summary = sync_knowledge_base()
    except DatabaseUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"数据库不可用：{exc}") from exc

    clear_index()  # 知识库变更，索引缓存失效
    oplog.log_from_request(
        request,
        oplog.ACTION_SYNC,
        "kb_doc",
        "knowledge-base",
        detail={
            "added": summary.get("added"),
            "updated": summary.get("updated"),
            "deleted": summary.get("deleted"),
            "skipped": summary.get("skipped"),
        },
    )
    return ApiResult(ok=True, data=summary)
