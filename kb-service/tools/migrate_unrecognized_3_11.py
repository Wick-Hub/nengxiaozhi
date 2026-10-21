"""补建未识别问题池的聚合/归因列（任务 3.11，幂等）。

``database/schema.sql`` 的 ``unrecognized_question`` 已加入 3.11 所需列；
本脚本给老库补齐，重复运行安全。JSON 模式使用 ``data/unrecognized.json``，无需迁移。

用法::

    python tools/migrate_unrecognized_3_11.py
    python tools/migrate_unrecognized_3_11.py --dry-run
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

#: 列名 → DDL（顺序与 schema.sql 一致）
COLUMNS = (
    ("msg_id", "ADD COLUMN msg_id BIGINT COMMENT '关联的 AI 消息 msg_id' AFTER handled_at"),
    (
        "question_key",
        "ADD COLUMN question_key VARCHAR(255) NOT NULL DEFAULT '' "
        "COMMENT '归一化问题（聚合键）' AFTER msg_id",
    ),
    (
        "confidence",
        "ADD COLUMN confidence DECIMAL(5,4) COMMENT '回答置信度' AFTER question_key",
    ),
    (
        "source",
        "ADD COLUMN source VARCHAR(20) NOT NULL DEFAULT '' "
        "COMMENT 'weak_recall/refused/low_confidence' AFTER confidence",
    ),
    (
        "role_snapshot",
        "ADD COLUMN role_snapshot VARCHAR(32) NOT NULL DEFAULT '' "
        "COMMENT '提问者角色快照' AFTER source",
    ),
    (
        "channel_code",
        "ADD COLUMN channel_code VARCHAR(64) NOT NULL DEFAULT '' "
        "COMMENT '来源渠道编码' AFTER role_snapshot",
    ),
    (
        "agent_id",
        "ADD COLUMN agent_id VARCHAR(64) NOT NULL DEFAULT '' "
        "COMMENT '回答问题时的助手 ID' AFTER channel_code",
    ),
    (
        "dept_name",
        "ADD COLUMN dept_name VARCHAR(64) NOT NULL DEFAULT '' "
        "COMMENT '归属部门/业务域（人工归类）' AFTER agent_id",
    ),
    (
        "new_doc_id",
        "ADD COLUMN new_doc_id BIGINT COMMENT '反哺生成的知识文档 doc_id' AFTER dept_name",
    ),
)

INDEXES = (
    ("idx_question_key", "ADD INDEX idx_question_key (question_key)"),
    ("idx_source", "ADD INDEX idx_source (source)"),
    ("idx_created", "ADD INDEX idx_created (created_at)"),
)

TABLE = "unrecognized_question"


def main() -> int:
    parser = argparse.ArgumentParser(description="补建未识别问题池 3.11 列（幂等）")
    parser.add_argument("--dry-run", action="store_true", help="只打印 DDL，不执行")
    args = parser.parse_args()

    from sqlalchemy import text

    from app.config import settings
    from app.db.session import db_session

    print("=" * 68)
    print("迁移（任务 3.11：unrecognized_question 聚合/归因列，幂等）")
    print(f"存储模式：{settings.store_label}")
    print("=" * 68)

    if settings.store_label != "mysql":
        print("当前不是 MySQL 模式，无需迁移（JSON 模式使用 data/unrecognized.json）。")
        return 0

    statements = [f"ALTER TABLE {TABLE} {ddl}" for _, ddl in COLUMNS]
    statements += [f"ALTER TABLE {TABLE} {ddl}" for _, ddl in INDEXES]
    if args.dry_run:
        print("\n".join(statements))
        return 0

    try:
        with db_session() as conn:
            live = {str(r[0]) for r in conn.execute(text("SHOW TABLES")).all()}
            if TABLE not in live:
                print(f"\n{TABLE}：表不存在，请先执行批1 建表脚本。")
                return 1
            columns = {
                str(r[0]) for r in conn.execute(text(f"SHOW COLUMNS FROM {TABLE}")).all()
            }
            indexes = {
                str(r[2]) for r in conn.execute(text(f"SHOW INDEX FROM {TABLE}")).all()
            }
    except Exception as exc:  # noqa: BLE001
        print(f"无法读取库结构：{type(exc).__name__}: {exc}")
        print("请确认 MySQL 已启动、库已按 schema.sql 建表。")
        return 1

    added = 0
    for name, ddl in COLUMNS:
        if name in columns:
            print(f"{TABLE}.{name}：已存在，跳过")
            continue
        print(f"{TABLE}.{name}：补列…")
        try:
            with db_session() as conn:
                conn.execute(text(f"ALTER TABLE {TABLE} {ddl}"))
            added += 1
        except Exception as exc:  # noqa: BLE001
            print(f"补列失败（{name}）：{type(exc).__name__}: {exc}")
            return 1

    for name, ddl in INDEXES:
        if name in indexes:
            print(f"{TABLE}.{name}：索引已存在，跳过")
            continue
        print(f"{TABLE}.{name}：补索引…")
        try:
            with db_session() as conn:
                conn.execute(text(f"ALTER TABLE {TABLE} {ddl}"))
            added += 1
        except Exception as exc:  # noqa: BLE001
            print(f"补索引失败（{name}）：{type(exc).__name__}: {exc}")
            return 1

    print(f"\n完成：新增 {added} 项。历史记录 question_key 为空时，读取侧会自动归一化。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
