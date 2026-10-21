"""为已有 MySQL 库补 ``sensitive_word`` 表（任务 2.10 / M-SYS-07，幂等）。

``database/schema.sql`` 已同步该表（新建库直接带），已建好的库需要一次迁移。
MySQL 没有 ``CREATE TABLE IF NOT EXISTS`` 之外的列级条件 DDL，这里先
``SHOW TABLES`` 取差集，只对缺的表执行建表，重复运行安全。

用法::

    python tools/migrate_sensitive_2_10.py
    python tools/migrate_sensitive_2_10.py --dry-run
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
CREATE_SENSITIVE_WORD = """
CREATE TABLE IF NOT EXISTS sensitive_word (
  word_id      BIGINT PRIMARY KEY AUTO_INCREMENT,
  word         VARCHAR(128) NOT NULL COMMENT '词条',
  kind         VARCHAR(16) NOT NULL DEFAULT 'sensitive' COMMENT 'sensitive敏感词 / keyword关键词',
  category     VARCHAR(32) NOT NULL DEFAULT '自定义' COMMENT '分类',
  status       TINYINT NOT NULL DEFAULT 1 COMMENT '1启用 0停用',
  remark       VARCHAR(255) NOT NULL DEFAULT '',
  created_by   BIGINT COMMENT '创建人',
  created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_word (word),
  KEY idx_kind_status (kind, status)
) COMMENT='敏感词/关键词库'
"""

TABLES = {"sensitive_word": CREATE_SENSITIVE_WORD}


def main() -> int:
    parser = argparse.ArgumentParser(description="补建 sensitive_word（任务 2.10，幂等）")
    parser.add_argument("--dry-run", action="store_true", help="只打印 DDL，不执行")
    args = parser.parse_args()

    from sqlalchemy import text

    from app.config import settings
    from app.db.session import db_session

    print("=" * 68)
    print("迁移（任务 2.10：sensitive_word 补表，幂等）")
    print(f"存储模式：{settings.store_label}")
    print("=" * 68)

    if settings.store_label != "mysql":
        print("当前不是 MySQL 模式，无需迁移（JSON 模式无表结构）。")
        return 0

    try:
        with db_session() as conn:
            live = {str(r[0]) for r in conn.execute(text("SHOW TABLES")).all()}
    except Exception as exc:  # noqa: BLE001
        print(f"无法读取库结构：{type(exc).__name__}: {exc}")
        print("请确认 MySQL 已启动、库已按 schema.sql 建表。")
        return 1

    created = 0
    for name, ddl in TABLES.items():
        if name in live:
            print(f"\n{name}：已存在，跳过")
            continue
        print(f"\n{name}：缺失，将创建")
        # text() 会把 COMMENT 里的半角冒号当 bind 参数，执行前先编译校验
        stmt = text(ddl)
        params = stmt.compile().params
        if params:
            print(f"  ! DDL 解析出意外 bind 参数 {sorted(params)}，已中止（请检查 COMMENT）")
            return 1
        if args.dry_run:
            print(ddl.strip())
            continue
        with db_session() as conn:
            conn.execute(stmt)
            conn.commit()
        print(f"  ✓ {name} 已创建")
        created += 1

    if args.dry_run:
        print("\n（--dry-run：未执行任何 DDL）")
        return 0

    with db_session() as conn:
        after = {str(r[0]) for r in conn.execute(text("SHOW TABLES")).all()}
    ok = all(name in after for name in TABLES)
    print(f"\n迁移后：sensitive_word 就位={'是' if ok else '否'}（本次新建 {created} 张）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
