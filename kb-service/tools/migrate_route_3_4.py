"""补建渠道路由列（任务 3.4 / M-C-07，幂等）。

``database/schema.sql`` 的 ``channel`` 表已加入 ``route_config``；本脚本给 3.4
之前建好的老库补列，重复运行安全。JSON 模式无需迁移。

用法::

    python tools/migrate_route_3_4.py
    python tools/migrate_route_3_4.py --dry-run
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

#: 与 database/schema.sql 保持一致；AFTER theme_config 便于与门户配置相邻查看
ALTER_ROUTE = (
    "ALTER TABLE channel ADD COLUMN route_config JSON "
    "COMMENT '渠道×角色路由:助手/欢迎语/引导问题/模块开关(M-C-07)' "
    "AFTER theme_config"
)


def main() -> int:
    parser = argparse.ArgumentParser(description="补建渠道路由列（任务 3.4，幂等）")
    parser.add_argument("--dry-run", action="store_true", help="只打印 DDL，不执行")
    args = parser.parse_args()

    from sqlalchemy import text

    from app.config import settings
    from app.db.session import db_session

    print("=" * 68)
    print("迁移（任务 3.4：channel.route_config，幂等）")
    print(f"存储模式：{settings.store_label}")
    print("=" * 68)

    if settings.store_label != "mysql":
        print("当前不是 MySQL 模式，无需迁移（JSON 模式无表结构）。")
        return 0

    if args.dry_run:
        print(ALTER_ROUTE)
        return 0

    try:
        with db_session() as conn:
            live = {str(r[0]) for r in conn.execute(text("SHOW TABLES")).all()}
    except Exception as exc:  # noqa: BLE001
        print(f"无法读取库结构：{type(exc).__name__}: {exc}")
        print("请确认 MySQL 已启动、库已按 schema.sql 建表。")
        return 1

    if "channel" not in live:
        print("\nchannel：表不存在，请先执行 tools/migrate_channel_3_1.py。")
        return 1

    try:
        with db_session() as conn:
            columns = {
                str(r[0])
                for r in conn.execute(text("SHOW COLUMNS FROM channel")).all()
            }
    except Exception as exc:  # noqa: BLE001
        print(f"读取 channel 表结构失败：{type(exc).__name__}: {exc}")
        return 1

    if "route_config" in columns:
        print("\nchannel.route_config：已存在，跳过")
        return 0

    print("\nchannel.route_config：不存在，补列…")
    try:
        with db_session() as conn:
            conn.execute(text(ALTER_ROUTE))
    except Exception as exc:  # noqa: BLE001
        print(f"补列失败：{type(exc).__name__}: {exc}")
        return 1
    print("\n完成：channel 新增 1 列。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
