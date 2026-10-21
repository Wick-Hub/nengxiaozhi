"""补建广场应用表（任务 3.3 / M-C-05·06，幂等）。

``database/schema.sql`` 已加入 ``app`` 表；本脚本给 3.3 之前建好的老库补建，
重复运行安全。JSON 模式无需迁移。

用法::

    python tools/migrate_app_3_3.py
    python tools/migrate_app_3_3.py --dry-run
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
CREATE_APP = """
CREATE TABLE IF NOT EXISTS app (
  app_id        BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '应用ID',
  app_name      VARCHAR(128) NOT NULL COMMENT '应用名称',
  source_type   VARCHAR(16) NOT NULL DEFAULT 'agent' COMMENT '来源:agent智能体/link外部链接',
  agent_id      BIGINT COMMENT '来源为智能体时的助手ID',
  link_url      VARCHAR(512) NOT NULL DEFAULT '' COMMENT '来源为外部链接时的地址',
  category      VARCHAR(32) NOT NULL DEFAULT '' COMMENT '分类',
  description   VARCHAR(255) NOT NULL DEFAULT '' COMMENT '简介',
  icon_url      VARCHAR(255) NOT NULL DEFAULT '' COMMENT '图标(URL或emoji)',
  sort_no       INT NOT NULL DEFAULT 0 COMMENT '排序,小的在前',
  visible_roles VARCHAR(255) NOT NULL DEFAULT '全部' COMMENT '可见角色,逗号分隔',
  status        TINYINT NOT NULL DEFAULT 1 COMMENT '1上架 0下架',
  created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_app_sort (sort_no, app_id),
  KEY idx_app_agent (agent_id)
) COMMENT='智能体广场应用'
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="补建广场应用表（任务 3.3，幂等）")
    parser.add_argument("--dry-run", action="store_true", help="只打印 DDL，不执行")
    args = parser.parse_args()

    from sqlalchemy import text

    from app.config import settings
    from app.db.session import db_session

    print("=" * 68)
    print("迁移（任务 3.3：app，幂等）")
    print(f"存储模式：{settings.store_label}")
    print("=" * 68)

    if settings.store_label != "mysql":
        print("当前不是 MySQL 模式，无需迁移（JSON 模式无表结构）。")
        return 0

    if args.dry_run:
        print(CREATE_APP.strip())
        return 0

    try:
        with db_session() as conn:
            live = {str(r[0]) for r in conn.execute(text("SHOW TABLES")).all()}
    except Exception as exc:  # noqa: BLE001
        print(f"无法读取库结构：{type(exc).__name__}: {exc}")
        print("请确认 MySQL 已启动、库已按 schema.sql 建表。")
        return 1

    if "app" in live:
        print("\napp：已存在，跳过")
        return 0

    print("\napp：不存在，创建…")
    try:
        with db_session() as conn:
            conn.execute(text(CREATE_APP))
    except Exception as exc:  # noqa: BLE001
        print(f"创建失败：{type(exc).__name__}: {exc}")
        return 1
    print("\n完成：新建 1 张表。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
