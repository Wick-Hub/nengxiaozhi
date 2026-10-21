"""为已有 MySQL 库补 ``agent.retrieval_config`` 列（任务 2.3 / M-A-05，幂等）。

``database/schema.sql`` 已同步该列（新建库直接带），但**已经建好的库**需要一次 ALTER。
MySQL 8 不支持 ``ADD COLUMN IF NOT EXISTS``，因此这里先 ``SHOW COLUMNS`` 再按差集补列，
重复执行安全。

该列为 ``JSON`` 且**允许 NULL**：不设默认值是有意的——MySQL 的 JSON 列不支持普通
字面量默认（只支持 ``DEFAULT (JSON_OBJECT())`` 这类表达式默认），而「NULL 与 {} 等价」
正是业务要的语义（缺省即沿用全局检索参数）。

用法::

    python tools/migrate_agent_2_3.py            # 检查并补列
    python tools/migrate_agent_2_3.py --dry-run  # 只打印将要执行的 DDL

前置：``KB_STORE=mysql`` 且数据库可连接（否则本脚本无意义，直接退出 0）。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

os.environ.setdefault("KB_STORE", "mysql")

try:  # pragma: no cover
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

#: 列名 → 列定义（常量，不来自外部输入）
#:
#: ⚠ **COMMENT 里不能用半角冒号紧跟单词**（如 ``(M-A-05):topK``）：
#: 这段 DDL 经 SQLAlchemy ``text()`` 执行，而它的 bind 参数正则是
#: ``(?<![:\w]):(\w+)`` —— 冒号前是 ``)`` 时会把 ``topK`` 当成参数，
#: 抛 ``A value is required for bind parameter 'topK'``。
#: （同理注意：冒号前若是中文则**不会**被匹配，因为 ``\w`` 含中文——
#: 所以这个坑很隐蔽，只有冒号前是 ``)``/空白等符号时才触发。）
#: 这里改用全角冒号规避，`:data:`database/schema.sql` 里保持同一文案。
REQUIRED_COLUMNS: dict[str, str] = {
    "retrieval_config": (
        "JSON COMMENT '检索配置(M-A-05)：topK/rerankTopn/minScore/depts/ktypes;缺省即沿用全局'"
    ),
}


def main() -> int:
    parser = argparse.ArgumentParser(description="agent 表任务 2.3 列迁移（幂等）")
    parser.add_argument("--dry-run", action="store_true", help="只打印 DDL，不执行")
    args = parser.parse_args()

    from sqlalchemy import text

    from app.config import settings
    from app.db.session import db_session

    print("=" * 68)
    print("agent 表迁移（任务 2.3：检索配置 retrieval_config）")
    print(f"存储模式：{settings.store_label}")
    print("=" * 68)

    if settings.store_label != "mysql":
        print("当前不是 MySQL 模式，无需迁移（JSON 模式字段天然存在）。")
        return 0

    try:
        with db_session() as conn:
            rows = conn.execute(text("SHOW COLUMNS FROM agent")).all()
            existing = {str(r[0]) for r in rows}
    except Exception as exc:  # noqa: BLE001
        print(f"无法读取 agent 表结构：{type(exc).__name__}: {exc}")
        print("请确认 MySQL 已启动、库已按 schema.sql 建表。")
        return 1

    print(f"现有列 {len(existing)} 个：{', '.join(sorted(existing))}")
    missing = [c for c in REQUIRED_COLUMNS if c not in existing]
    if not missing:
        print("\nretrieval_config 已存在，无需迁移。")
        return 0

    print(f"\n缺少 {len(missing)} 列：{', '.join(missing)}")
    for col in missing:
        ddl = f"ALTER TABLE agent ADD COLUMN {col} {REQUIRED_COLUMNS[col]}"
        print(f"  {ddl}")
        if args.dry_run:
            continue
        with db_session() as conn:
            conn.execute(text(ddl))
            conn.commit()
        print(f"  ✓ {col} 已添加")

    if args.dry_run:
        print("\n（--dry-run：未执行任何 DDL）")
        return 0

    with db_session() as conn:
        rows = conn.execute(text("SHOW COLUMNS FROM agent")).all()
        after = {str(r[0]) for r in rows}
    ok = all(c in after for c in REQUIRED_COLUMNS)
    print(f"\n迁移后列数：{len(after)}；retrieval_config 就位：{'是' if ok else '否'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
