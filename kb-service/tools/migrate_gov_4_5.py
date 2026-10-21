"""导办事项表结构与票据表迁移（任务 4.5，幂等）。

``database/schema.sql`` 已包含 ``gov_item``（含生效时段）/ ``gov_ticket``；
本脚本给存量库补列补表，并可选择导入内置演示事项。

用法::

    python tools/migrate_gov_4_5.py
    python tools/migrate_gov_4_5.py --seed
    python tools/migrate_gov_4_5.py --dry-run

JSON 模式使用 ``data/gov_items.json`` / ``data/gov_tickets.json``，无需迁移；
``--seed`` 会写入 JSON 或 MySQL 当前配置的存储。
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

CREATE_ITEM = """
CREATE TABLE IF NOT EXISTS gov_item (
  item_id        BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '事项ID',
  item_name      VARCHAR(255) NOT NULL COMMENT '事项名称',
  dept_id        BIGINT NOT NULL COMMENT '主管部门',
  hall_url       VARCHAR(512) NOT NULL DEFAULT '' COMMENT '一网通办直达URL',
  need_ticket    TINYINT NOT NULL DEFAULT 1 COMMENT '1需免登录票据',
  conditions     TEXT COMMENT '办理条件',
  materials      TEXT COMMENT '所需材料',
  keyword_tags   VARCHAR(512) DEFAULT '' COMMENT '检索关键词',
  visible_roles  VARCHAR(255) NOT NULL DEFAULT '全部',
  status         TINYINT NOT NULL DEFAULT 1,
  effective_from DATETIME NULL COMMENT '生效开始,空=立即',
  effective_to   DATETIME NULL COMMENT '生效结束,空=长期',
  updated_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_dept (dept_id),
  KEY idx_gov_item_status (status),
  FULLTEXT KEY ft_item (item_name, keyword_tags) WITH PARSER ngram
) COMMENT='导办事项'
"""

CREATE_TICKET = """
CREATE TABLE IF NOT EXISTS gov_ticket (
  ticket_id   VARCHAR(64) PRIMARY KEY COMMENT '高熵随机票据',
  item_id     BIGINT NOT NULL,
  user_no     VARCHAR(64) NOT NULL DEFAULT '',
  user_name   VARCHAR(64) NOT NULL DEFAULT '',
  role        VARCHAR(32) NOT NULL DEFAULT '',
  issued_at   DATETIME NOT NULL,
  expires_at  DATETIME NOT NULL,
  used_at     DATETIME NULL,
  KEY idx_expires (expires_at),
  KEY idx_item (item_id)
) COMMENT='导办免登录票据'
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="导办事项/票据表迁移（幂等）")
    parser.add_argument("--seed", action="store_true", help="迁移后导入内置演示事项")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不执行")
    args = parser.parse_args()

    from sqlalchemy import text

    from app.config import settings
    from app.db.session import db_session, db_transaction

    print("=" * 68)
    print("迁移（任务 4.5：gov_item 生效时段 / gov_ticket，幂等）")
    print(f"存储模式：{settings.store_label}")
    print("=" * 68)

    if settings.store_label != "mysql":
        if args.seed:
            from app.services import gov_admin

            result = gov_admin.sync_items()
            print(f"JSON 模式导入：{result}")
            return 0 if result["ok"] else 1
        print("当前不是 MySQL 模式，无需迁移（JSON 模式使用 data/gov_items.json）。")
        return 0

    if args.dry_run:
        print(f"-- gov_item\n{CREATE_ITEM.strip()};\n")
        print("-- 若 gov_item 已存在但缺少生效时段：")
        print("ALTER TABLE gov_item ADD COLUMN effective_from DATETIME NULL COMMENT '生效开始,空=立即' AFTER status;")
        print("ALTER TABLE gov_item ADD COLUMN effective_to DATETIME NULL COMMENT '生效结束,空=长期' AFTER effective_from;\n")
        print(f"-- gov_ticket\n{CREATE_TICKET.strip()};")
        return 0

    try:
        with db_session() as conn:
            tables = {str(r[0]) for r in conn.execute(text("SHOW TABLES")).all()}
            columns = set()
            if "gov_item" in tables:
                columns = {
                    str(r[0])
                    for r in conn.execute(
                        text(
                            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'gov_item'"
                        )
                    ).all()
                }
    except Exception as exc:  # noqa: BLE001
        print(f"无法连接数据库：{type(exc).__name__}: {exc}")
        print("请确认 MySQL 已启动、库已按 schema.sql 建表。")
        return 1

    if "gov_item" not in tables:
        print("gov_item：建表中…")
        try:
            with db_transaction() as conn:
                conn.execute(text(CREATE_ITEM))
        except Exception as exc:  # noqa: BLE001
            print(f"建表失败（gov_item）：{type(exc).__name__}: {exc}")
            return 1
    else:
        print("gov_item：已存在")
        missing = [
            ("effective_from", "DATETIME NULL COMMENT '生效开始,空=立即' AFTER status"),
            ("effective_to", "DATETIME NULL COMMENT '生效结束,空=长期' AFTER effective_from"),
        ]
        for name, definition in missing:
            if name in columns:
                print(f"  - {name}：已存在")
                continue
            print(f"  - {name}：补列中…")
            try:
                with db_transaction() as conn:
                    conn.execute(text(f"ALTER TABLE gov_item ADD COLUMN {name} {definition}"))
            except Exception as exc:  # noqa: BLE001
                print(f"补列失败（{name}）：{type(exc).__name__}: {exc}")
                return 1

    if "gov_ticket" not in tables:
        print("gov_ticket：建表中…")
        try:
            with db_transaction() as conn:
                conn.execute(text(CREATE_TICKET))
        except Exception as exc:  # noqa: BLE001
            print(f"建表失败（gov_ticket）：{type(exc).__name__}: {exc}")
            return 1
    else:
        print("gov_ticket：已存在")

    if args.seed:
        from app.services import gov_admin

        result = gov_admin.sync_items()
        if not result["ok"]:
            print(f"导入失败：{result.get('error')}")
            return 1
        data = result["data"]
        print(
            f"演示事项导入完成：新增 {data['created']} / 更新 {data['updated']} / "
            f"跳过 {data['skipped']}（共 {data['total']}）"
        )

    print("\n完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
