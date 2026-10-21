"""补建/回填渠道表（任务 3.1 / M-C-01，幂等）。

``database/schema.sql`` 从批1 起就预留了 ``channel`` 与 ``channel_agent``；
本脚本做两件事：

1. 老库缺表时补建（``CREATE TABLE IF NOT EXISTS``）；
2. 把每个渠道的 ``default_agent_id`` 回填进 ``channel_agent``——
   3.1 之前只配主助手、没有关联行的渠道，也能在用户端看到默认助手。

重复运行安全。用法::

    python tools/migrate_channel_3_1.py
    python tools/migrate_channel_3_1.py --dry-run
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

#: 与 database/schema.sql 逐字一致，仅把 CREATE TABLE 换成 IF NOT EXISTS
CREATE_CHANNEL = """
CREATE TABLE IF NOT EXISTS channel (
  channel_id      BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '渠道ID',
  channel_code    VARCHAR(64) NOT NULL COMMENT '渠道编码',
  channel_name    VARCHAR(128) NOT NULL COMMENT '渠道名称',
  default_agent_id BIGINT NOT NULL COMMENT '绑定主助手',
  welcome_text    VARCHAR(1024) DEFAULT '' COMMENT '欢迎语',
  theme_config    JSON COMMENT '主题色/浮窗/形象等UI配置(M-C-01)',
  embed_url       VARCHAR(255) DEFAULT '' COMMENT '独立访问URL',
  embed_snippet   TEXT COMMENT 'iframe/浮窗嵌入代码',
  need_auth       TINYINT NOT NULL DEFAULT 1 COMMENT '1需统一认证 0访客可用',
  copy_of         BIGINT COMMENT '复制来源渠道ID(M-C-02 一键复制)',
  status          TINYINT NOT NULL DEFAULT 1,
  created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_channel_code (channel_code),
  KEY idx_default_agent (default_agent_id)
) COMMENT='渠道分身'
"""

CREATE_CHANNEL_AGENT = """
CREATE TABLE IF NOT EXISTS channel_agent (
  id          BIGINT PRIMARY KEY AUTO_INCREMENT,
  channel_id  BIGINT NOT NULL,
  agent_id    BIGINT NOT NULL,
  sort_no     INT NOT NULL DEFAULT 0,
  UNIQUE KEY uk_channel_agent (channel_id, agent_id)
) COMMENT='渠道助手关联'
"""

#: 主助手回填：只补缺失行，重复执行不会改动已有排序
BACKFILL_DEFAULT_AGENT = """
INSERT IGNORE INTO channel_agent (channel_id, agent_id, sort_no)
SELECT channel_id, default_agent_id, 0 FROM channel
"""

TABLES = {"channel": CREATE_CHANNEL, "channel_agent": CREATE_CHANNEL_AGENT}


def main() -> int:
    parser = argparse.ArgumentParser(description="补建/回填渠道表（任务 3.1，幂等）")
    parser.add_argument("--dry-run", action="store_true", help="只打印 DDL，不执行")
    args = parser.parse_args()

    from sqlalchemy import text

    from app.config import settings
    from app.db.session import db_session

    print("=" * 68)
    print("迁移（任务 3.1：channel / channel_agent，幂等）")
    print(f"存储模式：{settings.store_label}")
    print("=" * 68)

    if settings.store_label != "mysql":
        print("当前不是 MySQL 模式，无需迁移（JSON 模式无表结构）。")
        return 0

    if args.dry_run:
        for name, ddl in TABLES.items():
            print(f"\n-- {name}\n{ddl.strip()}")
        print(f"\n-- 回填默认助手\n{BACKFILL_DEFAULT_AGENT.strip()}")
        return 0

    try:
        with db_session() as conn:
            live = {str(r[0]) for r in conn.execute(text("SHOW TABLES")).all()}
    except Exception as exc:  # noqa: BLE001
        print(f"无法读取库结构：{type(exc).__name__}: {exc}")
        print("请确认 MySQL 已启动、库已按 schema.sql 建表。")
        return 1

    created = 0
    for name, ddl in TABLES.items():
        if name in live:
            print(f"\n{name}：已存在，跳过")
            continue
        print(f"\n{name}：不存在，创建…")
        try:
            with db_session() as conn:
                conn.execute(text(ddl))
        except Exception as exc:  # noqa: BLE001
            print(f"创建失败：{type(exc).__name__}: {exc}")
            return 1
        created += 1

    try:
        with db_session() as conn:
            result = conn.execute(text(BACKFILL_DEFAULT_AGENT))
            inserted = int(result.rowcount or 0)
        print(f"\n默认助手回填：新增 {inserted} 条 channel_agent 关联")
    except Exception as exc:  # noqa: BLE001
        print(f"回填失败：{type(exc).__name__}: {exc}")
        return 1

    print(f"\n完成：新建 {created} 张表，回填 {inserted} 条关联。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
