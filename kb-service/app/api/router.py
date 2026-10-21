"""路由汇总，统一挂载到 /api 前缀。"""

from __future__ import annotations

from fastapi import APIRouter

from app.api import (
    access,
    admin,
    agents,
    apps,
    asr,
    auth,
    channels,
    chat,
    data,
    data_center,
    docs,
    gov,
    health,
    integrations,
    knowledge,
    meta,
    sessions,
    share,
    support,
    sync,
    unrecognized,
)

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(asr.router, prefix="/asr", tags=["asr"])
api_router.include_router(auth.router, tags=["auth"])
api_router.include_router(chat.router, tags=["chat"])
api_router.include_router(docs.router, tags=["docs"])
api_router.include_router(knowledge.router, tags=["knowledge"])
api_router.include_router(sessions.router, tags=["sessions"])
api_router.include_router(meta.router, tags=["meta"])
api_router.include_router(sync.router, tags=["sync"])
api_router.include_router(admin.router, tags=["admin"])
api_router.include_router(access.router, tags=["access"])
api_router.include_router(unrecognized.router, tags=["unrecognized"])
api_router.include_router(channels.admin_router, tags=["channels"])
api_router.include_router(channels.public_router, tags=["channels"])
api_router.include_router(agents.public_router, tags=["agents"])
api_router.include_router(apps.admin_router, tags=["apps"])
api_router.include_router(apps.public_router, tags=["apps"])
api_router.include_router(gov.admin_router, tags=["gov"])
api_router.include_router(gov.public_router, tags=["gov"])
api_router.include_router(data.router, tags=["data"])
api_router.include_router(data_center.router, tags=["data-center"])
api_router.include_router(integrations.router, tags=["integrations"])
api_router.include_router(share.public_router, tags=["share"])
api_router.include_router(share.feedback_router, tags=["share"])
api_router.include_router(share.admin_router, tags=["share"])
api_router.include_router(support.admin_router, tags=["support"])
api_router.include_router(support.public_router, tags=["support"])
