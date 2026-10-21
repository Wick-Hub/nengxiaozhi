"""补建/升级客服表（任务 4.6 / P-B-09～13、M-S-02～06，幂等）。

``database/schema.sql`` 已给出 4.6 完整结构；存量库在 4.6 之前只有精简版
``agent_seat`` / ``ticket`` / ``ticket_message``，缺 ``support_group`` 以及
分组、凭证、情绪、摘要等字段。本脚本：

1. 补建 ``support_group``；
2. 给三张旧表补齐 4.6 所需字段（逐列判断，重复执行安全）；
3. 去掉 ``agent_seat.user_id`` 的唯一约束（演示坐席的 user_id 均为 0）。
4. 表为空时写入演示分组与坐席（``support_auto_seed`` 关闭则跳过）。

JSON 模式使用 ``data/support.json``，无需迁移。用法::

    python tools/migrate_support_4_6.py
    python tools/migrate_support_4_6.py --dry-run
    python tools/migrate_support_4_6.py --no-seed
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

CREATE_SUPPORT_GROUP = """
CREATE TABLE IF NOT EXISTS support_group (
  group_id     BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '分组ID',
  group_name   VARCHAR(64) NOT NULL COMMENT '分组名称',
  channel_ids  VARCHAR(255) NOT NULL DEFAULT '' COMMENT '渠道编码(逗号分隔),空=全部',
  description  VARCHAR(255) NOT NULL DEFAULT '' COMMENT '分组说明',
  status       TINYINT NOT NULL DEFAULT 1 COMMENT '1启用 0停用',
  created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_support_group_name (group_name)
) COMMENT='客服分组'
"""

# 存量库逐列补齐：列名 -> 列定义（含默认值，保证老数据可用）
COLUMNS: dict[str, dict[str, str]] = {
    "agent_seat": {
        "user_no": "VARCHAR(64) NOT NULL DEFAULT '' COMMENT '坐席学工号'",
        "group_id": "BIGINT NULL COMMENT '所属客服分组'",
        "created_at": "DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP",
        "updated_at": "DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP",
    },
    "ticket": {
        "ticket_no": "VARCHAR(32) NOT NULL DEFAULT '' COMMENT '工单号'",
        "channel_code": "VARCHAR(64) NOT NULL DEFAULT '' COMMENT '来源渠道编码'",
        "user_no": "VARCHAR(64) NOT NULL DEFAULT '' COMMENT '发起用户学工号'",
        "user_name": "VARCHAR(64) NOT NULL DEFAULT '' COMMENT '发起用户显示名'",
        "role": "VARCHAR(32) NOT NULL DEFAULT '' COMMENT '用户角色'",
        "group_id": "BIGINT NULL COMMENT '客服分组'",
        "sentiment": "TINYINT NOT NULL DEFAULT 0 COMMENT '0正常 1负面'",
        "sentiment_label": "VARCHAR(16) NOT NULL DEFAULT '正常'",
        "subject": "VARCHAR(255) NOT NULL DEFAULT '' COMMENT '问题摘要'",
        "access_key_hash": "VARCHAR(64) NOT NULL DEFAULT '' COMMENT '访问凭证SHA-256'",
        "accepted_at": "DATETIME NULL COMMENT '坐席接入时间'",
        "last_message_at": "DATETIME NULL COMMENT '最后消息时间'",
        "close_reason": "VARCHAR(255) NOT NULL DEFAULT ''",
        "updated_at": "DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP",
    },
    "ticket_message": {
        "sender_name": "VARCHAR(64) NOT NULL DEFAULT '' COMMENT '发送人显示名'",
        "sentiment": "TINYINT NOT NULL DEFAULT 0 COMMENT '0正常 1负面'",
    },
}

INDEXES: dict[str, dict[str, str]] = {
    "agent_seat": {
        "idx_seat_group": "KEY idx_seat_group (group_id)",
        "idx_seat_user_no": "KEY idx_seat_user_no (user_no)",
    },
    "ticket": {
        "idx_ticket_no": "KEY idx_ticket_no (ticket_no)",
        "idx_ticket_group": "KEY idx_ticket_group (group_id)",
    },
}

# 4.6 之前 agent_seat 以 user_id 唯一约束标识坐席；演示坐席 user_id=0 会冲突
DROP_INDEXES = {"agent_seat": ("uk_user",)}

DEFAULT_GROUPS = (
    ("综合客服组", "", "默认分组，承接全部渠道的转人工请求。", 1),
    ("教务客服组", "jw,academic", "课表、成绩、学籍等教务事务。", 1),
)
DEFAULT_SEATS = (
    ("seat-demo", "演示坐席·小智", "综合客服组", 5, 1),
    ("seat-backup", "演示坐席·小云", "综合客服组", 5, 0),
    ("seat-jw", "教务坐席·小林", "教务客服组", 5, 0),
)


def _columns(conn, table: str) -> set[str]:
    from sqlalchemy import text

    rows = conn.execute(
        text(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table"
        ),
        {"table": table},
    ).all()
    return {str(row[0]) for row in rows}


def _indexes(conn, table: str) -> set[str]:
    from sqlalchemy import text

    rows = conn.execute(
        text(
            "SELECT DISTINCT INDEX_NAME FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table"
        ),
        {"table": table},
    ).all()
    return {str(row[0]) for row in rows}


def _seed(conn) -> tuple[int, int]:
    from sqlalchemy import text

    groups = 0
    for name, channels, desc, status in DEFAULT_GROUPS:
        res = conn.execute(
            text(
                "INSERT INTO support_group (group_name, channel_ids, description, status) "
                "SELECT :name, :channels, :desc, :status FROM DUAL "
                "WHERE NOT EXISTS (SELECT 1 FROM support_group WHERE group_name = :name)"
            ),
            {"name": name, "channels": channels, "desc": desc, "status": status},
        )
        groups += int(res.rowcount or 0)

    seats = 0
    for user_no, seat_name, group_name, max_sessions, online in DEFAULT_SEATS:
        res = conn.execute(
            text(
                "INSERT INTO agent_seat "
                "(user_id, user_no, seat_name, group_id, max_sessions, online_flag) "
                "SELECT 0, :user_no, :seat_name, "
                "(SELECT group_id FROM support_group WHERE group_name = :group_name), "
                ":max_sessions, :online FROM DUAL "
                "WHERE NOT EXISTS (SELECT 1 FROM agent_seat WHERE user_no = :user_no)"
            ),
            {
                "user_no": user_no,
                "seat_name": seat_name,
                "group_name": group_name,
                "max_sessions": max_sessions,
                "online": online,
            },
        )
        seats += int(res.rowcount or 0)
    return groups, seats


def main() -> int:
    parser = argparse.ArgumentParser(description="补建/升级客服表（任务 4.6，幂等）")
    parser.add_argument("--dry-run", action="store_true", help="只打印 DDL，不执行")
    parser.add_argument("--no-seed", action="store_true", help="不写入演示分组/坐席")
    args = parser.parse_args()

    from sqlalchemy import text

    from app.config import settings
    from app.db.session import db_session

    print("=" * 68)
    print("迁移（任务 4.6：客服分组 / 坐席 / 工单，幂等）")
    print(f"存储模式：{settings.store_label}")
    print("=" * 68)

    if settings.store_label != "mysql":
        print("当前不是 MySQL 模式，无需迁移（JSON 模式使用 data/support.json）。")
        return 0

    if args.dry_run:
        print(CREATE_SUPPORT_GROUP.strip() + ";\n")
        for table, cols in COLUMNS.items():
            for name, ddl in cols.items():
                print(f"ALTER TABLE {table} ADD COLUMN {name} {ddl};")
        for table, idx in INDEXES.items():
            for ddl in idx.values():
                print(f"ALTER TABLE {table} ADD {ddl};")
        print("ALTER TABLE agent_seat DROP INDEX uk_user;  -- 仅当存在")
        return 0

    try:
        with db_session() as conn:
            live = {str(r[0]) for r in conn.execute(text("SHOW TABLES")).all()}
    except Exception as exc:  # noqa: BLE001
        print(f"无法连接数据库：{type(exc).__name__}: {exc}")
        print("请确认 MySQL 已启动、库已按 schema.sql 建表。")
        return 1

    missing = {"agent_seat", "ticket", "ticket_message"} - live
    if missing:
        print(f"缺少基础表：{', '.join(sorted(missing))}；请先导入 database/schema.sql。")
        return 1

    added = 0
    try:
        with db_session() as conn:
            if "support_group" not in live:
                print("support_group：建表中…")
                conn.execute(text(CREATE_SUPPORT_GROUP))
            else:
                print("support_group：已存在，跳过")

            for table, cols in COLUMNS.items():
                present = _columns(conn, table)
                for name, ddl in cols.items():
                    if name in present:
                        continue
                    print(f"{table}.{name}：补列…")
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                    added += 1

            for table, idx in INDEXES.items():
                present = _indexes(conn, table)
                for name, ddl in idx.items():
                    if name in present:
                        continue
                    print(f"{table}.{name}：补索引…")
                    conn.execute(text(f"ALTER TABLE {table} ADD {ddl}"))

            for table, names in DROP_INDEXES.items():
                present = _indexes(conn, table)
                for name in names:
                    if name in present:
                        print(f"{table}.{name}：移除旧唯一约束…")
                        conn.execute(text(f"ALTER TABLE {table} DROP INDEX {name}"))
    except Exception as exc:  # noqa: BLE001
        print(f"迁移失败：{type(exc).__name__}: {exc}")
        return 1

    seeded_groups = seeded_seats = 0
    if args.no_seed or not settings.support_auto_seed:
        print("\n演示数据：已按参数/配置跳过。")
    else:
        try:
            with db_session() as conn:
                seeded_groups, seeded_seats = _seed(conn)
        except Exception as exc:  # noqa: BLE001
            print(f"演示数据写入失败：{type(exc).__name__}: {exc}")
            return 1
        print(f"\n演示数据：新增分组 {seeded_groups} 个、坐席 {seeded_seats} 个。")

    print(f"完成：补列 {added} 个。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
