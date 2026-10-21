"""为已有 MySQL 库补 ``kb_category`` / ``kb_permission`` 两张表（任务 2.5 / 2.6，幂等）。

``database/schema.sql`` 已同步这两项（新建库直接带），但**已经建好的库**需要一次迁移——
本机就踩过： schema 加了 ``kb_permission`` 而线上库没建，管理页一调
``/api/admin/kb-permissions`` 就是 ``Table 'nengxiaozhi.kb_permission' doesn't exist``。

MySQL 的条件 DDL 能力很弱（没有 ``ADD COLUMN IF NOT EXISTS``），因此这里先
``SHOW TABLES`` 取差集，只对缺的表执行 ``CREATE TABLE IF NOT EXISTS``，
重复运行安全。

用法::

    python tools/migrate_kb_2_5_2_6.py            # 检查并迁移
    python tools/migrate_kb_2_5_2_6.py --dry-run  # 只打印将要执行的 DDL

前置：``KB_STORE=mysql`` 且数据库可连接（否则脚本无意义，直接退出 0）。
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

#: 新建表 DDL（与 database/schema.sql 逐字一致，仅把 CREATE TABLE 换成 IF NOT EXISTS）
TABLES: dict[str, str] = {
    "kb_category": """
CREATE TABLE IF NOT EXISTS kb_category (
  category_id  BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '分类ID',
  dept_id      BIGINT NOT NULL COMMENT '所属部门(sys_org)',
  ktype        VARCHAR(32) NOT NULL COMMENT 'policy政策/guide指南/faq问答/item事项/org机构',
  category_name VARCHAR(128) NOT NULL COMMENT '分类名称',
  parent_id    BIGINT NOT NULL DEFAULT 0,
  sort_no      INT NOT NULL DEFAULT 0,
  KEY idx_dept_ktype (dept_id, ktype)
) COMMENT='知识分类'
""",
    "kb_permission": """
CREATE TABLE IF NOT EXISTS kb_permission (
  perm_id         BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '授权ID',
  user_id         BIGINT NOT NULL COMMENT '被授权用户(sys_user,部门联络员)',
  dept_id         BIGINT NOT NULL DEFAULT 0 COMMENT '授权部门(0=全部部门)',
  ktype           VARCHAR(32) NOT NULL DEFAULT '*' COMMENT '授权知识类型(*=全部)',
  can_upload      TINYINT NOT NULL DEFAULT 0 COMMENT '可上传',
  can_view        TINYINT NOT NULL DEFAULT 0 COMMENT '可查看',
  can_download    TINYINT NOT NULL DEFAULT 0 COMMENT '可下载',
  can_delete      TINYINT NOT NULL DEFAULT 0 COMMENT '可删除',
  can_create_sub  TINYINT NOT NULL DEFAULT 0 COMMENT '可建子分类',
  created_by     BIGINT COMMENT '授权人(super_admin)',
  created_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_perm (user_id, dept_id, ktype),
  KEY idx_perm_user (user_id),
  KEY idx_perm_dept (dept_id)
) COMMENT='知识库部门授权'
""",
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="补建 kb_category / kb_permission（任务 2.5 / 2.6，幂等）"
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印 DDL，不执行")
    args = parser.parse_args()

    from sqlalchemy import text

    from app.config import settings
    from app.db.session import db_session

    print("=" * 68)
    print("迁移（任务 2.5 / 2.6：kb_category + kb_permission 补表，幂等）")
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
    print(f"\n迁移后：两张表就位={'是' if ok else '否'}（本次新建 {created} 张）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
