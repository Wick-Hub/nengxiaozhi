"""为已有 MySQL 库补 2.9 角色权限结构（幂等）。

    python tools/migrate_rbac_2_9.py
    python tools/migrate_rbac_2_9.py --dry-run

做三件事：

1. 建 ``sys_role_permission``（角色 × 权限点矩阵）；
2. 给 ``sys_user`` 补 ``password_hash``（新建库由 ``schema.sql`` 直接带）；
3. 部门联络员首次初始化默认权限点（已有配置不覆盖）。
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
CREATE_ROLE_PERMISSION = """
CREATE TABLE IF NOT EXISTS sys_role_permission (
  id         BIGINT PRIMARY KEY AUTO_INCREMENT,
  role_id    BIGINT NOT NULL,
  perm_code  VARCHAR(64) NOT NULL COMMENT '权限点,如 docs.view/agents.manage',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_role_perm (role_id, perm_code),
  KEY idx_perm (perm_code)
) COMMENT='角色权限矩阵'
"""

TABLES = {"sys_role_permission": CREATE_ROLE_PERMISSION}

REQUIRED_COLUMNS = {
    "password_hash": (
        "VARCHAR(255) COMMENT "
        "'管理账号密码哈希(PBKDF2,任务2.9;NULL=沿用内部演示免密登录)'"
    )
}

TABLE_NAME = "sys_user"


def _seed_liaison_defaults() -> tuple[int, str]:
    from sqlalchemy import func, insert, select

    from app.db import models as M
    from app.db.session import db_transaction
    from app.security import permissions

    with db_transaction() as conn:
        role_id = conn.execute(
            select(M.SysRole.role_id).where(
                M.SysRole.role_code == "dept_liaison", M.SysRole.role_type == 2
            )
        ).scalar()
        if role_id is None:
            return 0, "未找到部门联络员角色（请先运行 tools/seed_auth.py）"
        existing = int(
            conn.execute(
                select(func.count())
                .select_from(M.SysRolePermission)
                .where(M.SysRolePermission.role_id == role_id)
            ).scalar()
            or 0
        )
        if existing:
            return 0, f"部门联络员已有 {existing} 个权限点，保持不动"
        conn.execute(
            insert(M.SysRolePermission),
            [
                {"role_id": role_id, "perm_code": code}
                for code in permissions.LIAISON_DEFAULT_PERMS
            ],
        )
        return len(permissions.LIAISON_DEFAULT_PERMS), "已写入部门联络员默认权限点"


def main() -> int:
    parser = argparse.ArgumentParser(description="补齐 2.9 角色权限结构（幂等）")
    parser.add_argument("--dry-run", action="store_true", help="只打印 DDL，不执行")
    args = parser.parse_args()

    from sqlalchemy import text

    from app.config import settings
    from app.db.session import db_session, db_transaction

    print("=" * 68)
    print("迁移（任务 2.9：sys_role_permission + sys_user.password_hash）")
    print(f"存储模式：{settings.store_label}")
    print("=" * 68)

    if settings.store_label != "mysql":
        print("当前不是 MySQL 模式，无需迁移（JSON 模式无表结构）。")
        return 0

    try:
        with db_session() as conn:
            tables = {str(r[0]) for r in conn.execute(text("SHOW TABLES")).all()}
            columns = {
                str(r[0]) for r in conn.execute(text(f"SHOW COLUMNS FROM {TABLE_NAME}")).all()
            }
    except Exception as exc:  # noqa: BLE001
        print(f"无法读取库结构：{type(exc).__name__}: {exc}")
        print("请确认 MySQL 已启动、库已按 schema.sql 建表。")
        return 1

    created_table = 0
    for name, ddl in TABLES.items():
        if name in tables:
            print(f"\n{name}：已存在，跳过")
            continue
        print(f"\n{name}：缺失，将创建")
        stmt = text(ddl)
        params = stmt.compile().params
        if params:
            print(f"  ! DDL 解析出意外 bind 参数 {sorted(params)}，已中止")
            return 1
        if args.dry_run:
            print(ddl.strip())
            continue
        with db_transaction() as conn:
            conn.execute(stmt)
        print(f"  ✓ {name} 已创建")
        created_table += 1

    added_column = 0
    for column, definition in REQUIRED_COLUMNS.items():
        if column in columns:
            print(f"\n{TABLE_NAME}.{column}：已存在，跳过")
            continue
        ddl = f"ALTER TABLE {TABLE_NAME} ADD COLUMN {column} {definition}"
        print(f"\n{TABLE_NAME}.{column}：缺失，将添加")
        stmt = text(ddl)
        params = stmt.compile().params
        if params:
            print(f"  ! DDL 解析出意外 bind 参数 {sorted(params)}，已中止")
            return 1
        if args.dry_run:
            print(ddl)
            continue
        with db_transaction() as conn:
            conn.execute(stmt)
        print(f"  ✓ {TABLE_NAME}.{column} 已添加")
        added_column += 1

    if args.dry_run:
        print("\n（--dry-run：未执行任何 DDL，也未写入默认权限）")
        return 0

    perms, note = _seed_liaison_defaults()
    print(f"\n权限种子：{note}（{perms} 个）")

    with db_session() as conn:
        after_tables = {str(r[0]) for r in conn.execute(text("SHOW TABLES")).all()}
        after_columns = {
            str(r[0]) for r in conn.execute(text(f"SHOW COLUMNS FROM {TABLE_NAME}")).all()
        }
    ok = "sys_role_permission" in after_tables and "password_hash" in after_columns
    print(
        f"\n迁移后：矩阵表={'是' if 'sys_role_permission' in after_tables else '否'}"
        f" · 密码列={'是' if 'password_hash' in after_columns else '否'}"
        f"（本次新建表 {created_table} / 新增列 {added_column}）"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
