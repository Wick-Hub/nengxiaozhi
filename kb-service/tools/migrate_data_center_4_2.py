"""建数据中台任务/批次/水位表（任务 4.2，幂等）。

``database/schema.sql`` 已包含三张表；本脚本给存量库补建，重复运行安全。
组织与用户同步直接复用既有 ``sys_org`` / ``sys_user``，无需新表。
JSON 模式使用 ``data/data_center.json``，无需迁移。

用法::

    python tools/migrate_data_center_4_2.py
    python tools/migrate_data_center_4_2.py --dry-run
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

DDL: dict[str, str] = {
    "data_center_job": """
        CREATE TABLE IF NOT EXISTS data_center_job (
          job_id        BIGINT PRIMARY KEY AUTO_INCREMENT,
          job_type      VARCHAR(32) NOT NULL COMMENT 'report上报 / org_sync组织同步',
          mode          VARCHAR(32) NOT NULL DEFAULT '' COMMENT 'chatlog/stats/all 或 incremental/full',
          status        VARCHAR(16) NOT NULL DEFAULT 'running' COMMENT 'running/success/failed',
          started_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          finished_at   DATETIME,
          total         INT NOT NULL DEFAULT 0 COMMENT '本任务处理记录数',
          created_count INT NOT NULL DEFAULT 0,
          updated_count INT NOT NULL DEFAULT 0,
          skipped_count INT NOT NULL DEFAULT 0,
          failed_count  INT NOT NULL DEFAULT 0,
          checksum      VARCHAR(64) NOT NULL DEFAULT '' COMMENT '结果校验和',
          detail        JSON COMMENT '批次明细/同步计数',
          error         VARCHAR(512) NOT NULL DEFAULT '',
          KEY idx_dc_job_started (started_at),
          KEY idx_dc_job_type (job_type, status)
        ) COMMENT='数据中台上报/同步任务'
    """,
    "data_center_report": """
        CREATE TABLE IF NOT EXISTS data_center_report (
          report_id   BIGINT PRIMARY KEY AUTO_INCREMENT,
          batch_id    VARCHAR(64) NOT NULL COMMENT 'DC-CHATLOG-YYYYMMDD 等',
          kind        VARCHAR(16) NOT NULL DEFAULT 'chatlog' COMMENT 'chatlog/stats',
          report_date DATE NOT NULL,
          revision    INT NOT NULL DEFAULT 1,
          checksum    VARCHAR(64) NOT NULL DEFAULT '',
          status      VARCHAR(16) NOT NULL DEFAULT 'pending' COMMENT 'pending/delivered/failed',
          rows_count  INT NOT NULL DEFAULT 0,
          payload     MEDIUMTEXT COMMENT 'nxz.data-center.report.v1 报文',
          error       VARCHAR(512) NOT NULL DEFAULT '',
          created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          UNIQUE KEY uk_dc_batch (batch_id),
          KEY idx_dc_report_date (report_date)
        ) COMMENT='数据中台上报批次'
    """,
    "data_center_state": """
        CREATE TABLE IF NOT EXISTS data_center_state (
          state_key   VARCHAR(64) PRIMARY KEY,
          state_value VARCHAR(1024) NOT NULL DEFAULT '',
          updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) COMMENT='数据中台任务水位'
    """,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="补建数据中台三张表（幂等）")
    parser.add_argument("--dry-run", action="store_true", help="只打印 DDL，不执行")
    args = parser.parse_args()

    from sqlalchemy import text

    from app.config import settings
    from app.db.session import db_session

    print("=" * 68)
    print("迁移（任务 4.2：data_center_job / report / state，幂等）")
    print(f"存储模式：{settings.store_label}")
    print("=" * 68)

    if settings.store_label != "mysql":
        print("当前不是 MySQL 模式，无需迁移（JSON 模式使用 data/data_center.json）。")
        return 0

    if args.dry_run:
        for name, ddl in DDL.items():
            print(f"-- {name}\n{ddl.strip()};\n")
        return 0

    try:
        with db_session() as conn:
            live = {str(r[0]) for r in conn.execute(text("SHOW TABLES")).all()}
    except Exception as exc:  # noqa: BLE001
        print(f"无法连接数据库：{type(exc).__name__}: {exc}")
        print("请确认 MySQL 已启动、库已按 schema.sql 建表。")
        return 1

    created = 0
    for name, ddl in DDL.items():
        if name in live:
            print(f"{name}：已存在，跳过")
            continue
        print(f"{name}：建表中…")
        try:
            with db_session() as conn:
                conn.execute(text(ddl))
            created += 1
        except Exception as exc:  # noqa: BLE001
            print(f"建表失败（{name}）：{type(exc).__name__}: {exc}")
            return 1

    print(f"\n完成：新建 {created} 张表。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
