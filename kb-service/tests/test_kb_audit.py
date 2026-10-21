"""文档审核与文件管理（任务 2.7 / M-K-05·09）测试。

覆盖三层：

1. **JSON 模式（默认存储）**：``GET /api/docs`` 的 ``status/dept/ktype``
   在内存里过滤，``data`` **保持数组**（向后兼容既有前端与测试）；
   ``categoryId`` 只有 MySQL 有对应列，JSON 模式传入不报错但不过滤；
   ``POST /api/docs/batch`` 必须返回 **400 + KB_STORE=mysql 指引**，
   而不是静默成功。
2. **MySQL 模式（repo 桩）**：``kb_repo.list_docs`` 的四个筛选必须下推到 SQL、
   ``categoryId`` 出现在返回 dict；``batch_update_status`` /
   ``batch_move_category`` / ``batch_delete_docs`` 的命中计数、
   空 id 短路与「目标分类不存在」错误路径。
3. **MySQL 模式（API 桩）**：``POST /api/docs/batch`` 四种 action 的端到端分发
   （publish/reject/delete/move）、``docIds`` 接受 ``D001`` 对外编号、
   逐个文档的授权前置查询、oplog 落库。

桩测试只验证「给定行数据，归一化与 SQL 形状对不对」——真实 MySQL 语法、
类型映射与事务语义由 ``test_mysql_store.py`` 在有库环境验证，两者互补。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.api import docs as docs_api
from app.config import settings
from app.db import kb_repo
from app.main import app
from app.security import oplog
from app.services import kb_permission

client = TestClient(app)


# ------------------------------------------------------------------ 假连接
class _Row(tuple):
    """行替身：**同时支持下标**（``row[0]``）**与属性**（``row.doc_id``）访问，
    与真实 SQLAlchemy Row 的行为一致（``dept_id_by_name`` 用下标，
    ``list_docs`` / ``get_doc`` 用属性，两种都得能喂）。"""

    def __new__(cls, **fields):
        keys = list(fields)
        obj = super().__new__(cls, [fields[k] for k in keys])
        obj._keys = keys  # noqa: SLF001 - 供 __getattr__ 反查列名
        return obj

    def __getattr__(self, name: str):
        try:
            return self[self._keys.index(name)]
        except ValueError:
            raise AttributeError(name) from None


class _Result:
    """最小结果集替身：被测代码只用到 ``all/first/scalar/rowcount``。"""

    def __init__(self, rows=(), *, rowcount: int = 0) -> None:
        self._rows = list(rows)
        self.rowcount = rowcount

    def all(self) -> list:
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        row = self.first()
        return row[0] if row is not None else None


class _Conn:
    """按调用顺序发牌的假连接；语句全部留存供断言。"""

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
    """``db_session()`` / ``db_transaction()`` 的上下文管理器替身。"""

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
    """把相关模块的 session 引用换成假连接。

    与 2.6 同样的坑：``kb_repo``（get_doc / batch_*）、``kb_permission``
    （resolve_dept_id 查 sys_org）、``oplog``（log_operation 写审计表）
    各自持有一份 ``db_session`` / ``db_transaction`` 引用，**每一处都要打点**，
    否则查询会落到真实数据库（MySQL 恰好在线时表现为「查不到部门」）。
    """
    conn = _Conn(results)
    sess = _Session(conn)
    monkeypatch.setattr(kb_repo, "db_session", lambda: sess)
    monkeypatch.setattr(kb_permission, "db_session", lambda: sess)
    monkeypatch.setattr(kb_permission, "db_transaction", lambda: sess)
    monkeypatch.setattr(oplog, "db_transaction", lambda: sess)
    return conn


@pytest.fixture
def mysql_mode(monkeypatch):
    monkeypatch.setattr(settings, "kb_store", "mysql")
    assert settings.store_label == "mysql"
    yield


# ------------------------------------------------------------------ 行构造
def _doc_row(**over) -> _Row:
    """按 ``kb_repo.list_docs`` 的 SELECT 顺序造一行。"""
    base = dict(
        doc_id=1,
        doc_title="校园网报修流程",
        category_id=3,
        ktype="policy",
        file_format="md",
        file_key="D001.md",
        file_size=2048,
        file_hash="h" * 32,
        chunk_count=2,
        visible_roles="全部",
        status=1,
        version_no=1,
        source_type="import",
        published_at=None,
        created_at=datetime(2026, 10, 1, 9, 0, 0),
        updated_at=datetime(2026, 10, 2, 10, 30, 0),
        org_name="教务处",
    )
    base.update(over)
    return _Row(**base)


# ================================================================== JSON 模式
def test_json_mode_docs_list_stays_array() -> None:
    """合同兼容：``data`` 必须是数组（既有前端与测试都按列表用）。"""
    body = client.get("/api/docs").json()
    assert body["ok"] is True
    assert isinstance(body["data"], list)
    assert body["data"]


def test_json_mode_status_filter_in_memory() -> None:
    all_docs = client.get("/api/docs").json()["data"]
    target = str(all_docs[0]["status"])
    filtered = client.get("/api/docs", params={"status": target}).json()["data"]
    assert filtered, "按状态过滤后不应为空"
    assert all(str(d["status"]) == target for d in filtered)


def test_json_mode_ktype_filter_in_memory() -> None:
    all_docs = client.get("/api/docs").json()["data"]
    ktype = all_docs[0].get("ktype")
    if not ktype:
        pytest.skip("索引中无 ktype 字段")
    filtered = client.get("/api/docs", params={"ktype": ktype}).json()["data"]
    assert filtered
    assert all(d.get("ktype") == ktype for d in filtered)


def test_json_mode_category_filter_is_accepted() -> None:
    """JSON 模式没有分类表：传 categoryId 不报错，也不筛掉任何文档。"""
    resp = client.get("/api/docs", params={"categoryId": 1})
    assert resp.status_code == 200
    assert isinstance(resp.json()["data"], list)


def test_json_mode_batch_rejected_with_hint() -> None:
    """JSON 模式下批量操作要给可执行指引，而不是静默成功。"""
    resp = client.post("/api/docs/batch", json={"docIds": [1], "action": "publish"})
    assert resp.status_code == 400
    assert "KB_STORE=mysql" in resp.json()["detail"]


def test_batch_requires_known_action() -> None:
    """action 是 Literal：未知值在 schema 层就被拒（422）。"""
    resp = client.post("/api/docs/batch", json={"docIds": [1], "action": "explode"})
    assert resp.status_code == 422


def test_batch_requires_doc_ids() -> None:
    resp = client.post("/api/docs/batch", json={"docIds": [], "action": "publish"})
    assert resp.status_code == 422


# ================================================================== repo 桩
def test_list_docs_returns_admin_fields(mysql_mode, monkeypatch) -> None:
    conn = _install(monkeypatch, [_Result([_doc_row()])])
    docs = kb_repo.list_docs()
    assert len(docs) == 1
    d = docs[0]
    assert d["docId"] == 1
    assert d["categoryId"] == 3
    assert d["dept"] == "教务处"
    assert d["ktype"] == "政策文件"          # 枚举 → 中文名
    assert d["status"] == 1 and d["statusName"] == "待审核"
    assert d["updatedAt"] == "2026-10-02 10:30:00"
    # 无筛选 → 不带 WHERE
    assert "WHERE" not in conn.sql[0].upper()


def test_list_docs_pushes_status_to_sql(mysql_mode, monkeypatch) -> None:
    conn = _install(monkeypatch, [_Result([])])
    kb_repo.list_docs(status=2)
    sql = conn.sql[0]
    assert "WHERE" in sql.upper()
    assert "status" in sql.lower()


def test_list_docs_pushes_ktype_enum_to_sql(mysql_mode, monkeypatch) -> None:
    conn = _install(monkeypatch, [_Result([])])
    kb_repo.list_docs(ktype_raw="政策文件")
    # 中文名必须在 repo 层映射成生产枚举后再进 SQL
    assert "'policy'" in conn.sql[0]


def test_list_docs_pushes_category_and_dept_to_sql(mysql_mode, monkeypatch) -> None:
    conn = _install(monkeypatch, [_Result([])])
    kb_repo.list_docs(category_id=7, dept_name="教务处")
    sql = conn.sql[0]
    assert "category_id" in sql.lower()
    assert "org_name" in sql.lower()


def test_batch_update_status_counts_rowcount(mysql_mode, monkeypatch) -> None:
    _install(monkeypatch, [_Result(rowcount=2)])
    res = kb_repo.batch_update_status([1, 2], 2)
    assert res == {"ok": True, "updated": 2, "status": 2, "docIds": [1, 2]}


def test_batch_update_status_empty_ids_short_circuits(mysql_mode, monkeypatch) -> None:
    conn = _install(monkeypatch, [])
    res = kb_repo.batch_update_status([], 2)
    assert res["updated"] == 0
    assert conn.statements == [], "空 id 不应产生 SQL"


def test_batch_move_category_validates_target(mysql_mode, monkeypatch) -> None:
    _install(monkeypatch, [_Result([])])
    res = kb_repo.batch_move_category([1, 2], 99)
    assert res["ok"] is False
    assert "目标分类不存在" in res["error"]


def test_batch_move_category_to_existing_target(mysql_mode, monkeypatch) -> None:
    conn = _install(monkeypatch, [_Result([_Row(category_id=3)]), _Result(rowcount=2)])
    res = kb_repo.batch_move_category([1, 2], 3)
    assert res["ok"] is True and res["updated"] == 2
    assert res["categoryId"] == 3
    assert len(conn.statements) == 2  # 先校验存在，再更新


def test_batch_move_category_to_uncategorized(mysql_mode, monkeypatch) -> None:
    """categoryId=0 → 「未分类」，无需校验目标存在。"""
    conn = _install(monkeypatch, [_Result(rowcount=1)])
    res = kb_repo.batch_move_category([1], None)
    assert res["ok"] is True and res["categoryId"] == 0
    assert len(conn.statements) == 1


def test_batch_delete_docs_counts(mysql_mode, monkeypatch) -> None:
    # 每篇文档：引用 → 向量 → 切片 → 文档，共 4 条删除语句
    _install(monkeypatch, [_Result(rowcount=1)] * 4 + [_Result(rowcount=0)] * 4)
    res = kb_repo.batch_delete_docs([1, 2])
    assert res["ok"] is True and res["deleted"] == 1


def test_kb_admin_forwards_filters(mysql_mode, monkeypatch) -> None:
    """服务层必须把四个筛选转给 repo（对外名 ktype/dept → repo 形参）。"""
    seen: dict = {}

    def fake_list_docs(**kw):
        seen.update(kw)
        return []

    monkeypatch.setattr(kb_repo, "list_docs", fake_list_docs)
    from app.services import kb_admin

    kb_admin.list_docs(status=1, ktype="政策文件", category_id=3, dept="教务处")
    assert seen == {
        "status": 1,
        "ktype_raw": "政策文件",
        "category_id": 3,
        "dept_name": "教务处",
    }


# ================================================================== API 桩
def _perms_for(doc_ids) -> list[_Result]:
    """每篇文档的授权前置查询：get_doc + 部门名 → org_id。"""
    out: list[_Result] = []
    for _ in doc_ids:
        out.append(_Result([_doc_row()]))
        out.append(_Result([_Row(org_id=100)]))
    return out


def _publish_queue(doc_ids) -> list[_Result]:
    """publish/reject：授权前置 + 一次批量更新 + oplog。"""
    return _perms_for(doc_ids) + [
        _Result(rowcount=len(doc_ids)),
        _Result(rowcount=1),   # oplog 落库
    ]


def test_batch_publish_endpoint(mysql_mode, monkeypatch) -> None:
    conn = _install(monkeypatch, _publish_queue([1, 2]))
    resp = client.post(
        "/api/docs/batch",
        json={"docIds": ["D001", 2], "action": "publish"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["updated"] == 2 and data["status"] == 2
    # D001 → 1：对外编号与自增 id 混用也要能解析（schema 层不许先 422），
    # 且解析后的自增 id 才是进 SQL 的东西
    assert docs_api._parse_doc_id("D001") == 1
    # 倒数第二条是批量 UPDATE（最后一条是 oplog 的 INSERT）
    assert "UPDATE" in conn.sql[-2].upper()
    assert "D001" not in conn.sql[-2]


def test_batch_reject_endpoint(mysql_mode, monkeypatch) -> None:
    _install(monkeypatch, _publish_queue([1]))
    resp = client.post(
        "/api/docs/batch",
        json={"docIds": [1], "action": "reject"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["updated"] == 1 and data["status"] == 0


def test_batch_delete_endpoint(mysql_mode, monkeypatch) -> None:
    # 授权前置 ×1 + 每篇 4 条删除（引用/向量/切片/文档）+ oplog
    _install(
        monkeypatch,
        _perms_for([1]) + [_Result(rowcount=1)] * 4 + [_Result(rowcount=1)],
    )
    resp = client.post(
        "/api/docs/batch",
        json={"docIds": ["D001"], "action": "delete"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["deleted"] == 1


def test_batch_move_endpoint(mysql_mode, monkeypatch) -> None:
    _install(
        monkeypatch,
        _perms_for([1]) + [_Result([_Row(category_id=5)]), _Result(rowcount=1),
                           _Result(rowcount=1)],
    )
    resp = client.post(
        "/api/docs/batch",
        json={"docIds": [1], "action": "move", "categoryId": 5},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["updated"] == 1 and data["categoryId"] == 5


def test_batch_move_without_target_goes_uncategorized(mysql_mode, monkeypatch) -> None:
    """不传 categoryId → 移到「未分类」(0)，无需校验目标分类存在。"""
    _install(
        monkeypatch,
        _perms_for([1]) + [_Result(rowcount=1), _Result(rowcount=1)],
    )
    resp = client.post(
        "/api/docs/batch",
        json={"docIds": [1], "action": "move"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["categoryId"] == 0


def test_batch_move_to_missing_category_is_400(mysql_mode, monkeypatch) -> None:
    _install(
        monkeypatch,
        _perms_for([1]) + [_Result([])],
    )
    resp = client.post(
        "/api/docs/batch",
        json={"docIds": [1], "action": "move", "categoryId": 99},
    )
    assert resp.status_code == 400
    assert "目标分类不存在" in resp.json()["detail"]


def test_batch_publish_skips_permission_for_missing_doc(mysql_mode, monkeypatch) -> None:
    """文档不存在时跳过授权前置查询，只发批量更新 + oplog（不存在的 id 忽略）。"""
    _install(monkeypatch, [_Result([]), _Result(rowcount=1), _Result(rowcount=1)])
    resp = client.post(
        "/api/docs/batch",
        json={"docIds": [1], "action": "publish"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["updated"] == 1


def test_batch_bad_doc_id_is_400(mysql_mode, monkeypatch) -> None:
    """对外编号解析失败要在接口层给 400，而不是 500。"""
    _install(monkeypatch, [])
    resp = client.post(
        "/api/docs/batch",
        json={"docIds": ["X9"], "action": "publish"},
    )
    assert resp.status_code == 400
    assert "编号非法" in resp.json()["detail"]
