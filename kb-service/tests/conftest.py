"""pytest 全局前置：在任何 app 模块被导入前，固定测试用环境。

要点：
- `LLM_MOCK=1`：端到端接线后 /api/chat 会真实调用 LLM，测试须走确定性模拟回答，
  既不依赖网络，也保证断言可复现。
- `KB_STORE=json`：测试使用本地 JSON 索引（kb-engine/data/store.json），不连 MySQL。
- `RERANK_ENABLED` 保持默认（有模型则精排，无模型自动降级），不强制关闭。

必须在 import app.* 之前设置，因为 app.config 在导入时即实例化并缓存（lru_cache）。

另提供 autouse fixture：把会话存储重定向到每个测试的临时目录，
避免测试写入真实运行数据 ``kb-service/data/sessions.json``。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("LLM_MOCK", "1")
os.environ.setdefault("KB_STORE", "json")
# 限流（任务 1.12）默认开启会让同一 TestClient 的连续请求触发 429，
# 因此测试默认关闭；限流本身在 tests/test_security.py 中显式开启验证。
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")


@pytest.fixture(autouse=True)
def _isolate_session_store(monkeypatch, tmp_path: Path):
    """所有测试默认使用独立会话/助手文件，互不干扰、不落真实数据。

    ⚠ 架构变更后**不再需要重定向 models.json**：模型清单的来源是网关
    （``gateway_admin.list_remote_models``，HTTP + 30s 缓存），能小智侧没有模型文件。
    但清单缓存必须在每个用例前清空 —— 否则前一个用例缓存的清单会串到下一个，
    表现为「改了 mock 却拿到旧清单」。
    """
    from app.core import llm_router
    from app.db import (
        access_store,
        agent_store,
        app_store,
        channel_store,
        chatlog,
        data_center_store,
        gov_store,
        integration_store,
        share_store,
        support_store,
        unrecognized_store,
    )
    from app.services import gateway_admin, unrecognized_admin

    monkeypatch.setattr(chatlog, "SESSIONS_PATH", tmp_path / "sessions.json")
    # 任务 3.11：未识别问题池同样隔离，避免测试数据落真实 data/unrecognized.json
    monkeypatch.setattr(unrecognized_store, "POOL_PATH", tmp_path / "unrecognized.json")
    unrecognized_admin.reset_cluster_cache()
    # 任务 2.2：助手存储同样重定向，避免测试污染 data/agents.json
    monkeypatch.setattr(agent_store, "AGENTS_PATH", tmp_path / "agents.json")
    # 任务 3.1：渠道存储同样隔离，避免测试写入真实 data/channels.json
    monkeypatch.setattr(channel_store, "CHANNELS_PATH", tmp_path / "channels.json")
    # 任务 3.3：应用存储隔离
    monkeypatch.setattr(app_store, "APPS_PATH", tmp_path / "apps.json")
    # 任务 4.9：分享与反馈存储隔离
    monkeypatch.setattr(share_store, "SHARES_PATH", tmp_path / "shares.json")
    monkeypatch.setattr(share_store, "FEEDBACK_PATH", tmp_path / "feedback.json")
    # 任务 4.5：导办事项与票据隔离（避免测试消费真实票据）
    monkeypatch.setattr(gov_store, "GOV_ITEMS_PATH", tmp_path / "gov_items.json")
    monkeypatch.setattr(gov_store, "GOV_TICKETS_PATH", tmp_path / "gov_tickets.json")
    # 任务 4.4：门禁流水本地分析库隔离
    monkeypatch.setattr(access_store, "ACCESS_EVENTS_PATH", tmp_path / "access_events.json")
    # 任务 4.6：客服分组 / 坐席 / 工单隔离
    monkeypatch.setattr(support_store, "SUPPORT_PATH", tmp_path / "support.json")
    # 任务 4.2：数据中台任务/批次/组织镜像与 mock 收件箱隔离
    monkeypatch.setattr(data_center_store, "DATA_CENTER_PATH", tmp_path / "data_center.json")
    monkeypatch.setattr(data_center_store, "SINK_DIR", tmp_path / "data_center_outbox")
    # 任务 4.8：本地模型 / MCP / 第三方智能体配置隔离
    monkeypatch.setattr(integration_store, "INTEGRATION_PATH", tmp_path / "integrations.json")
    gateway_admin.reset_models_cache()
    llm_router.reset_routes()
    yield
    gateway_admin.reset_models_cache()
    llm_router.reset_routes()
