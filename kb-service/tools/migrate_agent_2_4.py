"""为已有 MySQL 库补 ``llm_model`` 表并把 ``agent`` 生成参数列改为可空（任务 2.4，幂等）。

``database/schema.sql`` 已同步这两项（新建库直接带），但**已经建好的库**需要一次迁移。
MySQL 8 不支持 ``ADD COLUMN IF NOT EXISTS`` / ``CREATE TABLE IF NOT EXISTS`` 之外的条件 DDL，
因此这里先 ``SHOW TABLES`` / ``SHOW COLUMNS`` 再按差集执行，重复运行安全。

本次迁移包含三步：

1. **建表** ``llm_model``（M-A-06 / M-SYS-06 / C-01）。缺表则按 ``schema.sql`` 的定义创建。
2. **改列可空**：``agent.temperature`` / ``top_p`` / ``max_tokens`` 由
   ``NOT NULL`` + 物化默认值（0.70 / 1.00 / 2000）改为**可空、无默认值**。
   原因：0.0 与 1.0 都是合法温度，**不能拿 0 当「未配置」的哨兵值**；而物化全局默认值会让
   改 ``.env`` 后已建助手不跟随，且无法区分「没配过」与「显式配成了和默认一样」。
   这与 2.3 的 ``retrieval_config`` 用的是同一套「可辨识空值」思路。
3. **回填 NULL**：把存量行的这三列置为 ``NULL``（= 沿用平台默认）。

   为什么敢直接置空：这三列**在 2.4 之前从未被读取过**（2.3 的 ``AgentRuntime`` 只解析
   ``system_prompt`` 与检索参数，模型参数登记在「待接线字段」清单里），
   即存量数值**从未生效**。置 NULL 是「如实反映现状」，不是丢数据。
   若确实想保留旧值，加 ``--keep-values`` 跳过第 3 步（此后助手会显式沿用那些数值）。

用法::

    python tools/migrate_agent_2_4.py                # 检查并迁移
    python tools/migrate_agent_2_4.py --dry-run      # 只打印将要执行的 DDL
    python tools/migrate_agent_2_4.py --keep-values  # 不回填 NULL，保留存量数值

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

#: 新建表 DDL（与 database/schema.sql 的 llm_model 逐字一致）
CREATE_LLM_MODEL = """
CREATE TABLE llm_model (
  model_id     BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '模型ID',
  model_code   VARCHAR(64) NOT NULL COMMENT '模型编码(agent.model_code 引用;default 保留给 .env)',
  model_name   VARCHAR(128) NOT NULL COMMENT '展示名称,如 豆包·Doubao-pro-32k',
  vendor       VARCHAR(32) NOT NULL COMMENT '厂商:deepseek/doubao/qwen/glm/kimi/openai/local/custom',
  api_base     VARCHAR(255) NOT NULL DEFAULT '' COMMENT 'OpenAI 兼容 base_url,如 https://ark.cn-beijing.volces.com/api/v3',
  remote_model VARCHAR(128) NOT NULL DEFAULT '' COMMENT '厂商侧模型名,如 doubao-pro-32k',
  api_key_enc  VARBINARY(512) COMMENT 'API Key 密文(AES-256-GCM),本地模型可空',
  timeout      FLOAT COMMENT '单次调用读超时(秒),NULL=沿用全局;本地模型可调大',
  is_local     TINYINT NOT NULL DEFAULT 0 COMMENT '1=校内本地模型(C-05,通常无需 API Key)',
  sort_no      INT NOT NULL DEFAULT 0 COMMENT '列表排序',
  remark       VARCHAR(255) NOT NULL DEFAULT '' COMMENT '备注(用途/负责人等)',
  status       TINYINT NOT NULL DEFAULT 1 COMMENT '1启用 0停用(停用后引用它的助手回退全局默认)',
  created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_model_code (model_code),
  KEY idx_vendor (vendor),
  KEY idx_status (status)
) COMMENT='大模型配置'
"""

#: 需要改为「可空、无默认值」的列（列名 → 新定义）
NULLABLE_COLUMNS: dict[str, str] = {
    "temperature": "DECIMAL(3,2) NULL COMMENT '温度,NULL=沿用平台默认'",
    "top_p": "DECIMAL(3,2) NULL COMMENT 'topP,NULL=沿用平台默认'",
    "max_tokens": "INT NULL COMMENT '最大输出token,NULL=沿用平台默认'",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="llm_model 建表 + agent 参数列改可空（幂等）")
    parser.add_argument("--dry-run", action="store_true", help="只打印 DDL，不执行")
    parser.add_argument(
        "--keep-values",
        action="store_true",
        help="不回填 NULL（保留存量 temperature/top_p/max_tokens 数值）",
    )
    args = parser.parse_args()

    from sqlalchemy import text

    from app.config import settings
    from app.db.session import db_session

    print("=" * 68)
    print("迁移（任务 2.4：llm_model 建表 + agent 生成参数列改可空）")
    print(f"存储模式：{settings.store_label}")
    print("=" * 68)

    if settings.store_label != "mysql":
        print("当前不是 MySQL 模式，无需迁移（JSON 模式无表结构）。")
        return 0

    try:
        with db_session() as conn:
            tables = {str(r[0]) for r in conn.execute(text("SHOW TABLES")).all()}
            agent_cols = {str(r[0]) for r in conn.execute(text("SHOW COLUMNS FROM agent")).all()}
    except Exception as exc:  # noqa: BLE001
        print(f"无法读取库结构：{type(exc).__name__}: {exc}")
        print("请确认 MySQL 已启动、库已按 schema.sql 建表。")
        return 1

    # ---- 第 1 步：llm_model 建表 ----
    print(f"\n[1/3] llm_model 表：{'已存在，跳过' if 'llm_model' in tables else '缺失，将创建'}")
    if "llm_model" not in tables:
        if args.dry_run:
            print(CREATE_LLM_MODEL.strip())
        else:
            with db_session() as conn:
                conn.execute(text(CREATE_LLM_MODEL))
                conn.commit()
            print("  ✓ llm_model 已创建")

    # ---- 第 2 步：agent 生成参数列改可空 ----
    print("\n[2/3] agent 生成参数列改可空")
    for col, ddl_type in NULLABLE_COLUMNS.items():
        if col not in agent_cols:
            print(f"  ! {col} 不存在，跳过（请先跑 migrate_agent_2_2.py 确认基础列）")
            continue
        ddl = f"ALTER TABLE agent MODIFY COLUMN {col} {ddl_type}"
        print(f"  {ddl}")
        if args.dry_run:
            continue
        with db_session() as conn:
            conn.execute(text(ddl))
            conn.commit()
        print(f"  ✓ {col} 已改为可空")

    # ---- 第 3 步：回填 NULL ----
    print("\n[3/3] 存量行回填 NULL（= 沿用平台默认）")
    if args.keep_values:
        print("  （--keep-values：已跳过）")
    elif args.dry_run:
        print("  UPDATE agent SET temperature=NULL, top_p=NULL, max_tokens=NULL")
    else:
        with db_session() as conn:
            result = conn.execute(
                text("UPDATE agent SET temperature=NULL, top_p=NULL, max_tokens=NULL")
            )
            conn.commit()
            affected = result.rowcount
        print(f"  ✓ 已置空 {affected} 行（这些数值在 2.4 之前从未生效）")

    if args.dry_run:
        print("\n（--dry-run：未执行任何 DDL/DML）")
        return 0

    with db_session() as conn:
        tables_after = {str(r[0]) for r in conn.execute(text("SHOW TABLES")).all()}
        cols_after = {
            str(r[0]): r for r in conn.execute(text("SHOW COLUMNS FROM agent")).all()
        }

    # SHOW COLUMNS 的第 3 列是 Null 标志（YES/NO）
    nullable_ok = all(
        str(cols_after.get(c, ("", "", "NO"))[2]).upper() == "YES" for c in NULLABLE_COLUMNS
    )
    table_ok = "llm_model" in tables_after
    print(f"\n迁移后：llm_model 就位={'是' if table_ok else '否'}；"
          f"三列均可空={'是' if nullable_ok else '否'}")
    return 0 if (table_ok and nullable_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
