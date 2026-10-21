"""数据库连通性检查（任务 1.14 前置 / 验收脚本）。

用法（在 kb-service 目录下）::

    python tools/check_db.py

检查项：
    1. 配置（脱敏打印 DB 连接串）
    2. TCP 可达性（3306）
    3. 账号认证 + 库是否存在
    4. 关键表与行数（kb_doc / kb_chunk / kb_chunk_vec_proto / chat_* / token_usage）
    5. 建表脚本是否已执行（期望 23 张表 + 1 视图）

退出码：0 全部通过；1 有失败项。
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text  # noqa: E402

from app.config import settings  # noqa: E402

EXPECTED_TABLES = [
    "kb_doc",
    "kb_chunk",
    "kb_chunk_vec_proto",
    "chat_session",
    "chat_message",
    "answer_citation",
    "token_usage",
    "unrecognized_question",
    "sys_user",
    "sys_role",
    "sys_org",
    "agent",
    "channel",
]

ok = True


def fail(msg: str) -> None:
    global ok
    ok = False
    print(f"  ✘ {msg}")


def main() -> int:
    print("=== 数据库连通性检查 ===\n")

    # 1) 配置
    masked = f"mysql+pymysql://{settings.db_user}:{'*' * len(settings.db_password)}@{settings.db_host}:{settings.db_port}/{settings.db_name}"
    print(f"1. 连接配置      : {masked}")
    print(f"   密码是否已配置: {'是' if settings.db_password else '否（空密码会 1045 拒绝）'}")
    if not settings.db_password:
        fail("DB_PASSWORD 为空，请检查 kb-service/.env")

    # 2) TCP
    reachable = False
    try:
        with socket.create_connection((settings.db_host, settings.db_port), timeout=5):
            reachable = True
    except OSError as exc:
        fail(f"TCP {settings.db_host}:{settings.db_port} 不可达：{exc}")
    print(f"2. TCP 可达性    : {settings.db_host}:{settings.db_port} {'可达 ✔' if reachable else '不可达 ✘'}")
    if not reachable:
        print("\n  提示：MySQL 跑在 Docker 里，先确认 Docker Desktop 已启动，再执行")
        print("        docker compose -f ../database/docker-compose.yml up -d")
        return 1

    # 3~5) 认证 + 表
    try:
        engine = create_engine(settings.db_url, pool_pre_ping=True)
        with engine.connect() as c:
            ver = c.execute(text("SELECT VERSION()")).scalar()
            db = c.execute(text("SELECT DATABASE()")).scalar()
            print(f"3. 认证通过      : MySQL {ver}，当前库 {db} ✔")

            n_tables = c.execute(
                text(
                    "SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE table_schema = :s AND table_type = 'BASE TABLE'"
                ),
                {"s": settings.db_name},
            ).scalar()
            n_views = c.execute(
                text(
                    "SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE table_schema = :s AND table_type = 'VIEW'"
                ),
                {"s": settings.db_name},
            ).scalar()
            print(f"4. 表结构        : {n_tables} 张表 + {n_views} 个视图")
            if n_tables < 23:
                fail(f"表数量偏少（期望 ≥23，实际 {n_tables}）——建表脚本可能未执行")

            print("5. 关键表行数    :")
            for t in EXPECTED_TABLES:
                try:
                    cnt = c.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
                    print(f"     {t:22} {cnt:>6} 行")
                except Exception as exc:  # noqa: BLE001
                    fail(f"{t} 查询失败：{exc}")

            total_docs = c.execute(text("SELECT COUNT(*) FROM kb_doc")).scalar()
            if total_docs == 0:
                print("\n  注意：kb_doc 为空——库结构已就绪，但业务数据尚未入库（需执行 ingest/sync）。")
    except Exception as exc:  # noqa: BLE001
        fail(f"连接或查询失败：{type(exc).__name__}: {exc}")
        return 1

    print("\n" + (">>> 数据库连接正常 <<<" if ok else ">>> 存在问题，见上方 ✘ <<<"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
