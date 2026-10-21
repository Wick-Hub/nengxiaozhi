"""MySQL 分支的**桩测试**（任务 2.1 / 2.2 / 2.4 的数据层补测）。

## 为什么需要这个文件

``tests/test_mysql_store.py`` 里的 MySQL 测试全部依赖真实数据库（``ping()`` 不通即
``skip``）。但 2.1（会话日志）、2.2（助手管理）、2.4（模型管理）三层各自新增了
**MySQL 专属代码路径**——``_row_to_dict`` 的归一化、``_load_mysql`` 的**筛选下推**、
``_payload`` 的 NULL 语义——这些路径在无库环境下**一行都没被执行过**。

本文件用**场景驱动的假连接**（``_Conn``）覆盖它们：

* 假连接按调用顺序**发牌**返回结果集，并记录收到的语句；
* 断言分两类：
  1. **语义**：行数据 → 对外 dict 的归一化结果（角色拆分、NULL 生成参数、
     Key 掩码、时间格式）必须与 JSON 后端**同形**；
  2. **下推**：会话级筛选/时间范围必须出现在 **SQL 里**而不是内存过滤
     （用 ``literal_binds`` 编译出可读 SQL 后断言）。

**边界（如实声明）**：桩测试验证的是「给定行数据，归一化与 SQL 形状对不对」，
不验证 MySQL 语法、类型映射、字符集与事务语义——那些由 ``test_mysql_store.py``
在有库环境验证。两者互补，不互相替代。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.config import settings
from app.db import agent_store
from app.services import chatlog_admin

# ------------------------------------------------------------------ 假连接


class _Result:
    """最小结果集替身：被测代码只用到 ``all/first/scalar/rowcount/inserted_primary_key``。"""

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
    """按顺序发牌的假连接；语句全部留存供断言。"""

    def __init__(self, results) -> None:
        self._queue = list(results)
        self.statements: list = []

    def execute(self, stmt, *args, **kwargs) -> _Result:
        self.statements.append(stmt)
        if not self._queue:
            raise AssertionError(
                "MySQL 桩：SQL 调用次数多于预期——被测代码新增了查询？"
                f"已收到的第 {len(self.statements)} 条语句：{_sql(stmt)[:200]}"
            )
        return self._queue.pop(0)

    # 便于断言
    @property
    def sql(self) -> list[str]:
        return [_sql(s) for s in self.statements]


class _Session:
    """``db_session()`` / ``db_transaction()`` 的上下文管理器替身。"""

    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    def __enter__(self) -> _Conn:
        return self._conn

    def __exit__(self, *exc) -> bool:
        return False


def _sql(stmt) -> str:
    """编译成 SQL 文本（用于断言「筛选是否下推」）。

    ``literal_binds`` 对 JSON 参数会失败（无字面量渲染器），此时退回带占位符的
    形态——这类断言改看 ``compile().params``（见各用例）。
    """
    try:
        return str(stmt.compile(compile_kwargs={"literal_binds": True}))
    except Exception:  # noqa: BLE001 - 仅为诊断输出，不参与业务
        return str(stmt.compile())


def _install(monkeypatch, module, results) -> _Conn:
    """把模块内的 ``db_session`` / ``db_transaction`` 换成假连接。"""
    conn = _Conn(results)
    monkeypatch.setattr(module, "db_session", lambda: _Session(conn))
    if hasattr(module, "db_transaction"):
        monkeypatch.setattr(module, "db_transaction", lambda: _Session(conn))
    return conn


def _forbid_json(monkeypatch, module, name: str, *, label: str) -> None:
    """断言 MySQL 分支**绝不回退读 JSON**（否则「双后端语义一致」是假的）。"""

    def boom(*_a, **_k):
        raise AssertionError(f"MySQL 模式不应调用 {label}")

    monkeypatch.setattr(module, name, boom)


@pytest.fixture
def mysql_mode(monkeypatch):
    """切到 MySQL 模式（只改标签，不建连接）。"""
    monkeypatch.setattr(settings, "kb_store", "mysql")
    assert settings.store_label == "mysql"
    yield


# ------------------------------------------------------------------ 行构造
def _agent_row(**over) -> tuple:
    """按 ``agent_store._COL_KEYS`` 顺序造一行（键名与 ``_COLS`` 一一对应）。"""
    base = {
        "agentId": 1,
        "agentCode": "campus-default",
        "agentName": "能小智·校园百事通",
        "agentType": "chat",
        "systemPrompt": "你是校园助手",
        "modelCode": "default",
        "temperature": None,
        "topP": None,
        "maxTokens": None,
        "visibleRoles": "全部,本科生",
        "iconUrl": "",
        "description": "默认助手",
        "streamEnabled": 1,
        "riskReply": "",
        "fallbackReply": "",
        "retrievalConfig": {"topK": 0, "rerankTopn": 0, "minScore": 0.0, "depts": [], "ktypes": []},
        "guideQuestions": ["学费怎么交？", "差旅怎么报销？"],
        "status": 1,
        "createdAt": datetime(2026, 10, 1, 9, 0, 0),
        "updatedAt": datetime(2026, 10, 1, 9, 30, 0),
    }
    base.update(over)
    return tuple(base[k] for k in agent_store._COL_KEYS)


# ================================================================== 助手存储（2.2）
def test_list_agents_mysql_pushes_status_filter(mysql_mode, monkeypatch) -> None:
    conn = _install(monkeypatch, agent_store, [_Result([_agent_row(), _agent_row(agentId=2)])])
    _forbid_json(monkeypatch, agent_store, "_read_all", label="agents.json")

    items = agent_store.list_agents(status=1)
    assert len(items) == 2
    sql = conn.sql[0]
    assert "FROM agent" in sql
    assert "agent.status = 1" in sql
    assert "ORDER BY agent.agent_id" in sql


def test_list_agents_mysql_normalizes_row(mysql_mode, monkeypatch) -> None:
    """同一行数据在两条后端上必须给出**同形**的对外 dict。"""
    _install(monkeypatch, agent_store, [_Result([_agent_row()])])
    item = agent_store.list_agents()[0]

    # 角色：库里是逗号串，对外必须是 list
    assert item["visibleRoles"] == ["全部", "本科生"]
    # 生成参数：NULL 原样保留 = 沿用平台默认，**不物化**成具体数值
    assert item["temperature"] is None
    assert item["topP"] is None
    assert item["maxTokens"] is None
    # 时间：datetime → ISO 秒级字符串
    assert item["createdAt"] == "2026-10-01T09:00:00"
    assert item["updatedAt"] == "2026-10-01T09:30:00"
    assert item["statusName"] == "已上架"
    assert item["streamEnabled"] == 1
    # 检索配置：JSON 列原样 dict → 补全默认键
    assert item["retrievalConfig"]["topK"] == 0
    assert set(item["retrievalConfig"]) == {"topK", "rerankTopn", "minScore", "depts", "ktypes"}
    # 引导问题（任务 3.8）：JSON 列 → 去空白/去重后的字符串数组
    assert item["guideQuestions"] == ["学费怎么交？", "差旅怎么报销？"]


def test_row_to_dict_tolerates_broken_retrieval_json(mysql_mode) -> None:
    """坏 JSON / 非 dict 一律降级为默认值，绝不抛异常（检索配置不该让问答整体失败）。"""
    broken = agent_store._row_to_dict(_agent_row(retrievalConfig="{不是 JSON"))
    assert broken["retrievalConfig"] == agent_store.default_retrieval()
    scalar = agent_store._row_to_dict(_agent_row(retrievalConfig=42))
    assert scalar["retrievalConfig"] == agent_store.default_retrieval()


def test_row_to_dict_accepts_json_string(mysql_mode) -> None:
    """有些 MySQL 驱动把 JSON 列按字符串回传，也必须解析。"""
    row = _agent_row(
        retrievalConfig='{"topK": 8, "depts": ["教务处"]}',
        guideQuestions='["学费怎么交？", " 差旅怎么报销？ ", "学费怎么交？"]',
    )
    out = agent_store._row_to_dict(row)
    assert out["retrievalConfig"]["topK"] == 8
    assert out["retrievalConfig"]["depts"] == ["教务处"]
    assert out["guideQuestions"] == ["学费怎么交？", "差旅怎么报销？"]


def test_get_agent_mysql_missing_returns_none(mysql_mode, monkeypatch) -> None:
    conn = _install(monkeypatch, agent_store, [_Result([])])
    assert agent_store.get_agent(999) is None
    assert "agent.agent_id = 999" in conn.sql[0]


def test_get_by_code_mysql(mysql_mode, monkeypatch) -> None:
    conn = _install(monkeypatch, agent_store, [_Result([_agent_row(agentCode="t-x")])])
    got = agent_store.get_by_code("t-x")
    assert got is not None and got["agentCode"] == "t-x"
    assert "agent.agent_code = 't-x'" in conn.sql[0]


def test_create_agent_mysql_inserts_then_reads_back(mysql_mode, monkeypatch) -> None:
    conn = _install(
        monkeypatch,
        agent_store,
        [_Result(inserted=7, rowcount=1), _Result([_agent_row(agentId=7, agentCode="new-1")])],
    )
    created = agent_store.create_agent(
        {"agentCode": "new-1", "agentName": "新助手", "temperature": 0.0}
    )
    assert created["agentId"] == 7

    insert_params = conn.statements[0].compile().params
    # 关键：显式传入的 0.0 必须原样落库（0.0 是合法温度，不是哨兵值）
    assert insert_params["temperature"] == 0.0
    # 未传的 top_p / max_tokens 落 NULL
    assert insert_params["top_p"] is None
    assert insert_params["max_tokens"] is None
    # 空 modelCode 收敛到保留编码 default（不落库到 llm_model）
    assert insert_params["model_code"] == agent_store.DEFAULT_MODEL_CODE
    assert insert_params["visible_roles"] == "全部"
    # 未传引导问题 → 落空数组（NULL 与 [] 等价，读路径统一补 []）
    assert insert_params["guide_questions"] == []


def test_update_agent_mysql_merges_retrieval_and_keeps_null(mysql_mode, monkeypatch) -> None:
    """增量语义：只覆盖显式传入的键；未传的检索键保留原值。"""
    current = _agent_row(retrievalConfig={"topK": 9, "depts": ["图书馆"]})
    conn = _install(
        monkeypatch,
        agent_store,
        [_Result([current]), _Result(rowcount=1), _Result([current])],
    )
    agent_store.update_agent(1, {"retrievalConfig": {"minScore": 0.5}})

    update_params = conn.statements[1].compile().params
    assert update_params["retrieval_config"]["topK"] == 9  # 未被清空
    assert update_params["retrieval_config"]["minScore"] == 0.5
    assert update_params["retrieval_config"]["depts"] == ["图书馆"]
    # 只改检索配置 → 生成参数仍为 NULL
    assert update_params["temperature"] is None
    # 更新按主键定位（UPDATE 的 JSON 参数无字面量渲染，改看 params）
    assert update_params["agent_id_1"] == 1
    assert "UPDATE agent" in conn.sql[1]


def test_update_agent_mysql_missing_returns_none(mysql_mode, monkeypatch) -> None:
    conn = _install(monkeypatch, agent_store, [_Result([])])
    assert agent_store.update_agent(404, {"agentName": "x"}) is None
    assert len(conn.statements) == 1  # 不存在就不该发 UPDATE


def test_delete_agent_mysql_uses_rowcount(mysql_mode, monkeypatch) -> None:
    conn = _install(monkeypatch, agent_store, [_Result(rowcount=1)])
    assert agent_store.delete_agent(3) is True
    assert "DELETE FROM agent" in conn.sql[0]

    _install(monkeypatch, agent_store, [_Result(rowcount=0)])
    assert agent_store.delete_agent(3) is False


def test_count_channels_using_mysql(mysql_mode, monkeypatch) -> None:
    conn = _install(monkeypatch, agent_store, [_Result([(2,)])])
    assert agent_store.count_channels_using(1) == 2
    assert "channel.default_agent_id = 1" in conn.sql[0]

    _install(monkeypatch, agent_store, [_Result([(None,)])])
    assert agent_store.count_channels_using(1) == 0


# ================================================================== 会话日志（2.1）
#: ``_load_mysql`` 主查询的列顺序（12 列，位置取值）
def _msg_row(
    msg_id: int,
    session_id: str,
    msg_role: int,
    content: str,
    *,
    confidence: float | None,
    trace_id: str,
    created_at: datetime,
    role_snapshot: str,
    agent_id: int | None,
    channel_id: int | None,
    session_created: datetime,
    refused: int = 0,
) -> tuple:
    return (
        msg_id, session_id, msg_role, content, confidence, refused, trace_id, created_at,
        role_snapshot, agent_id, channel_id, session_created,
    )


SEED_ROWS = [
    _msg_row(
        101, "S-M1", 1, "校园卡如何补办",
        confidence=None, trace_id="",
        created_at=datetime(2026, 10, 1, 9, 0, 10),
        role_snapshot="本科生", agent_id=1, channel_id=1,
        session_created=datetime(2026, 10, 1, 9, 0, 0),
    ),
    _msg_row(
        102, "S-M1", 2, "请携带校园卡到一卡通中心办理。",
        confidence=0.8123, trace_id="T-1",
        created_at=datetime(2026, 10, 1, 9, 0, 12),
        role_snapshot="本科生", agent_id=1, channel_id=1,
        session_created=datetime(2026, 10, 1, 9, 0, 0),
    ),
    _msg_row(
        201, "S-M2", 1, "今天食堂几点开门",
        confidence=None, trace_id="",
        created_at=datetime(2026, 10, 1, 10, 0, 5),
        role_snapshot="访客", agent_id=None, channel_id=None,
        session_created=datetime(2026, 10, 1, 10, 0, 0),
    ),
    _msg_row(
        202, "S-M2", 2, "抱歉，知识库中未找到相关内容。",
        confidence=0.2, refused=1, trace_id="",
        created_at=datetime(2026, 10, 1, 10, 0, 8),
        role_snapshot="访客", agent_id=None, channel_id=None,
        session_created=datetime(2026, 10, 1, 10, 0, 0),
    ),
]


def _seed_results(rows=None, *, citations=None, usage=None) -> list[_Result]:
    """``_load_mysql`` 的 5 次查询：主查询 → 助手名 → 渠道名 → 引用数 → token。"""
    rows = SEED_ROWS if rows is None else rows
    ai_ids = [r[0] for r in rows if int(r[2]) == chatlog_admin.ROLE_AI]
    traces = [(r[6], r[2]) for r in rows if r[6] and int(r[2]) == chatlog_admin.ROLE_AI]
    out = [
        _Result(rows),
        _Result([(1, "能小智·校园百事通")]),
        _Result([(1, "门户Web")]),
    ]
    if ai_ids:
        out.append(_Result(citations if citations is not None else [(102, 2)]))
    if any(t for t, _ in traces):
        out.append(
            _Result(usage if usage is not None else [("T-1", "qwen-max", 100, 50, 150, 800)])
        )
    return out


def test_load_mysql_pushes_session_filters_down_to_sql(mysql_mode, monkeypatch) -> None:
    """会话级筛选与时间范围**必须下推到 SQL**——否则 SCAN_LIMIT 窗口会被无关消息占满。"""
    conn = _install(monkeypatch, chatlog_admin, _seed_results())
    _forbid_json(monkeypatch, chatlog_admin, "_load_json", label="JSON 后端")

    chatlog_admin.query_qa(
        role="本科生",
        agent_id=1,
        channel_id=1,
        date_from="2026-10-01",
        date_to="2026-10-01",
        page_size=50,
    )
    sql = conn.sql[0]
    assert "JOIN chat_session" in sql
    assert "chat_session.role_snapshot = '本科生'" in sql
    assert "chat_session.agent_id = 1" in sql
    assert "chat_session.channel_id = 1" in sql
    assert "chat_message.created_at >= '2026-10-01 00:00:00'" in sql
    assert "chat_message.created_at <= '2026-10-01 23:59:59'" in sql
    assert "LIMIT 2000" in sql  # 扫描窗口显式可见


def test_load_mysql_no_filters_means_no_where(mysql_mode, monkeypatch) -> None:
    conn = _install(monkeypatch, chatlog_admin, _seed_results())
    chatlog_admin.query_qa(page_size=50)
    sql = conn.sql[0]
    assert "role_snapshot =" not in sql
    assert "created_at >=" not in sql


def test_load_mysql_normalizes_rows_and_usage(mysql_mode, monkeypatch) -> None:
    _install(monkeypatch, chatlog_admin, _seed_results())
    data = chatlog_admin.query_qa(page_size=50)

    assert data["store"] == "mysql"
    assert data["total"] == 2
    assert data["scanned"] == 4

    # 排序：回答时间倒序（S-M2 在后 → 排在前）
    first, second = data["items"]
    assert first["msgId"] == 202 and second["msgId"] == 102

    # token / 模型来自 token_usage（按 trace_id 关联）
    assert second["model"] == "qwen-max"
    assert second["promptTokens"] == 100
    assert second["completionTokens"] == 50
    assert second["totalTokens"] == 150
    assert second["latencyMs"] == 800
    # 无 trace 的回答不带 token
    assert first["model"] is None and first["totalTokens"] is None

    # 引用数来自 answer_citation 聚合
    assert second["citationCount"] == 2
    assert first["citationCount"] == 0
    # 问答配对：AI 消息向前找同会话最大 msg_id 的用户消息
    assert second["question"] == "校园卡如何补办"
    assert first["question"] == "今天食堂几点开门"


def test_load_mysql_agent_and_channel_names_from_db(mysql_mode, monkeypatch) -> None:
    _install(monkeypatch, chatlog_admin, _seed_results())
    items = chatlog_admin.query_qa(page_size=50)["items"]
    by_id = {i["msgId"]: i for i in items}

    assert by_id[102]["role"] == "本科生"
    assert by_id[102]["agentName"] == "能小智·校园百事通"
    assert by_id[102]["channelName"] == "门户Web"
    assert by_id[102]["agentId"] == 1 and by_id[102]["channelId"] == 1

    # agent_id 为 NULL 的会话：**不造假数据**，名字保持 None（前端显示「—」）
    assert by_id[202]["agentId"] is None
    assert by_id[202]["agentName"] is None
    assert by_id[202]["channelName"] is None
    assert by_id[202]["role"] == "访客"


def test_load_mysql_reads_refused_column(mysql_mode, monkeypatch) -> None:
    """3.10 起 ``chat_message.refused`` 显式落库，不再按兜底话术猜测。"""
    _install(monkeypatch, chatlog_admin, _seed_results())
    items = {i["msgId"]: i for i in chatlog_admin.query_qa(page_size=50)["items"]}
    assert items[202]["refused"] is True
    assert items[102]["refused"] is False


def test_load_mysql_skips_citation_query_without_ai_rows(mysql_mode, monkeypatch) -> None:
    """只有用户消息时不发引用数查询（少一次往返）。"""
    rows = [r for r in SEED_ROWS if int(r[2]) == chatlog_admin.ROLE_USER]
    conn = _install(monkeypatch, chatlog_admin, _seed_results(rows))
    data = chatlog_admin.query_qa(page_size=50)
    assert data["total"] == 0
    assert len(conn.statements) == 3  # 主查询 + 助手名 + 渠道名，无引用/ token 查询


def test_query_users_mysql_aggregates_by_session(mysql_mode, monkeypatch) -> None:
    _install(monkeypatch, chatlog_admin, _seed_results())
    data = chatlog_admin.query_users(page_size=50)

    assert data["total"] == 2
    by_session = {u["sessionId"]: u for u in data["items"]}
    assert by_session["S-M1"]["questionCount"] == 1
    assert by_session["S-M1"]["totalTokens"] == 150
    assert by_session["S-M1"]["lastQuestion"] == "校园卡如何补办"
    assert by_session["S-M2"]["refusedCount"] == 1
    assert by_session["S-M2"]["totalTokens"] is None


def test_filter_options_mysql_reads_db(mysql_mode, monkeypatch) -> None:
    conn = _install(
        monkeypatch,
        chatlog_admin,
        [
            _Result([(1, "能小智·校园百事通", 1)]),
            _Result([(1, "门户Web", 1)]),
            _Result([("本科生",), ("访客",), ("本科生",)]),
        ],
    )
    _forbid_json(monkeypatch, chatlog_admin, "_load_json", label="JSON 后端")
    opts = chatlog_admin.filter_options()

    assert opts["store"] == "mysql"
    assert opts["agents"] == [{"agentId": 1, "agentName": "能小智·校园百事通", "status": 1}]
    assert opts["channels"] == [{"channelId": 1, "channelName": "门户Web", "status": 1}]
    assert opts["roles"] == ["本科生", "访客"]  # 去重，**不是** JSON 分支的占位数据
    assert "SELECT DISTINCT chat_session.role_snapshot" in conn.sql[2]


def test_filter_options_mysql_empty_tables(mysql_mode, monkeypatch) -> None:
    """空库时返回空列表，而不是 JSON 分支的「默认」占位。"""
    _install(monkeypatch, chatlog_admin, [_Result([]), _Result([]), _Result([])])
    opts = chatlog_admin.filter_options()
    assert opts["agents"] == [] and opts["channels"] == [] and opts["roles"] == []


def test_export_uses_same_collect_path_as_page(mysql_mode, monkeypatch) -> None:
    """导出与分页共用 ``_collect``：MySQL 分支下「导出 = 页面所见」同样成立。"""
    _install(monkeypatch, chatlog_admin, _seed_results())
    page = chatlog_admin.query_qa(page_size=50)
    # 导出会重新取数（不缓存页面结果），因此再发一次同样的牌
    _install(monkeypatch, chatlog_admin, _seed_results())
    rows, truncated = chatlog_admin.export_rows("qa")
    assert truncated is False
    assert [r["msgId"] for r in rows] == [r["msgId"] for r in page["items"]]

    _install(monkeypatch, chatlog_admin, _seed_results())
    csv_text = chatlog_admin.to_csv("qa", rows)
    assert csv_text.startswith("\ufeff")  # UTF-8 BOM：Excel 直接打开不乱码
    assert "校园卡如何补办" in csv_text
