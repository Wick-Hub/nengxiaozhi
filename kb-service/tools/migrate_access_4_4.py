"""门禁通行流水表迁移（任务 4.4，幂等）。

``database/schema.sql`` 已包含 ``access_event``；本脚本给存量库补表，并可选择
立即拉取内置演示流水。

用法::

    python tools/migrate_access_4_4.py
    python tools/migrate_access_4_4.py --seed
    python tools/migrate_access_4_4.py --dry-run

JSON 模式使用 ``data/access_events.json``，无需迁移；``--seed`` 会写入 JSON 或
MySQL 当前配置的存储。
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

CREATE_EVENT = """
CREATE TABLE IF NOT EXISTS access_event (
  event_id        BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '流水ID',
  event_no        VARCHAR(64) NOT NULL DEFAULT '' COMMENT '上游流水号(幂等)',
  event_time      DATETIME NOT NULL COMMENT '通行时间',
  direction       TINYINT NOT NULL DEFAULT 1 COMMENT '1进入 0离开',
  campus_id       BIGINT NOT NULL DEFAULT 0 COMMENT '校区ID',
  campus_name     VARCHAR(64) NOT NULL DEFAULT '' COMMENT '校区名称',
  college_name    VARCHAR(128) NOT NULL DEFAULT '' COMMENT '学院/单位',
  gate_id         BIGINT NOT NULL DEFAULT 0 COMMENT '门禁点ID',
  gate_name       VARCHAR(64) NOT NULL DEFAULT '' COMMENT '门禁点名称',
  user_no         VARCHAR(64) NOT NULL DEFAULT '' COMMENT '学工号',
  user_name       VARCHAR(64) NOT NULL DEFAULT '' COMMENT '姓名',
  role            VARCHAR(32) NOT NULL DEFAULT '' COMMENT '身份角色',
  abnormal        TINYINT NOT NULL DEFAULT 0 COMMENT '1异常通行',
  abnormal_reason VARCHAR(64) NOT NULL DEFAULT '' COMMENT '异常原因',
  created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '同步入库时间',
  UNIQUE KEY uk_access_event_no (event_no),
  KEY idx_access_time (event_time),
  KEY idx_access_campus (campus_id, event_time),
  KEY idx_access_college (college_name),
  KEY idx_access_gate (gate_id),
  KEY idx_access_user (user_no),
  KEY idx_access_abnormal (abnormal)
) COMMENT='门禁通行流水本地分析库'
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="门禁通行流水表迁移（幂等）")
    parser.add_argument("--seed", action="store_true", help="迁移后同步内置演示流水")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不执行")
    parser.add_argument("--days", type=int, default=30, help="演示流水天数（默认 30）")
    args = parser.parse_args()

    from app.config import settings

    print("=" * 68)
    print("迁移（任务 4.4：access_event，幂等）")
    print(f"存储模式：{settings.store_label}")
    print("=" * 68)

    if settings.store_label != "mysql":
        if args.seed:
            from app.services import access

            result = access.sync_events(args.days)
            print(f"JSON 模式导入：{result}")
            return 0 if result["ok"] else 1
        print("当前不是 MySQL 模式，无需迁移（JSON 模式使用 data/access_events.json）。")
        return 0

    if args.dry_run:
        print(f"{CREATE_EVENT.strip()};")
        return 0

    from sqlalchemy import text

    from app.db.session import db_session, db_transaction

    try:
        with db_session() as conn:
            tables = {str(row[0]) for row in conn.execute(text("SHOW TABLES")).all()}
    except Exception as exc:  # noqa: BLE001
        print(f"无法连接数据库：{type(exc).__name__}: {exc}")
        print("请确认 MySQL 已启动、库已按 schema.sql 建表。")
        return 1

    if "access_event" not in tables:
        print("access_event：建表中…")
        try:
            with db_transaction() as conn:
                conn.execute(text(CREATE_EVENT))
        except Exception as exc:  # noqa: BLE001
            print(f"建表失败（access_event）：{type(exc).__name__}: {exc}")
            return 1
    else:
        print("access_event：已存在")

    if args.seed:
        from app.services import access

        result = access.sync_events(args.days)
        if not result["ok"]:
            print(f"导入失败：{result.get('error')}")
            return 1
        data = result["data"]
        print(
            f"演示流水导入完成：入库 {data['imported']} 条 / 跳过 {data['skipped']} 条，"
            f"区间 {data['from']} ~ {data['to']}"
        )

    print("\n完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
