"""补建助手场景引导问题列（任务 3.8 / P-QA-06，幂等）。

``database/schema.sql`` 的 ``agent`` 表已加入 ``guide_questions``；本脚本给 3.8
之前建好的老库补列，重复运行安全。JSON 模式无需迁移。

用法::

    python tools/migrate_agent_guide_3_8.py
    python tools/migrate_agent_guide_3_8.py --dry-run
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

#: 与 database/schema.sql 保持一致；AFTER retrieval_config 便于与检索配置相邻查看
ALTER_GUIDE = (
    "ALTER TABLE agent ADD COLUMN guide_questions JSON "
    "COMMENT '场景引导问题(P-QA-06多智能体切换)：最多6条、单条≤120字;空=不单独推荐' "
    "AFTER retrieval_config"
)


def main() -> int:
    parser = argparse.ArgumentParser(description="补建助手引导问题列（任务 3.8，幂等）")
    parser.add_argument("--dry-run", action="store_true", help="只打印 DDL，不执行")
    args = parser.parse_args()

    from sqlalchemy import text

    from app.config import settings
    from app.db.session import db_session

    print("=" * 68)
    print("迁移（任务 3.8：agent.guide_questions，幂等）")
    print(f"存储模式：{settings.store_label}")
    print("=" * 68)

    if settings.store_label != "mysql":
        print("当前不是 MySQL 模式，无需迁移（JSON 模式无表结构）。")
        return 0

    if args.dry_run:
        print(ALTER_GUIDE)
        return 0

    try:
        with db_session() as conn:
            live = {str(r[0]) for r in conn.execute(text("SHOW TABLES")).all()}
    except Exception as exc:  # noqa: BLE001
        print(f"无法读取库结构：{type(exc).__name__}: {exc}")
        print("请确认 MySQL 已启动、库已按 schema.sql 建表。")
        return 1

    if "agent" not in live:
        print("\nagent：表不存在，请先执行 tools/migrate_agent_2_2.py。")
        return 1

    try:
        with db_session() as conn:
            columns = {str(r[0]) for r in conn.execute(text("SHOW COLUMNS FROM agent")).all()}
    except Exception as exc:  # noqa: BLE001
        print(f"读取 agent 表结构失败：{type(exc).__name__}: {exc}")
        return 1

    if "guide_questions" in columns:
        print("\nagent.guide_questions：已存在，跳过")
        return 0

    print("\nagent.guide_questions：不存在，补列…")
    try:
        with db_session() as conn:
            conn.execute(text(ALTER_GUIDE))
    except Exception as exc:  # noqa: BLE001
        print(f"补列失败：{type(exc).__name__}: {exc}")
        return 1
    print("\n完成：agent 新增 1 列。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
