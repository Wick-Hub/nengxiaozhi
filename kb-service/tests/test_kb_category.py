"""知识库分类管理（任务 2.5 / M-K-01·02）测试。

覆盖两条路径：

* **JSON 模式（只读推导）**：``list_categories`` 从 ``get_index().docs`` 按
  （部门 × 知识类型）组合出分类，``editable=False``；写操作返回 400 + 指引。
* **MySQL 模式（桩）**：用场景驱动的假连接覆盖 ``kb_category`` 全链路
  （列表 join + doc_count、按名称解析部门、幂等建分类、重命名、删除前
   校验「有文档 / 有子分类」），SQL 形状用 ``literal_binds`` 断言。

桩测试同样**不验证真实 MySQL 语法**——那是 ``test_mysql_store.py`` 的职责。
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.db import kb_repo
from app.services import kb_category

# ------------------------------------------------------------------ 假连接（与 test_store_mysql_stub 同款）


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

    @property
    def sql(self) -> list[str]:
        return [_sql(s) for s in self.statements]


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
    except Exception:  # noqa: BLE001 - 仅为诊断输出
        return str(stmt.compile())


def _install(monkeypatch, results) -> _Conn:
    """把数据层的 ``db_session`` / ``db_transaction`` 换成假连接。

    注意：``kb_category`` 模块从 ``app.db.session`` 导入 ``db_session /
    db_transaction``（写操作与按名称解析部门用），而 **``kb_repo`` 模块也持有
    自己的 ``db_session``**（``list_categories`` 等查询直接用它）——两处都要打点，
    否则列表查询会落到真实数据库。
    """
    conn = _Conn(results)
    monkeypatch.setattr(kb_category, "db_session", lambda: _Session(conn))
    monkeypatch.setattr(kb_category, "db_transaction", lambda: _Session(conn))
    monkeypatch.setattr(kb_repo, "db_session", lambda: _Session(conn))
    return conn


def _forbid_json(monkeypatch) -> None:
    """断言 MySQL 分支绝不回退读 JSON。"""

    def boom(*_a, **_k):
        raise AssertionError("MySQL 模式不应调用 get_index")

    monkeypatch.setattr(kb_category, "get_index", boom)


@pytest.fixture
def mysql_mode(monkeypatch):
    monkeypatch.setattr(settings, "kb_store", "mysql")
    assert settings.store_label == "mysql"
    yield


# ------------------------------------------------------------------ 行构造（按查询 SELECT 顺序）

def _category_row(**over) -> tuple:
    """list_categories 的 SELECT 顺序：
    category_id, dept_id, ktype, category_name, parent_id, sort_no, org_name, doc_count。
    """
    base = {
        "categoryId": 1,
        "deptId": 10,
        "ktype": "policy",
        "categoryName": "政策文件",
        "parentId": 0,
        "sortNo": 1,
        "orgName": "教务处",
        "docCount": 3,
    }
    base.update(over)
    return tuple(base[k] for k in (
        "categoryId", "deptId", "ktype", "categoryName", "parentId", "sortNo",
        "orgName", "docCount",
    ))


def _single_row(over: dict | None = None) -> tuple:
    """get_category 的 SELECT 顺序：
    category_id, dept_id, ktype, category_name, parent_id, sort_no, org_name。
    """
    base = {
        "categoryId": 1,
        "deptId": 10,
        "ktype": "policy",
        "categoryName": "政策文件",
        "parentId": 0,
        "sortNo": 1,
        "orgName": "教务处",
    }
    base.update(over or {})
    return tuple(base[k] for k in (
        "categoryId", "deptId", "ktype", "categoryName", "parentId", "sortNo", "orgName",
    ))


def _patch_index(monkeypatch, docs: list[dict]) -> None:
    """替换 get_index 返回的索引快照（避免依赖 data/store.json）。

    kb_category 模块以 ``from app.core.index_store import get_index`` 引入，
    因此要打点模块内引用。
    """

    class _Idx:
        def __init__(self) -> None:
            self.error = None
            self.docs = docs

    def _fake_get_index(*_a, **_k):
        return _Idx()

    monkeypatch.setattr(kb_category, "get_index", _fake_get_index)


# ================================================================== JSON 模式（只读推导）
def test_json_list_categories_derives_from_index(monkeypatch) -> None:
    docs = [
        {"dept": "教务处", "ktype": "政策文件", "title": "a"},
        {"dept": "教务处", "ktype": "政策文件", "title": "b"},
        {"dept": "教务处", "ktype": "办事指南", "title": "c"},
        {"dept": "财务处", "ktype": "办事指南", "title": "d"},
        {"dept": "财务处", "ktype": "办事指南", "title": "e"},
    ]
    monkeypatch.setattr(settings, "kb_store", "json")
    _patch_index(monkeypatch, docs)

    res = kb_category.list_categories()
    assert res["ok"], res
    data = res["data"]
    assert data["editable"] is False
    assert data["total"] == 3
    assert data["docTotal"] == 5
    items = {f"{it['dept']}/{it['ktype']}": it for it in data["items"]}
    assert items["教务处/政策文件"]["docCount"] == 2
    assert items["教务处/办事指南"]["docCount"] == 1
    assert items["财务处/办事指南"]["docCount"] == 2
    # 派生分类无真实主键：前端不得依赖 categoryId
    assert items["教务处/政策文件"]["categoryId"] is None
    assert data["depts"] == ["教务处", "财务处"]
    assert "政策文件" in data["ktypes"] and "办事指南" in data["ktypes"]


def test_json_filter_by_dept_and_ktype(monkeypatch) -> None:
    docs = [
        {"dept": "教务处", "ktype": "政策文件"},
        {"dept": "教务处", "ktype": "办事指南"},
        {"dept": "财务处", "ktype": "政策文件"},
    ]
    monkeypatch.setattr(settings, "kb_store", "json")
    _patch_index(monkeypatch, docs)

    res = kb_category.list_categories(dept="教务处")
    assert res["ok"]
    assert all(it["dept"] == "教务处" for it in res["data"]["items"])
    assert res["data"]["total"] == 2

    res = kb_category.list_categories(dept="教务处", ktype="办事指南")
    assert res["ok"]
    assert res["data"]["total"] == 1
    assert res["data"]["items"][0]["ktype"] == "办事指南"


def test_json_write_ops_return_error_with_hint(monkeypatch) -> None:
    monkeypatch.setattr(settings, "kb_store", "json")
    assert kb_category.create_category({"dept": "教务处", "ktype": "政策文件"})["ok"] is False
    assert kb_category.update_category(1, {"categoryName": "x"})["ok"] is False
    assert kb_category.delete_category(1)["ok"] is False


# ================================================================== MySQL 模式（桩 CRUD）
def test_mysql_list_categories_joins_org_and_counts(mysql_mode, monkeypatch) -> None:
    _forbid_json(monkeypatch)
    conn = _install(monkeypatch, [_Result([_category_row()])])
    res = kb_category.list_categories()
    assert res["ok"], res
    item = res["data"]["items"][0]
    assert item["categoryId"] == 1
    assert item["dept"] == "教务处"
    assert item["ktype"] == "政策文件"
    assert item["docCount"] == 3
    assert res["data"]["editable"] is True
    assert res["data"]["depts"] == ["教务处"]
    joined = " ".join(conn.sql).lower()
    assert "sys_org" in joined and "kb_doc" in joined and "outer" in joined


def test_mysql_list_categories_empty(mysql_mode, monkeypatch) -> None:
    _install(monkeypatch, [_Result([])])
    res = kb_category.list_categories()
    assert res["ok"]
    assert res["data"]["items"] == []
    assert res["data"]["total"] == 0
    assert res["data"]["docTotal"] == 0


def test_mysql_create_category_resolves_dept_by_name(mysql_mode, monkeypatch) -> None:
    # 顺序：① kb_category.db_session 的 dept_id_by_name(S1)
    #      ② db_transaction：dept_exists(S2) → find_category 判重(S3)
    #         → repo.create_category 内部 find_category(S4) + insert(S5)
    #         → get_category(S6) + 内部 category_doc_count(S7)
    conn = _install(
        monkeypatch,
        [
            _Result([(10,)]),                          # S1 dept_id_by_name
            _Result([(10,)]),                          # S2 dept_exists（first() is not None）
            _Result([], rowcount=0),                   # S3 find_category 判重（未命中）
            _Result([], rowcount=0),                   # S4 repo.create_category 内部 find（未命中）
            _Result(inserted=41),                      # S5 INSERT
            _Result([_single_row({"categoryId": 41, "categoryName": "校级政策", "sortNo": 2})]),  # S6 get_category（回读插入后的值）
            _Result([], rowcount=1),                   # S7 category_doc_count → 0
        ],
    )
    res = kb_category.create_category(
        {"dept": "教务处", "ktype": "政策文件", "categoryName": "校级政策",
         "parentId": 0, "sortNo": 2}
    )
    assert res["ok"], res
    item = res["data"]
    assert item["categoryId"] == 41
    assert item["categoryName"] == "校级政策"
    sql_text = " ".join(conn.sql).replace("`", "")
    assert "INSERT INTO kb_category" in sql_text


def test_mysql_create_category_duplicate_returns_409(mysql_mode, monkeypatch) -> None:
    # deptId 直接给出：跳过 db_session 查询；db_transaction：dept_exists(S1) → find_category(S2 命中)
    _install(
        monkeypatch,
        [
            _Result([(10,)]),   # S1 dept_exists
            _Result([(7,)]),    # S2 find_category 命中 category_id=7
        ],
    )
    res = kb_category.create_category({"deptId": 10, "ktype": "policy"})
    assert res["ok"] is False
    assert res["status"] == 409
    assert "已存在" in res["error"]


def test_mysql_create_category_unknown_dept(mysql_mode, monkeypatch) -> None:
    _install(
        monkeypatch,
        [
            _Result([], rowcount=0),   # dept_exists → False
        ],
    )
    res = kb_category.create_category({"deptId": 999, "ktype": "policy"})
    assert res["ok"] is False
    assert res["status"] == 400
    assert "部门不存在" in res["error"]


def test_mysql_update_category_renames(mysql_mode, monkeypatch) -> None:
    # get_category(S1 + 内部 doc_count S2) → update(S3) → get_category(S4 + doc_count S5)
    conn = _install(
        monkeypatch,
        [
            _Result([_single_row()]),                    # S1 get_category
            _Result([], rowcount=1),                     # S2 category_doc_count → 0
            _Result([], rowcount=1),                     # S3 UPDATE
            _Result([_single_row({"categoryName": "新名称", "sortNo": 9})]),  # S4 get_category
            _Result([], rowcount=1),                     # S5 category_doc_count → 0
        ],
    )
    res = kb_category.update_category(1, {"categoryName": "新名称", "sortNo": 9})
    assert res["ok"], res
    assert res["data"]["categoryName"] == "新名称"
    assert res["data"]["sortNo"] == 9
    update_sql = [s for s in conn.sql if "UPDATE" in s.replace("`", "").upper()]
    assert update_sql
    assert "category_name" in update_sql[0] and "sort_no" in update_sql[0]


def test_mysql_update_category_missing_returns_404(mysql_mode, monkeypatch) -> None:
    _install(monkeypatch, [_Result([])])
    res = kb_category.update_category(99, {"categoryName": "x"})
    assert res["ok"] is False
    assert res["status"] == 404


def test_mysql_delete_category_with_docs_returns_409(mysql_mode, monkeypatch) -> None:
    # get_category 主查询（7 列）→ 内部 category_doc_count=3 → 409
    _install(
        monkeypatch,
        [
            _Result([_single_row()]),   # S1 get_category 主查询
            _Result([(3,)]),            # S2 内部 category_doc_count → 3
        ],
    )
    res = kb_category.delete_category(1)
    assert res["ok"] is False
    assert res["status"] == 409
    assert "3 篇文档" in res["error"]


def test_mysql_delete_category_with_children_returns_409(mysql_mode, monkeypatch) -> None:
    # get_category 主查询 → 内部 category_doc_count=0 → category_child_count=2 → 409
    _install(
        monkeypatch,
        [
            _Result([_single_row()]),   # S1 get_category 主查询
            _Result([], rowcount=1),    # S2 内部 category_doc_count → 0
            _Result([(2,)]),            # S3 category_child_count → 2
        ],
    )
    res = kb_category.delete_category(1)
    assert res["ok"] is False
    assert res["status"] == 409
    assert "2 个子分类" in res["error"]


def test_mysql_delete_category_ok(mysql_mode, monkeypatch) -> None:
    # get_category 主查询 → 内部 count=0 → category_child_count=0 → delete rowcount=1
    _install(
        monkeypatch,
        [
            _Result([_single_row()]),   # S1 get_category 主查询
            _Result([], rowcount=1),    # S2 内部 category_doc_count → 0
            _Result([], rowcount=1),    # S3 category_child_count → 0
            _Result([], rowcount=1),    # S4 DELETE
        ],
    )
    res = kb_category.delete_category(1)
    assert res["ok"], res
    assert res["data"]["deleted"] is True