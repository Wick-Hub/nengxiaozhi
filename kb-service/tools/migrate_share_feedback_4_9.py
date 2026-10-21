"""建分享/反馈表（任务 4.9，幂等）。

``database/schema.sql`` 已包含 ``qa_share`` / ``qa_feedback``；本脚本给存量库
补建，重复运行安全。JSON 模式使用 ``data/shares.json`` / ``data/feedback.json``，
无需迁移。

用法::

    python tools/migrate_share_feedback_4_9.py
    python tools/migrate_share_feedback_4_9.py --dry-run
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
    "qa_share": """
        CREATE TABLE IF NOT EXISTS qa_share (
          share_id      VARCHAR(24) PRIMARY KEY COMMENT '公开分享ID(URL安全)',
          session_id    VARCHAR(64) NOT NULL COMMENT '来源会话',
          msg_id        BIGINT COMMENT '来源AI消息',
          question      VARCHAR(1024) NOT NULL DEFAULT '' COMMENT '用户问题快照',
          answer        MEDIUMTEXT NOT NULL COMMENT '回答快照(纯文本/Markdown)',
          citations     JSON COMMENT '引用来源快照',
          agent_name    VARCHAR(128) NOT NULL DEFAULT '' COMMENT '回答助手名称',
          channel_code  VARCHAR(64) NOT NULL DEFAULT '' COMMENT '来源渠道',
          role_snapshot VARCHAR(64) NOT NULL DEFAULT '' COMMENT '提问者角色快照',
          views         INT NOT NULL DEFAULT 0 COMMENT '分享页打开次数',
          created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          KEY idx_created (created_at),
          KEY idx_session (session_id)
        ) COMMENT='问答分享卡片'
    """,
    "qa_feedback": """
        CREATE TABLE IF NOT EXISTS qa_feedback (
          feedback_id   BIGINT PRIMARY KEY AUTO_INCREMENT,
          share_id      VARCHAR(24) NOT NULL DEFAULT '' COMMENT '来自分享页时记录',
          session_id    VARCHAR(64) NOT NULL DEFAULT '' COMMENT '来源会话',
          msg_id        BIGINT COMMENT '来源AI消息',
          rating        TINYINT NOT NULL COMMENT '1点赞 -1点踩',
          reason_code   VARCHAR(32) NOT NULL DEFAULT '' COMMENT 'inaccurate/incomplete/irrelevant/other',
          reason        VARCHAR(512) NOT NULL DEFAULT '' COMMENT '补充说明',
          question      VARCHAR(1024) NOT NULL DEFAULT '' COMMENT '问题快照',
          answer_excerpt VARCHAR(512) NOT NULL DEFAULT '' COMMENT '回答摘要',
          role_snapshot VARCHAR(64) NOT NULL DEFAULT '' COMMENT '提问者角色快照',
          channel_code  VARCHAR(64) NOT NULL DEFAULT '' COMMENT '来源渠道',
          handled       TINYINT NOT NULL DEFAULT 0 COMMENT '0待处理 1已处理',
          handle_note   VARCHAR(512) NOT NULL DEFAULT '' COMMENT '处理备注',
          created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          handled_at    DATETIME,
          KEY idx_rating (rating),
          KEY idx_handled (handled),
          KEY idx_created (created_at)
        ) COMMENT='问答反馈'
    """,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="补建 qa_share / qa_feedback（幂等）")
    parser.add_argument("--dry-run", action="store_true", help="只打印 DDL，不执行")
    args = parser.parse_args()

    from sqlalchemy import text

    from app.config import settings
    from app.db.session import db_session

    print("=" * 68)
    print("迁移（任务 4.9：qa_share / qa_feedback，幂等）")
    print(f"存储模式：{settings.store_label}")
    print("=" * 68)

    if settings.store_label != "mysql":
        print("当前不是 MySQL 模式，无需迁移（JSON 模式使用 data/shares.json / feedback.json）。")
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
