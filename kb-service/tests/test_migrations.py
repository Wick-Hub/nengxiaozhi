r"""数据库迁移脚本的守卫（任务 2.2 / 2.3 / 2.4）。

## 为什么需要这个文件

三个迁移脚本（`tools/migrate_agent_2_2/2_3/2_4.py`）的 DDL 常量是**字符串**，
经 SQLAlchemy ``text()`` 执行。``text()`` 会把形如 ``:name`` 的片段当作
**bind 参数**，而 COMMENT 文案里出现半角冒号紧跟单词时就会误判。

**这个 bug 长期潜伏，只在真库上炸过**：

* ``--dry-run`` 只 ``print`` DDL 字符串，**不编译** → 看不到问题；
* 无 MySQL 的环境（本机长期如此）根本走不到 ``conn.execute(text(ddl))``；
* 于是 ``migrate_agent_2_3.py`` 带着一个必炸的 DDL 一直"看起来正常"，
  直到在本机把 MySQL 跑起来才暴露：
  ``A value is required for bind parameter 'topK'``

触发条件很隐蔽：SQLAlchemy 的 bind 参数正则是 ``(?<![:\w]):(\w+)``，
而 ``\\w`` **包含中文**——所以 ``厂商:deepseek``（冒号前是中文）不会触发，
``(M-A-05):topK``（冒号前是 ``)``）才会。靠人眼 review 很难稳定发现。

本文件**直接编译**这些 DDL，断言它们不产生任何意外 bind 参数——
不依赖正则、不依赖真库，任何环境都能跑。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

SERVICE_DIR = Path(__file__).resolve().parent.parent
TOOLS_DIR = SERVICE_DIR / "tools"
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))


def _load(name: str):
    """按路径加载 migrations 脚本（tools/ 不是包，不能直接 import）。"""
    path = TOOLS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_mig_{name}", path)
    assert spec and spec.loader, f"无法加载 {path}"
    mod = importlib.util.module_from_spec(spec)
    # 脚本在导入期会 setdefault KB_STORE=mysql，不连库，安全
    spec.loader.exec_module(mod)
    return mod


def _assert_compiles(ddl: str, label: str) -> None:
    """DDL 编译后不得出现意外 bind 参数。"""
    stmt = text(ddl)
    params = stmt.compile().params
    assert not params, (
        f"{label} 的 DDL 被 SQLAlchemy 解析出意外 bind 参数 {sorted(params)}。\n"
        f"多半是 COMMENT 里出现了半角冒号紧跟单词（如 \"(M-A-05):topK\"）。\n"
        f"规避：改用全角冒号，或调整文案。\nDDL: {ddl}"
    )


def test_add_column_ddl_has_no_bind_params() -> None:
    """2_2 / 2_3 的 ADD COLUMN DDL 必须能安全编译。"""
    for mod_name in ("migrate_agent_2_2", "migrate_agent_2_3"):
        mod = _load(mod_name)
        cols = mod.REQUIRED_COLUMNS
        assert cols, f"{mod_name}.REQUIRED_COLUMNS 不应为空"
        for col, definition in cols.items():
            _assert_compiles(f"ALTER TABLE agent ADD COLUMN {col} {definition}",
                             f"{mod_name}:{col}")


def test_create_table_ddl_has_no_bind_params() -> None:
    """2_4 的 CREATE TABLE llm_model 必须能安全编译。"""
    mod = _load("migrate_agent_2_4")
    _assert_compiles(mod.CREATE_LLM_MODEL, "migrate_agent_2_4:CREATE_LLM_MODEL")


def test_kb_2_5_2_6_create_table_ddl_has_no_bind_params() -> None:
    """2.5 / 2.6 的 kb_category / kb_permission 建表 DDL 必须能安全编译。

    这两张表的 COMMENT 里有 ``被授权用户(sys_user,部门联络员)`` 这类半角括号
    紧跟中文的写法，正是历史上炸过 bind 参数的形状，必须过一遍守卫。
    """
    mod = _load("migrate_kb_2_5_2_6")
    assert set(mod.TABLES) == {"kb_category", "kb_permission"}
    for name, ddl in mod.TABLES.items():
        _assert_compiles(ddl, f"migrate_kb_2_5_2_6:{name}")


def test_sensitive_2_10_create_table_ddl_has_no_bind_params() -> None:
    """2.10 的 sensitive_word 建表 DDL 必须能安全编译。"""
    mod = _load("migrate_sensitive_2_10")
    assert set(mod.TABLES) == {"sensitive_word"}
    for name, ddl in mod.TABLES.items():
        _assert_compiles(ddl, f"migrate_sensitive_2_10:{name}")


def test_channel_3_1_ddl_has_no_bind_params() -> None:
    """3.1 的 channel / channel_agent 建表与主助手回填 SQL 必须能安全编译。"""
    mod = _load("migrate_channel_3_1")
    assert set(mod.TABLES) == {"channel", "channel_agent"}
    for name, ddl in mod.TABLES.items():
        _assert_compiles(ddl, f"migrate_channel_3_1:{name}")
    _assert_compiles(mod.BACKFILL_DEFAULT_AGENT, "migrate_channel_3_1:BACKFILL_DEFAULT_AGENT")


def test_app_3_3_ddl_has_no_bind_params() -> None:
    """3.3 的 app 建表 DDL 必须能安全编译。"""
    mod = _load("migrate_app_3_3")
    _assert_compiles(mod.CREATE_APP, "migrate_app_3_3:CREATE_APP")


def test_rbac_2_9_ddl_has_no_bind_params() -> None:
    """2.9 的 sys_role_permission 建表与 password_hash 加列 DDL 必须能安全编译。"""
    mod = _load("migrate_rbac_2_9")
    assert set(mod.TABLES) == {"sys_role_permission"}
    for name, ddl in mod.TABLES.items():
        _assert_compiles(ddl, f"migrate_rbac_2_9:{name}")
    for col, definition in mod.REQUIRED_COLUMNS.items():
        _assert_compiles(
            f"ALTER TABLE {mod.TABLE_NAME} ADD COLUMN {col} {definition}",
            f"migrate_rbac_2_9:{col}",
        )


def test_modify_column_ddl_has_no_bind_params() -> None:
    """2_4 的 MODIFY COLUMN DDL 必须能安全编译。"""
    mod = _load("migrate_agent_2_4")
    for col, ddl_type in mod.NULLABLE_COLUMNS.items():
        _assert_compiles(f"ALTER TABLE agent MODIFY COLUMN {col} {ddl_type}",
                         f"migrate_agent_2_4:{col}")


def test_guard_itself_detects_the_real_bug() -> None:
    """守卫必须真的抓得住 —— 用当初那个炸过的 DDL 反证。

    没有这条，"守卫通过"只能说明守卫没报错，不能说明守卫有判别力。
    """
    buggy = (
        "ALTER TABLE agent ADD COLUMN retrieval_config "
        "JSON COMMENT '检索配置(M-A-05):topK/rerankTopn'"
    )
    with pytest.raises(AssertionError, match="意外 bind 参数"):
        _assert_compiles(buggy, "故意构造的历史 bug")

    # 反例：冒号前是中文时 SQLAlchemy 不匹配（\w 含中文），这类写法是安全的。
    # 记录在此以免将来有人"看到冒号就以为有问题"而误改。
    safe = "VARCHAR(32) COMMENT '厂商:deepseek/qwen'"
    _assert_compiles(f"ALTER TABLE llm_model ADD COLUMN vendor {safe}", "冒号前为中文")
