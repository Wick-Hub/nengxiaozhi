"""MySQL 分支桩验收（任务 2.1 / 2.2 / 2.4 的数据层）。

## 为什么需要这个工具

``verify_agents.py`` / ``verify_chatlogs.py`` / ``verify_model.py`` 默认跑 JSON 后端；
它们的 ``--store mysql`` 分支**需要真实数据库**。而本机（及多数开发机）没有 MySQL，
于是 2.1/2.2/2.4 新增的 **MySQL 专属代码路径**长期处于「写了但从没执行过」的状态：

* ``agent_store._row_to_dict`` / ``model_store._row_to_dict`` 的归一化；
* ``chatlog_admin._load_mysql`` 的**筛选下推**与名字映射；
* ``_payload`` 的 **NULL 语义**（``temperature=0.0`` 必须原样落库、未配置必须是 NULL）。

本工具用**场景驱动的假连接**把真实 SQL 换掉，只执行这些分支的 Python 代码，
从而在无库环境下验证它们。**这是模式分支验收，不是实机验收**——
MySQL 语法、类型映射、字符集、事务语义仍须在有库环境用
``verify_*.py --store mysql`` 与 ``pytest tests/test_mysql_store.py`` 验证。

## 四组自检

1. **归一化同形**：同一份逻辑记录，JSON 后端写入回读 与 MySQL 行归一，
   **键集合必须完全一致**（否则前端要为两种后端写两套解析）；
2. **可辨识 NULL**：未配置的生成参数在 MySQL 分支必须是 ``None``，
   显式 ``temperature=0.0`` 必须原样落库（0.0 是合法温度，不是哨兵值）；
3. **筛选下推**：会话级筛选（role/agent/channel）与时间范围必须出现在 SQL 里，
   而不是取回后在内存里过滤（否则 ``SCAN_LIMIT`` 窗口会被无关消息占满）；
4. **失败要保住现场**：探测异常不得让整个验收崩掉。
   落库的是密文，明文绝不出现在 SQL 参数里。

用法::

    python tools/verify_mysql_branch.py
    python tools/verify_mysql_branch.py --verbose

退出码：全部通过为 0，存在 FAIL 为 1。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

# 必须在导入 app.* 之前定型（settings 在导入期读取环境变量）
os.environ.setdefault("KB_STORE", "json")

try:  # pragma: no cover - 仅影响交互输出
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

PASSED = 0
FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
    else:
        FAILED += 1
    tail = f"  [{detail}]" if detail else ""
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{tail}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ------------------------------------------------------------------ 假连接
class _Result:
    def __init__(self, rows=(), *, inserted: int | None = None, rowcount: int = 0) -> None:
        self._rows = [tuple(r) for r in rows]
        self._inserted = inserted
        self.rowcount = rowcount

    def all(self) -> list[tuple]:
        return list(self._rows)

    def first(self) -> tuple | None:
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._rows[0][0] if self._rows else None

    @property
    def inserted_primary_key(self) -> tuple:
        return (self._inserted,)


class _Conn:
    """按顺序发牌的假连接；语句留存供「下推」断言。"""

    def __init__(self, results) -> None:
        self._queue = list(results)
        self.statements: list = []

    def execute(self, stmt, *args, **kwargs) -> _Result:
        self.statements.append(stmt)
        if not self._queue:
            raise AssertionError(f"SQL 调用次数多于预期（第 {len(self.statements)} 条）")
        return self._queue.pop(0)


class _Session:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    def __enter__(self) -> _Conn:
        return self._conn

    def __exit__(self, *exc) -> bool:
        return False


def _sql(stmt) -> str:
    try:
        return str(stmt.compile(compile_kwargs={"literal_binds": True}))
    except Exception:  # noqa: BLE001 - JSON 参数无字面量渲染器
        return str(stmt.compile())


_ORIGINALS: dict = {}


def install(module, results) -> _Conn:
    """把模块内的 ``db_session`` / ``db_transaction`` 换成假连接（可多次调用覆盖）。"""
    conn = _Conn(results)
    for name in ("db_session", "db_transaction"):
        if hasattr(module, name):
            _ORIGINALS.setdefault((id(module), name), getattr(module, name))
            setattr(module, name, lambda: _Session(conn))
    return conn


def restore_all() -> None:
    """还原所有被替换的 ``db_session`` / ``db_transaction``。"""
    for mid, name in list(_ORIGINALS):
        mod = _MODULES.get(mid)
        if mod is not None:
            setattr(mod, name, _ORIGINALS[(mid, name)])
    _ORIGINALS.clear()
    _MODULES.clear()


_MODULES: dict = {}


def bind(module) -> None:
    """登记模块对象，供 :func:`restore_all` 还原。"""
    _MODULES[id(module)] = module


# ================================================================== 组 1：归一化同形
def check_shape_parity(tmpdir: Path) -> None:
    """JSON 后端写入回读 与 MySQL 行归一，**键集合必须完全一致**。"""
    from app.db import agent_store

    section("组 1  归一化同形（JSON ↔ MySQL）")
    agent_store.AGENTS_PATH = tmpdir / "agents.json"
    bind(agent_store)

    # ---- 助手
    created = agent_store.create_agent(
        {
            "agentCode": "shape-1",
            "agentName": "同形探针",
            "agentType": "chat",
            "systemPrompt": "你是探针",
            "modelCode": "default",
            "temperature": 0.0,
            "topP": None,
            "maxTokens": None,
            "visibleRoles": ["全部", "本科生"],
            "iconUrl": "",
            "description": "描述",
            "streamEnabled": 0,
            "riskReply": "",
            "fallbackReply": "",
            "retrievalConfig": {"topK": 3, "depts": ["图书馆"]},
            "status": 1,
        }
    )
    row = _agent_row_from(created)
    mysql_dict = agent_store._row_to_dict(row)

    ignore = {"agentId", "createdAt", "updatedAt"}
    check(
        "助手：两种后端对外键集合一致",
        set(created) == set(mysql_dict),
        f"仅 JSON 有 {sorted(set(created) - set(mysql_dict))}；仅 MySQL 有 {sorted(set(mysql_dict) - set(created))}",
    )
    diffs = [k for k in created if k not in ignore and created[k] != mysql_dict[k]]
    check("助手：除 id/时间外取值一致", not diffs, f"不一致：{diffs}")

    # 归一化容错：坏 JSON 不得抛异常
    broken = agent_store._row_to_dict(_agent_row(retrievalConfig="{不是 JSON"))
    check("助手：坏检索配置降级为默认值（不抛异常）",
          broken["retrievalConfig"] == agent_store.default_retrieval())
    as_dict = agent_store._row_to_dict(_agent_row(retrievalConfig={"topK": 8}))
    check("助手：检索配置缺键补默认值", as_dict["retrievalConfig"]["topK"] == 8)


def _agent_row_from(rec: dict) -> tuple:
    """把对外 dict 折算成库里那一行的形态。"""
    from app.db import agent_store

    values = {
        **rec,
        "visibleRoles": agent_store.join_roles(rec.get("visibleRoles")),
        "createdAt": datetime.fromisoformat(rec["createdAt"]),
        "updatedAt": datetime.fromisoformat(rec["updatedAt"]),
    }
    return tuple(values[k] for k in agent_store._COL_KEYS)


# ================================================================== 组 2：NULL 语义
def check_null_semantics() -> None:
    from app.config import settings
    from app.db import agent_store

    section("组 2  可辨识 NULL（生成参数）")
    settings.kb_store = "mysql"
    try:
        conn = install(
            agent_store,
            [
                _Result(inserted=9, rowcount=1),
                _Result([_agent_row(agentId=9)]),
            ],
        )
        agent_store.create_agent(
            {"agentCode": "null-1", "agentName": "NULL 探针", "temperature": 0.0}
        )
        params = conn.statements[0].compile().params
        check("显式 temperature=0.0 原样落库（0.0 是合法温度，不是哨兵值）",
              params["temperature"] == 0.0, f"temperature={params['temperature']!r}")
        check("未配置的 top_p 落 NULL（= 沿用平台默认，不物化）",
              params["top_p"] is None, f"top_p={params['top_p']!r}")
        check("未配置的 max_tokens 落 NULL",
              params["max_tokens"] is None, f"max_tokens={params['max_tokens']!r}")
        check("空 modelCode 收敛为保留编码 default（不落 llm_model）",
              params["model_code"] == agent_store.DEFAULT_MODEL_CODE)

        # 回读：库里 NULL → 对外 None（绝不物化成 .env 的数值）
        install(agent_store, [_Result([_agent_row()]), _Result([_agent_row()])])
        out = agent_store.list_agents()[0]
        check("库里 NULL 回读为 None（不物化成 .env 数值）",
              out["temperature"] is None and out["topP"] is None and out["maxTokens"] is None)

        # 环境默认值变化不应影响「未配置」的助手
        from app.config import get_settings  # noqa: F401

        settings.llm_temperature = 0.99
        again = agent_store.list_agents()[0]
        check("改 .env 默认值后，未配置助手仍为 None（可辨识性的意义）",
              again["temperature"] is None)
    finally:
        settings.kb_store = "json"


# ================================================================== 组 3：筛选下推
def check_filter_pushdown() -> None:
    from app.config import settings
    from app.services import chatlog_admin

    section("组 3  筛选下推（会话日志）")
    bind(chatlog_admin)
    settings.kb_store = "mysql"
    try:
        conn = install(chatlog_admin, _chatlog_results())
        chatlog_admin.query_qa(
            role="本科生", agent_id=1, channel_id=1,
            date_from="2026-10-01", date_to="2026-10-01", page_size=50,
        )
        sql = _sql(conn.statements[0])
        check("role 下推到 SQL", "chat_session.role_snapshot = '本科生'" in sql)
        check("agent_id 下推到 SQL", "chat_session.agent_id = 1" in sql)
        check("channel_id 下推到 SQL", "chat_session.channel_id = 1" in sql)
        check("时间下界下推到 SQL", "chat_message.created_at >= '2026-10-01 00:00:00'" in sql)
        check("时间上界下推到 SQL（含当天 23:59:59）",
              "chat_message.created_at <= '2026-10-01 23:59:59'" in sql)
        check("扫描窗口 SCAN_LIMIT 显式出现在 SQL", f"LIMIT {chatlog_admin.SCAN_LIMIT}" in sql)

        conn = install(chatlog_admin, _chatlog_results())
        chatlog_admin.query_qa(page_size=50)
        sql = _sql(conn.statements[0])
        check("不传筛选条件时不产生多余 WHERE",
              "role_snapshot =" not in sql and "created_at >=" not in sql)

        # 结果归一
        install(chatlog_admin, _chatlog_results())
        items = chatlog_admin.query_qa(page_size=50)["items"]
        by_id = {i["msgId"]: i for i in items}
        check("回答与问题正确配对", by_id[102]["question"] == "校园卡如何补办")
        check("助手/渠道名取自数据库", by_id[102]["agentName"] == "能小智·校园百事通"
              and by_id[102]["channelName"] == "门户Web")
        check("关联 ID 为空的会话不造假名字（返回 None，前端显示「—」）",
              by_id[202]["agentName"] is None and by_id[202]["channelId"] is None)
        check("token/模型按 trace_id 关联 token_usage",
              by_id[102]["model"] == "qwen-max" and by_id[102]["totalTokens"] == 150)
        check("引用数来自 answer_citation 聚合", by_id[102]["citationCount"] == 2)
        check("无 trace 的回答 token 为 None（不是 0）",
              by_id[202]["totalTokens"] is None and by_id[202]["model"] is None)
        check("chat_message 无 refused 列 → 不猜拒答，恒为 False",
              all(i["refused"] is False for i in items))

        # 导出与分页同源
        install(chatlog_admin, _chatlog_results())
        rows, truncated = chatlog_admin.export_rows("qa")
        check("导出与分页共用取数路径（导出 = 页面所见）",
              [r["msgId"] for r in rows] == [r["msgId"] for r in items] and not truncated)
        check("CSV 带 UTF-8 BOM（Excel 直接打开不乱码）",
              chatlog_admin.to_csv("qa", rows).startswith("\ufeff"))

        # 筛选项来自库，不是 JSON 分支的占位
        install(chatlog_admin, [
            _Result([(1, "能小智·校园百事通", 1)]),
            _Result([(1, "门户Web", 1)]),
            _Result([("本科生",), ("访客",), ("本科生",)]),
        ])
        opts = chatlog_admin.filter_options()
        check("筛选项来自数据库并去重",
              opts["roles"] == ["本科生", "访客"] and opts["store"] == "mysql")
        check("筛选项携带真实 agentId/channelId（JSON 分支为 None 占位）",
              opts["agents"][0]["agentId"] == 1 and opts["channels"][0]["channelId"] == 1)
    finally:
        settings.kb_store = "json"


def _agent_row(**over) -> tuple:
    from app.db import agent_store

    base = {
        "agentId": 1, "agentCode": "campus-default", "agentName": "能小智·校园百事通",
        "agentType": "chat", "systemPrompt": "你是校园助手", "modelCode": "default",
        "temperature": None, "topP": None, "maxTokens": None, "visibleRoles": "全部,本科生",
        "iconUrl": "", "description": "默认助手", "streamEnabled": 1,
        "riskReply": "", "fallbackReply": "",
        "retrievalConfig": {"topK": 0, "rerankTopn": 0, "minScore": 0.0, "depts": [], "ktypes": []},
        "status": 1,
        "createdAt": datetime(2026, 10, 1, 9, 0, 0),
        "updatedAt": datetime(2026, 10, 1, 9, 30, 0),
    }
    base.update(over)
    return tuple(base[k] for k in agent_store._COL_KEYS)


def _chatlog_results() -> list[_Result]:
    """``_load_mysql`` 的 5 次查询：主查询 → 助手名 → 渠道名 → 引用数 → token。"""
    rows = [
        (101, "S-M1", 1, "校园卡如何补办", None, "", datetime(2026, 10, 1, 9, 0, 10),
         "本科生", 1, 1, datetime(2026, 10, 1, 9, 0, 0)),
        (102, "S-M1", 2, "请携带校园卡到一卡通中心办理。", 0.8123, "T-1",
         datetime(2026, 10, 1, 9, 0, 12), "本科生", 1, 1, datetime(2026, 10, 1, 9, 0, 0)),
        (201, "S-M2", 1, "今天食堂几点开门", None, "", datetime(2026, 10, 1, 10, 0, 5),
         "访客", None, None, datetime(2026, 10, 1, 10, 0, 0)),
        (202, "S-M2", 2, "抱歉，知识库中未找到相关内容。", 0.2, "",
         datetime(2026, 10, 1, 10, 0, 8), "访客", None, None,
         datetime(2026, 10, 1, 10, 0, 0)),
    ]
    return [
        _Result(rows),
        _Result([(1, "能小智·校园百事通")]),
        _Result([(1, "门户Web")]),
        _Result([(102, 2)]),
        _Result([("T-1", "qwen-max", 100, 50, 150, 800)]),
    ]


# ==================================================================== main
def main() -> int:
    parser = argparse.ArgumentParser(description="MySQL 分支桩验收（任务 2.1/2.2）")
    parser.add_argument("--verbose", action="store_true", help="打印临时目录等细节")
    args = parser.parse_args()

    from app.config import settings

    tmpdir = Path(tempfile.mkdtemp(prefix="verify-mysql-branch-"))
    print("=" * 68)
    print("MySQL 分支桩验收（任务 2.1 / 2.2）")
    print(f"存储模式：{settings.store_label}（本工具用假连接，不需要数据库）")
    if args.verbose:
        print(f"临时数据目录：{tmpdir}")
    print("实机验收请在有库环境跑：verify_agents.py / verify_chatlogs.py /")
    print("verify_model.py --store mysql，以及 pytest tests/test_mysql_store.py")
    print("=" * 68)

    try:
        check_shape_parity(tmpdir)
        check_null_semantics()
        check_filter_pushdown()
    finally:
        restore_all()
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("\n" + "=" * 68)
    print(f"结果：{PASSED} PASS / {FAILED} FAIL")
    print("=" * 68)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
