"""离线安装：初始化 MySQL 结构并执行增量迁移（任务 5.2）。

只做「建库 + 建表 + 补列」，不写演示业务数据：
* 基础表来自 ``database/schema.sql``（仅当目标库为空时执行）；
* 增量迁移逐个以子进程运行，``migrate_support_4_6.py`` 固定 ``--no-seed``；
* 全程幂等，可重复执行；``--dry-run`` 只打印计划。

运行目录必须是 kb-service（读取同目录 ``.env``）。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

MIGRATIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("migrate_kb_2_5_2_6.py", ()),
    ("migrate_sensitive_2_10.py", ()),
    ("migrate_rbac_2_9.py", ()),
    ("migrate_agent_2_2.py", ()),
    ("migrate_agent_2_3.py", ()),
    ("migrate_agent_2_4.py", ()),
    ("migrate_channel_3_1.py", ()),
    ("migrate_app_3_3.py", ()),
    ("migrate_route_3_4.py", ()),
    ("migrate_agent_guide_3_8.py", ()),
    ("migrate_session_device_3_10.py", ()),
    ("migrate_message_refused_3_10.py", ()),
    ("migrate_unrecognized_3_11.py", ()),
    ("migrate_access_4_4.py", ()),
    ("migrate_gov_4_5.py", ()),
    ("migrate_share_feedback_4_9.py", ()),
    ("migrate_support_4_6.py", ("--no-seed",)),
    ("migrate_data_center_4_2.py", ()),
    ("migrate_integrations_4_8.py", ()),
)


def _split_sql(text: str) -> list[str]:
    statements: list[str] = []
    buffer: list[str] = []
    quote = ""
    line_comment = False
    i = 0
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if line_comment:
            buffer.append(ch)
            if ch == "\n":
                line_comment = False
            i += 1
            continue
        if not quote and ch == "-" and nxt == "-":
            line_comment = True
            buffer.append(ch)
            i += 1
            continue
        if quote:
            buffer.append(ch)
            if ch == "\\":
                if i + 1 < len(text):
                    buffer.append(text[i + 1])
                    i += 2
                    continue
            elif ch == quote:
                if nxt == quote:  # SQL 转义的连续引号
                    buffer.append(nxt)
                    i += 2
                    continue
                quote = ""
            i += 1
            continue
        if ch in {"'", '"', "`"}:
            quote = ch
            buffer.append(ch)
            i += 1
            continue
        if ch == ";":
            statement = "".join(buffer).strip()
            if statement:
                statements.append(statement)
            buffer = []
            i += 1
            continue
        buffer.append(ch)
        i += 1
    tail = "".join(buffer).strip()
    if tail:
        statements.append(tail)
    return statements


def _connect(database: str | None = None):
    import pymysql

    from app.config import settings

    return pymysql.connect(
        host=settings.db_host,
        port=int(settings.db_port),
        user=settings.db_user,
        password=settings.db_password,
        database=database,
        charset=settings.db_charset,
        autocommit=True,
        connect_timeout=8,
    )


def _schema_ready(database: str) -> bool:
    try:
        conn = _connect(database)
    except Exception:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME IN ('kb_doc','sys_user','agent')",
                (database,),
            )
            return int(cur.fetchone()[0]) == 3
    finally:
        conn.close()


def init_schema(schema: Path, dry_run: bool) -> str:
    from app.config import settings

    if _schema_ready(settings.db_name):
        return f"基础表已存在，跳过 schema.sql（{settings.db_name}）"
    if dry_run:
        return f"[dry-run] 将导入 {schema} 到 {settings.db_name}"
    if not schema.exists():
        raise SystemExit(f"schema 文件不存在：{schema}")
    statements = _split_sql(schema.read_text(encoding="utf-8"))
    conn = _connect()
    try:
        with conn.cursor() as cur:
            for statement in statements:
                cur.execute(statement)
    finally:
        conn.close()
    return f"基础结构导入完成：{len(statements)} 条语句"


def run_migrations(dry_run: bool) -> list[str]:
    results = []
    for name, extra in MIGRATIONS:
        script = SERVICE_DIR / "tools" / name
        if not script.exists():
            raise SystemExit(f"迁移脚本缺失：{script}")
        if dry_run:
            results.append(f"[dry-run] {name} {' '.join(extra)}".strip())
            continue
        results.append(f"{name}: {_run_script(script, extra)}")
    return results


def _run_script(script, extra: tuple[str, ...]) -> str:
    env = {**os.environ, "KB_STORE": "mysql"}
    proc = subprocess.run(
        [sys.executable, str(script), *extra],
        cwd=SERVICE_DIR,
        env=env,
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-1200:]
        raise SystemExit(f"脚本失败：{script.name}\n{detail}")
    return "ok"


def seed_auth(dry_run: bool) -> str:
    script = SERVICE_DIR / "tools" / "seed_auth.py"
    if not script.exists():
        raise SystemExit(f"账号种子脚本缺失：{script}")
    if dry_run:
        return "[dry-run] seed_auth.py（角色 + 演示账号）"
    return f"seed_auth.py: {_run_script(script, ())}"


def main() -> None:
    parser = argparse.ArgumentParser(description="离线安装数据库初始化（任务 5.2）")
    parser.add_argument("--schema", default="", help="基础结构 SQL；仅在目标库为空时执行")
    parser.add_argument("--seed-auth", action="store_true", help="写入 RBAC 角色与演示账号（仅全新安装）")
    parser.add_argument("--skip-migrations", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--list", action="store_true", help="只列出迁移顺序")
    args = parser.parse_args()

    if args.list:
        for name, extra in MIGRATIONS:
            print(name, " ".join(extra))
        return

    from app.config import settings

    print(f"目标库：{settings.db_host}:{settings.db_port}/{settings.db_name}")
    if args.schema:
        print(init_schema(Path(args.schema), args.dry_run))
    if not args.skip_migrations:
        for line in run_migrations(args.dry_run):
            print(line)
    if args.seed_auth:
        print(seed_auth(args.dry_run))
    print("数据库初始化完成。" if not args.dry_run else "（dry-run 结束）")


if __name__ == "__main__":
    main()
