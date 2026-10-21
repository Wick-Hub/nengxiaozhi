"""写入 RBAC 演示角色与账号（任务 1.13）。

    python tools/seed_auth.py            # 幂等写入
    python tools/seed_auth.py --dry-run  # 只看会写什么

账号清单（``app/services/auth_service.py`` 的 ``SEED_USERS``）：

    admin      系统管理员    → 管理员（可访问管理接口）
    20230001   本科生演示账号 → 本科生
    20240001   研究生演示账号 → 研究生
    T10001     教职工演示账号 → 教职工
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.session import db_transaction, ping  # noqa: E402
from app.services import auth_service  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="写入 RBAC 演示角色与账号")
    ap.add_argument("--dry-run", action="store_true", help="只打印将写入的内容")
    args = ap.parse_args(argv)

    ok, info = ping()
    if not ok:
        print(f"❌ MySQL 不可用：{info}")
        return 1
    print(f"数据库：{info}")
    print()

    if args.dry_run:
        print("（dry-run）将写入：")
        for code, name, rtype, is_public, _ in auth_service.SEED_ROLES:
            kind = "管理角色" if rtype == 2 else "身份角色"
            print(f"  角色  {name:<8} code={code:<12} {kind}{'  · 公开可见' if is_public else ''}")
        print()
        for user_no, real_name, id_type, role_code in auth_service.SEED_USERS:
            print(f"  账号  {user_no:<10} {real_name:<14} id_type={id_type} → {role_code}")
        return 0

    with db_transaction() as conn:
        created = auth_service.ensure_seed(conn)

    print(
        f"✅ 完成：新增角色 {created['roles']}（同步更新 {created['roles_updated']}）"
        f" ｜ 账号 {created['users']} ｜ 绑定 {created['bindings']}"
    )
    print(f"   库中合计：{auth_service.count_accounts()}")
    print()
    print("演示账号：admin（管理员）/ 20230001（本科生）/ 20240001（研究生）/ T10001（教职工）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
