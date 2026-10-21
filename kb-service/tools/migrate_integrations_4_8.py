"""建集成中心三张表（任务 4.8，幂等）。

``integration_local_model`` / ``integration_mcp_server`` / ``integration_agent_app``。
JSON 模式使用 ``data/integrations.json``，无需迁移。

用法::

    python tools/migrate_integrations_4_8.py
    python tools/migrate_integrations_4_8.py --dry-run
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

os.environ.setdefault("KB_STORE", "mysql")

try:  # pragma: no cover - 仅影响交互输出
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

DDL: dict[str, str] = {
    "integration_local_model": """
        CREATE TABLE IF NOT EXISTS integration_local_model (
          local_id        BIGINT PRIMARY KEY AUTO_INCREMENT,
          model_code      VARCHAR(64) NOT NULL COMMENT '本地模型编码（可被助手绑定）',
          model_name      VARCHAR(128) NOT NULL,
          base_url        VARCHAR(512) NOT NULL COMMENT 'OpenAI 兼容 base_url，如 http://127.0.0.1:8001/v1',
          remote_model    VARCHAR(128) NOT NULL COMMENT '下发给 /chat/completions 的 model',
          api_key_enc     VARBINARY(1024) COMMENT 'AES-256-GCM 密文',
          timeout         FLOAT NOT NULL DEFAULT 30,
          status          TINYINT NOT NULL DEFAULT 1 COMMENT '1启用 0停用',
          is_active       TINYINT NOT NULL DEFAULT 0 COMMENT '当前启用（全表至多一条）',
          remark          VARCHAR(255) NOT NULL DEFAULT '',
          last_test_at    DATETIME,
          last_test_state VARCHAR(16) NOT NULL DEFAULT '' COMMENT 'ok/error/config-only',
          last_test_error VARCHAR(512) NOT NULL DEFAULT '',
          last_latency_ms INT NOT NULL DEFAULT 0,
          created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          UNIQUE KEY uk_ilm_code (model_code)
        ) COMMENT='本地 OpenAI 兼容大模型（任务 4.8）'
    """,
    "integration_mcp_server": """
        CREATE TABLE IF NOT EXISTS integration_mcp_server (
          server_id       BIGINT PRIMARY KEY AUTO_INCREMENT,
          server_code     VARCHAR(64) NOT NULL,
          server_name     VARCHAR(128) NOT NULL,
          transport       VARCHAR(16) NOT NULL DEFAULT 'http' COMMENT 'http/sse/stdio',
          endpoint        VARCHAR(512) NOT NULL DEFAULT '',
          command         VARCHAR(255) NOT NULL DEFAULT '' COMMENT 'stdio 启动命令（不执行）',
          args_json       JSON COMMENT 'stdio 参数',
          headers_enc     VARBINARY(4096) COMMENT '自定义 Header JSON 的 AES 密文',
          status          TINYINT NOT NULL DEFAULT 1,
          remark          VARCHAR(255) NOT NULL DEFAULT '',
          last_test_at    DATETIME,
          last_test_state VARCHAR(16) NOT NULL DEFAULT '',
          last_test_error VARCHAR(512) NOT NULL DEFAULT '',
          last_latency_ms INT NOT NULL DEFAULT 0,
          tools_json      JSON COMMENT '最近一次 tools/list 快照',
          created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          UNIQUE KEY uk_imcp_code (server_code)
        ) COMMENT='MCP 服务配置（任务 4.8）'
    """,
    "integration_agent_app": """
        CREATE TABLE IF NOT EXISTS integration_agent_app (
          app_id          BIGINT PRIMARY KEY AUTO_INCREMENT,
          app_code        VARCHAR(64) NOT NULL,
          app_name        VARCHAR(128) NOT NULL,
          kind            VARCHAR(16) NOT NULL DEFAULT 'dify' COMMENT 'dify/coze/webhook',
          base_url        VARCHAR(512) NOT NULL,
          remote_app_id   VARCHAR(128) NOT NULL DEFAULT '' COMMENT 'Coze bot_id 等',
          token_enc       VARBINARY(1024) COMMENT 'AES-256-GCM 密文',
          status          TINYINT NOT NULL DEFAULT 1,
          remark          VARCHAR(255) NOT NULL DEFAULT '',
          last_test_at    DATETIME,
          last_test_state VARCHAR(16) NOT NULL DEFAULT '',
          last_test_error VARCHAR(512) NOT NULL DEFAULT '',
          last_latency_ms INT NOT NULL DEFAULT 0,
          last_reply      VARCHAR(512) NOT NULL DEFAULT '',
          created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          UNIQUE KEY uk_iagent_code (app_code)
        ) COMMENT='第三方智能体应用（任务 4.8）'
    """,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="补建集成中心三张表（幂等）")
    parser.add_argument("--dry-run", action="store_true", help="只打印 DDL，不执行")
    args = parser.parse_args()

    if args.dry_run:
        for name, ddl in DDL.items():
            print(f"-- {name}\n{ddl.strip()}\n")
        return 0

    from sqlalchemy import text

    from app.db.session import db_transaction

    with db_transaction() as conn:
        for name, ddl in DDL.items():
            conn.execute(text(ddl))
            print(f"[OK] {name}")
        rows = conn.execute(text("SHOW TABLES LIKE 'integration_%'")).all()
        print(f"[OK] integration_* 表数量：{len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
