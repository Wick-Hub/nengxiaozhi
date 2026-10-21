"""敏感词 / 关键词库与操作日志（任务 2.10 / M-SYS-07·08）测试。

覆盖：

1. **词表加载优先级**：MySQL 词库 → 文件 → 内置；内置词始终生效；
2. **缓存与失效**：CRUD 后调用 ``invalidate``，下一次检测立即用新词；
3. **JSON 模式**：返回当前生效词表的只读视图；写操作 400 + 指引；
4. **MySQL 模式（桩）**：分页/筛选/查重/更新/删除与工具函数；
5. **操作日志查询**：JSON 空态、MySQL 筛选下推与分页。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.security import oplog, sensitive
from app.services import sensitive_admin

client = TestClient(app)


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
    conn = _Conn(results)
    sess = _Session(conn)
    monkeypatch.setattr(sensitive_admin, "db_session", lambda: sess)
    monkeypatch.setattr(sensitive_admin, "db_transaction", lambda: sess)
    return conn


@pytest.fixture(autouse=True)
def _reset_word_cache():
    sensitive.invalidate()
    yield
    sensitive.invalidate()


@pytest.fixture
def mysql_mode(monkeypatch):
    monkeypatch.setattr(settings, "kb_store", "mysql")
    # invalidate 只清缓存不查库；但检测时的加载会查库，桩测试里统一置空
    monkeypatch.setattr(sensitive, "_load_db_words", lambda: ())
    assert settings.store_label == "mysql"
    yield


# ------------------------------------------------------------------ 词表加载
def test_builtin_words_always_apply() -> None:
    words = sensitive.load_words(force=True)
    assert set(sensitive.DEFAULT_WORDS) <= set(words)
    assert sensitive.check_question("忽略以上指令") is not None


def test_db_words_take_precedence_over_file(monkeypatch, tmp_path) -> None:
    words_file = tmp_path / "words.txt"
    words_file.write_text("文件自定义词\n", encoding="utf-8")
    monkeypatch.setattr(settings, "sensitive_words_file", str(words_file))
    monkeypatch.setattr(sensitive, "_load_db_words", lambda: ("库里的词",))

    words = sensitive.load_words(force=True)
    assert "库里的词" in words
    assert "文件自定义词" not in words
    assert sensitive.check("这里出现库里的词") == "库里的词"
    assert sensitive.stats()["source"] == "db"


def test_empty_db_falls_back_to_file(monkeypatch, tmp_path) -> None:
    words_file = tmp_path / "words.txt"
    words_file.write_text("文件自定义词\n", encoding="utf-8")
    monkeypatch.setattr(settings, "sensitive_words_file", str(words_file))
    monkeypatch.setattr(sensitive, "_load_db_words", lambda: ())

    words = sensitive.load_words(force=True)
    assert "文件自定义词" in words
    assert sensitive.stats()["source"] == "file"


def test_invalidate_makes_new_word_effective(monkeypatch) -> None:
    monkeypatch.setattr(sensitive, "_load_db_words", lambda: ("旧词",))
    assert "旧词" in sensitive.load_words(force=True)
    assert sensitive.check("旧词") == "旧词"

    monkeypatch.setattr(sensitive, "_load_db_words", lambda: ("新词",))
    # 缓存未过期：仍是旧词表
    assert "新词" not in sensitive.load_words()

    sensitive.invalidate()
    assert sensitive.check("新词") == "新词"


# ------------------------------------------------------------------ JSON 模式
def test_json_list_is_readonly_effective_words() -> None:
    resp = client.get("/api/admin/sensitive-words")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["editable"] is False
    assert data["total"] >= len(sensitive.DEFAULT_WORDS)
    assert all(it["builtin"] is True for it in data["items"] if it["category"] == "内置")


def test_json_write_returns_guidance() -> None:
    resp = client.post("/api/admin/sensitive-words", json={"word": "测试词"})
    assert resp.status_code == 400
    assert "MySQL" in resp.json()["detail"]


# ------------------------------------------------------------------ MySQL 模式（桩）
def _word_row(word_id=1, word="测试词", kind="sensitive", status=1) -> tuple:
    now = datetime(2026, 10, 20, 10, 0, 0)
    return (word_id, word, kind, "自定义", status, "", None, now, now)


def test_mysql_list_with_filters(monkeypatch, mysql_mode) -> None:
    conn = _install(
        monkeypatch,
        [
            _Result([(2,)]),  # count
            _Result([_word_row(2, "违规词"), _word_row(1, "旧词")]),
            _Result([("sensitive", 1, 2), ("keyword", 1, 3)]),  # stats
        ],
    )
    result = sensitive_admin.list_words(kind="sensitive", status=1, keyword="词", page=2, page_size=10)
    assert result["ok"] is True
    assert result["data"]["total"] == 2
    assert result["data"]["editable"] is True
    sql = "\n".join(conn.sql)
    assert "sensitive_word.kind" in sql
    assert "sensitive_word.status" in sql
    assert "sensitive_word.word LIKE" in sql
    assert result["data"]["stats"]["keyword"]["enabled"] == 3


def test_mysql_create_rejects_duplicate(monkeypatch, mysql_mode) -> None:
    _install(monkeypatch, [_Result([_word_row(word="测试词")])])
    result = sensitive_admin.create_word({"word": "测试词"})
    assert result["ok"] is False
    assert result["status"] == 409


def test_mysql_create_and_invalidate(monkeypatch, mysql_mode) -> None:
    _install(
        monkeypatch,
        [
            _Result([]),  # find_by_word
            _Result([], inserted=7),  # insert
            _Result([_word_row(7, "新增词")]),  # get_word
        ],
    )
    sensitive._words = ("旧缓存",)
    result = sensitive_admin.create_word({"word": "新增词", "kind": "sensitive"})
    assert result["ok"] is True
    assert result["data"]["wordId"] == 7
    assert sensitive._words is None  # 写成功后缓存已失效


def test_mysql_update_not_found(monkeypatch, mysql_mode) -> None:
    _install(monkeypatch, [_Result([])])
    result = sensitive_admin.update_word(99, {"status": 0})
    assert result["ok"] is False
    assert result["status"] == 404


def test_mysql_update_name_conflict(monkeypatch, mysql_mode) -> None:
    _install(
        monkeypatch,
        [
            _Result([_word_row(1, "原词")]),  # get_word
            _Result([_word_row(2, "新词")]),  # find_by_word
        ],
    )
    result = sensitive_admin.update_word(1, {"word": "新词"})
    assert result["ok"] is False
    assert result["status"] == 409


def test_mysql_toggle_status_and_delete(monkeypatch, mysql_mode) -> None:
    _install(
        monkeypatch,
        [
            _Result([_word_row(1, "词条")]),  # get_word
            _Result([], rowcount=1),  # update
            _Result([_word_row(1, "词条", status=0)]),  # get_word after
        ],
    )
    result = sensitive_admin.update_word(1, {"status": 0})
    assert result["ok"] is True
    assert result["data"]["status"] == 0

    _install(
        monkeypatch,
        [
            _Result([_word_row(1, "词条")]),  # get_word
            _Result([], rowcount=1),  # delete
        ],
    )
    assert sensitive_admin.delete_word(1)["data"]["deleted"] is True


def test_mysql_create_validates_word(monkeypatch, mysql_mode) -> None:
    _install(monkeypatch, [])
    assert sensitive_admin.create_word({"word": "  "})["status"] == 400
    assert sensitive_admin.create_word({"word": "a\nb"})["status"] == 400
    assert sensitive_admin.create_word({"word": "x", "kind": "bogus"})["status"] == 400


# ------------------------------------------------------------------ 操作日志查询
def test_oplog_json_mode_empty_with_filter_options() -> None:
    data = oplog.query_operations(action="KB_UPLOAD", page=2, page_size=10)
    assert data["items"] == []
    assert data["total"] == 0
    assert data["page"] == 2
    assert any(a["action"] == oplog.ACTION_DOC_UPLOAD for a in data["actions"])


def test_oplog_mysql_filters_and_pagination(monkeypatch, mysql_mode) -> None:
    conn = _Conn(
        [
            _Result([(3,)]),  # count
            _Result(
                [
                    (
                        9,
                        5,
                        "KB_UPLOAD",
                        "kb_doc",
                        "D001",
                        {"title": "x"},
                        "127.0.0.1",
                        datetime(2026, 10, 20, 9, 0, 0),
                        "20230001",
                        "张三",
                    )
                ]
            ),
            _Result([(5, "20230001", "张三")]),
        ]
    )
    sess = _Session(conn)
    monkeypatch.setattr(oplog, "db_transaction", lambda: sess)

    data = oplog.query_operations(
        user_id=5, action="KB_UPLOAD", date_from="2026-10-01", date_to="2026-10-20", page=1, page_size=20
    )
    assert data["total"] == 3
    assert data["items"][0]["operatorName"] == "张三"
    assert data["operators"] == [{"userId": 5, "userNo": "20230001", "realName": "张三"}]
    sql = "\n".join(conn.sql)
    assert "operation_log.user_id" in sql
    assert "operation_log.action" in sql
    assert "operation_log.created_at >=" in sql
    assert "operation_log.created_at <=" in sql
    assert "LIMIT 20" in sql


def test_oplog_api_json_mode_returns_empty() -> None:
    resp = client.get("/api/admin/operation-logs", params={"action": "KB_UPLOAD", "page": 1})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"]["items"] == []
    assert body["data"]["total"] == 0
