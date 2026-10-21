"""补建消息拒答标记列（任务 3.10 / M-T-01，幂等）。

统计「未答」需要与会话日志同口径，故把 ``refused`` 持久化到 ``chat_message``，
不再依赖 ``unrecognized_question`` 反推。老库补列后历史消息默认 0，重复运行安全；
JSON 模式无需迁移（消息本身已含 ``refused``）。

用法::

    python tools/migrate_message_refused_3_10.py
    python tools/migrate_message_refused_3_10.py --dry-run
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

#: 与 database/schema.sql 保持一致；放在 confidence 之后便于人工核对
ALTER_REFUSED = (
    "ALTER TABLE chat_message ADD COLUMN refused TINYINT NOT NULL DEFAULT 0 "
    "COMMENT 'AI消息是否拒答/未答(M-T-01),用户消息恒0' "
    "AFTER confidence"
)


def main() -> int:
    parser = argparse.ArgumentParser(description="补建消息拒答标记列（任务 3.10，幂等）")
    parser.add_argument("--dry-run", action="store_true", help="只打印 DDL，不执行")
    args = parser.parse_args()

    from sqlalchemy import text

    from app.config import settings
    from app.db.session import db_session

    print("=" * 68)
    print("迁移（任务 3.10：chat_message.refused，幂等）")
    print(f"存储模式：{settings.store_label}")
    print("=" * 68)

    if settings.store_label != "mysql":
        print("当前不是 MySQL 模式，无需迁移（JSON 消息已含 refused）。")
        return 0

    if args.dry_run:
        print(ALTER_REFUSED)
        return 0

    try:
        with db_session() as conn:
            live = {str(r[0]) for r in conn.execute(text("SHOW TABLES")).all()}
    except Exception as exc:  # noqa: BLE001
        print(f"无法读取库结构：{type(exc).__name__}: {exc}")
        print("请确认 MySQL 已启动、库已按 schema.sql 建表。")
        return 1

    if "chat_message" not in live:
        print("\nchat_message：表不存在，请先执行批1 建表脚本。")
        return 1

    try:
        with db_session() as conn:
            columns = {
                str(r[0]) for r in conn.execute(text("SHOW COLUMNS FROM chat_message")).all()
            }
    except Exception as exc:  # noqa: BLE001
        print(f"读取 chat_message 表结构失败：{type(exc).__name__}: {exc}")
        return 1

    if "refused" in columns:
        print("\nchat_message.refused：已存在，跳过")
        return 0

    print("\nchat_message.refused：不存在，补列…")
    try:
        with db_session() as conn:
            conn.execute(text(ALTER_REFUSED))
    except Exception as exc:  # noqa: BLE001
        print(f"补列失败：{type(exc).__name__}: {exc}")
        return 1
    print("\n完成：chat_message 新增 1 列。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
