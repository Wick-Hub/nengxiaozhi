"""kb-service 应用入口。

启动：
    uvicorn app.main:app --host 127.0.0.1 --port 8000
或：
    python -m app.main
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.router import api_router
from app.config import BASE_DIR, settings
from app.core.cors import cors_options
from app.security.masking import install_masking_filter

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
# 日志脱敏（任务 1.12）：学号 / 手机号 / 证件号 / 邮箱一律掩码后再落盘
install_masking_filter()
logger = logging.getLogger("kb-service")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "启动 %s v%s | store=%s | llm=%s | auth=%s | 限流=%s(%d/分钟) | 敏感词=%s",
        settings.app_name,
        settings.app_version,
        settings.store_label,
        settings.llm_label,
        "on" if settings.auth_enabled else "off",
        "on" if settings.rate_limit_enabled else "off",
        settings.rate_limit_per_minute,
        "on" if settings.sensitive_check_enabled else "off",
    )
    # TODO(1.4): 预热向量模型缓存
    # TODO(1.14): 建立数据库连接池并校验 schema
    from app.services import data_center

    data_center.start_scheduler()
    yield
    data_center.stop_scheduler()
    logger.info("关闭 %s", settings.app_name)


app = FastAPI(
    title="能小智 · kb-service",
    description="校园 AI 助手平台 Python 服务层（RAG 引擎 + 核心 API）",
    version=settings.app_version,
    lifespan=lifespan,
)

_cors = cors_options(settings)
if settings.cors_allow_credentials and "*" in _cors["allow_origins"]:
    logger.warning("CORS_ALLOW_ORIGINS=* 不允许携带凭证，已自动关闭 allow_credentials")
app.add_middleware(CORSMiddleware, **_cors)

app.include_router(api_router, prefix="/api")

# 前端静态资源同服（任务 1.15）
_static_dir = BASE_DIR / "static"
if _static_dir.is_dir():
    # 原型前端的「管理后台」入口是 /admin；预览对比页（任务 1.11）是新增的 /preview。
    # 这两个路由必须在 StaticFiles 挂载之前注册，否则会被 "/" 挂载吞掉。
    from fastapi.responses import FileResponse

    _pages = {
        "admin": _static_dir / "admin.html",
        "preview": _static_dir / "preview.html",
        # 任务 2.8 知识点管理（M-K-10）：独立页，避免 admin 页签数变化波及既有验收
        "points": _static_dir / "points.html",
        # 任务 3.1 渠道分身（M-C-01）：同上，独立页
        "channels": _static_dir / "channels.html",
        # 任务 3.3 应用广场（M-C-05/06）：同上，独立页
        "apps": _static_dir / "apps.html",
        # 任务 3.9 嵌入 SDK 演示页（模拟第三方站点用法）
        "sdk-demo": _static_dir / "sdk-demo.html",
        # 任务 3.11 未识别问题池（M-S-07～09）：独立页
        "unrecognized": _static_dir / "unrecognized.html",
        # 任务 4.9 问答反馈记录（P-QA-08）：独立页
        "feedback": _static_dir / "feedback.html",
        # 任务 4.5 导办事项管理（M-G-01）：独立页
        "gov": _static_dir / "gov.html",
        # 任务 4.4 通行查询智能体（P-DQ-04）：独立管理页
        "access": _static_dir / "access.html",
        # 任务 4.6 客服工作台（M-S-02～06）：独立管理页
        "support": _static_dir / "support.html",
        # 任务 4.2 数据中台任务与组织镜像：独立管理页
        "data-center": _static_dir / "data-center.html",
        # 任务 4.8 本地模型 / MCP / 第三方智能体：独立集成中心
        "integrations": _static_dir / "integrations.html",
        # 任务 4.5 一网通办大厅兑换票据（P-DB-02）：公开页，票据只在查询串
        "hall": _static_dir / "hall.html",
    }

    def _make_page(name: str):
        page = _pages[name]

        def _page() -> FileResponse:
            return FileResponse(page)

        return _page

    for _name, _page_path in _pages.items():
        if not _page_path.is_file():
            continue
        _handler = _make_page(_name)
        app.get(f"/{_name}", include_in_schema=False)(_handler)
        app.get(f"/{_name}/", include_in_schema=False)(_handler)

    if (_static_dir / "share.html").is_file():
        # 公开问答分享页（任务 4.9 / P-QA-07）：分享 ID 只在路径里，页面脚本再拉数据
        @app.get("/share/{share_id}", include_in_schema=False)
        def _share_page(share_id: str) -> FileResponse:
            return FileResponse(_static_dir / "share.html")

        @app.get("/share/{share_id}/", include_in_schema=False)
        def _share_page_slash(share_id: str) -> FileResponse:
            return FileResponse(_static_dir / "share.html")

    @app.get("/sdk/nxz.js", include_in_schema=False)
    def _sdk_js() -> FileResponse:
        # 独立路由而非静态挂载：保证 MIME 与缓存头稳定，第三方站点可长缓存
        return FileResponse(
            _static_dir / "sdk" / "nxz.js",
            media_type="application/javascript; charset=utf-8",
            headers={
                "Cache-Control": "public, max-age=300",
                "X-Content-Type-Options": "nosniff",
            },
        )

    app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static")


def main() -> None:
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )


if __name__ == "__main__":
    main()
