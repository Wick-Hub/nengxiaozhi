"""补建会话访问设备列（任务 3.10 / M-T-03，幂等）。

``database/schema.sql`` 的 ``chat_session`` 表已加入 ``device``；本脚本给 3.10
之前建好的老库补列，重复运行安全。JSON 模式无需迁移。

用法::

    python tools/migrate_session_device_3_10.py
    python tools/migrate_session_device_3_10.py --dry-run
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

#: 与 database/schema.sql 保持一致；放在 role_snapshot 之后与统计字段相邻
ALTER_DEVICE = (
    "ALTER TABLE chat_session ADD COLUMN device VARCHAR(16) NOT NULL DEFAULT '' "
    "COMMENT '访问设备(M-T-03)：pc/mobile/tablet/other,空=未采集' "
    "AFTER role_snapshot"
)


def main() -> int:
    parser = argparse.ArgumentParser(description="补建会话访问设备列（任务 3.10，幂等）")
    parser.add_argument("--dry-run", action="store_true", help="只打印 DDL，不执行")
    args = parser.parse_args()

    from sqlalchemy import text

    from app.config import settings
    from app.db.session import db_session

    print("=" * 68)
    print("迁移（任务 3.10：chat_session.device，幂等）")
    print(f"存储模式：{settings.store_label}")
    print("=" * 68)

    if settings.store_label != "mysql":
        print("当前不是 MySQL 模式，无需迁移（JSON 模式无表结构）。")
        return 0

    if args.dry_run:
        print(ALTER_DEVICE)
        return 0

    try:
        with db_session() as conn:
            live = {str(r[0]) for r in conn.execute(text("SHOW TABLES")).all()}
    except Exception as exc:  # noqa: BLE001
        print(f"无法读取库结构：{type(exc).__name__}: {exc}")
        print("请确认 MySQL 已启动、库已按 schema.sql 建表。")
        return 1

    if "chat_session" not in live:
        print("\nchat_session：表不存在，请先执行批1 建表脚本。")
        return 1

    try:
        with db_session() as conn:
            columns = {
                str(r[0]) for r in conn.execute(text("SHOW COLUMNS FROM chat_session")).all()
            }
    except Exception as exc:  # noqa: BLE001
        print(f"读取 chat_session 表结构失败：{type(exc).__name__}: {exc}")
        return 1

    if "device" in columns:
        print("\nchat_session.device：已存在，跳过")
        return 0

    print("\nchat_session.device：不存在，补列…")
    try:
        with db_session() as conn:
            conn.execute(text(ALTER_DEVICE))
    except Exception as exc:  # noqa: BLE001
        print(f"补列失败：{type(exc).__name__}: {exc}")
        return 1
    print("\n完成：chat_session 新增 1 列。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
