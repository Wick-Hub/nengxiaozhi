"""为已有 MySQL 库补 ``agent`` 表的任务 2.2 新增列（幂等）。

任务 2.2（M-A-03 助手基础配置）在 ``agent`` 表上新增四列：
``description`` / ``stream_enabled`` / ``risk_reply`` / ``fallback_reply``。
``database/schema.sql`` 已同步更新（新建库直接带这四列），但**已经建好的库**
需要一次 ALTER；MySQL 8 不支持 ``ADD COLUMN IF NOT EXISTS``，因此这里先
``SHOW COLUMNS`` 再按差集补列，重复执行安全。

用法::

    python tools/migrate_agent_2_2.py            # 检查并补列
    python tools/migrate_agent_2_2.py --dry-run  # 只打印将要执行的 DDL

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
REQUIRED_COLUMNS: dict[str, str] = {
    "description": "VARCHAR(255) NOT NULL DEFAULT '' COMMENT '助手描述(M-A-03)'",
    "stream_enabled": "TINYINT NOT NULL DEFAULT 1 COMMENT '1流式输出 0一次性返回(M-A-03)'",
    "risk_reply": "VARCHAR(512) NOT NULL DEFAULT '' COMMENT '风控话术:命中敏感词时的固定回复(M-A-03)'",
    "fallback_reply": "VARCHAR(512) NOT NULL DEFAULT '' COMMENT '兜底话术:知识库未命中时的固定回复(M-A-03)'",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="agent 表任务 2.2 列迁移（幂等）")
    parser.add_argument("--dry-run", action="store_true", help="只打印 DDL，不执行")
    args = parser.parse_args()

    from sqlalchemy import text

    from app.config import settings
    from app.db.session import db_session

    print("=" * 68)
    print("agent 表迁移（任务 2.2：描述 / 流式启停 / 风控话术 / 兜底话术）")
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
        print("\n四列均已存在，无需迁移。")
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
    print(f"\n迁移后列数：{len(after)}；四列齐备：{'是' if ok else '否'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
