"""知识库部门授权（任务 2.6 / M-K-03）测试。

覆盖两条路径：

* **JSON 模式（只读推导）**：``list_permissions`` 返回空、``editable=False``；
  写操作返回 400 + 指引。
* **MySQL 模式（桩）**：用场景驱动的假连接覆盖 ``kb_permission`` 全链路——
  授权矩阵分页、按名/按 id 解析部门、同键判重 409、更新/撤销、以及核心的
  ``can_operate`` 判定（管理员恒过、联络员需授权位、其他角色 403）。

桩测试同样不验证真实 MySQL 语法——那是 ``test_mysql_store.py`` 的职责。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.config import settings
from app.db import kb_repo
from app.security import rbac
from app.security.deps import Principal
from app.services import kb_permission

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
    """把 kb_permission / kb_repo 的 db_session、db_transaction 换成假连接。

    注意：kb_permission 与 kb_repo 各自持有一份 session 引用，
    两处都要打点，否则查询会落到真实数据库。
    """
    conn = _Conn(results)
    sess = _Session(conn)
    monkeypatch.setattr(kb_permission, "db_session", lambda: sess)
    monkeypatch.setattr(kb_permission, "db_transaction", lambda: sess)
    monkeypatch.setattr(kb_repo, "db_session", lambda: sess)
    return conn


@pytest.fixture
def mysql_mode(monkeypatch):
    monkeypatch.setattr(settings, "kb_store", "mysql")
    assert settings.store_label == "mysql"
    yield


# ------------------------------------------------------------------ 行构造

def _perm_row(over: dict | None = None) -> tuple:
    """list_permissions 的 SELECT 顺序：
    perm_id, user_id, dept_id, ktype, can_upload, can_view, can_download,
    can_delete, can_create_sub, user_no, real_name, dept, created_by, created_at, updated_at。
    """
    base = {
        "permId": 1,
        "userId": 10,
        "deptId": 100,
        "ktype": "policy",
        "canUpload": 1,
        "canView": 1,
        "canDownload": 0,
        "canDelete": 1,
        "canCreateSub": 0,
        "userNo": "D001",
        "realName": "教务处联络员",
        "dept": "教务处",
        "createdBy": 1,
        "createdAt": datetime(2026, 10, 1, 9, 0),
        "updatedAt": datetime(2026, 10, 1, 9, 30),
    }
    base.update(over or {})
    return tuple(base[k] for k in (
        "permId", "userId", "deptId", "ktype", "canUpload", "canView",
        "canDownload", "canDelete", "canCreateSub", "userNo", "realName",
        "dept", "createdBy", "createdAt", "updatedAt",
    ))


def _liaison_row(over: dict | None = None) -> tuple:
    base = {"userId": 10, "userNo": "D001", "realName": "教务处联络员", "orgId": 100}
    base.update(over or {})
    return tuple(base[k] for k in ("userId", "userNo", "realName", "orgId"))


def _perm_for_row(over: dict, *, v: list) -> tuple:
    """user_permission_for 的 SELECT 顺序：
    dept_id, ktype, can_upload, can_view, can_download, can_delete, can_create_sub。
    """
    return tuple([over.get("deptId", 100), over.get("ktype", "policy")] + list(v))


# ================================================================== JSON 模式（只读推导）
def test_json_list_permissions_read_only(monkeypatch) -> None:
    monkeypatch.setattr(settings, "kb_store", "json")
    res = kb_permission.list_permissions()
    assert res["ok"], res
    data = res["data"]
    assert data["editable"] is False
    assert data["items"] == []
    assert data["total"] == 0
    assert data["ktypes"]  # 类型模板仍下发
    assert set(data["actions"]) == set(kb_permission.ACTIONS)


def test_json_write_ops_return_error_with_hint(monkeypatch) -> None:
    monkeypatch.setattr(settings, "kb_store", "json")
    p = Principal(user_no="D001", role="管理员", user_id=1, authenticated=True)
    assert kb_permission.grant({"userId": 10, "canView": 1}, p)["ok"] is False
    assert kb_permission.update(1, {"canView": 1})["ok"] is False
    assert kb_permission.revoke(1)["ok"] is False


# ================================================================== MySQL 模式（桩：授权矩阵管理）
def test_mysql_list_permissions_joins_user_and_dept(mysql_mode, monkeypatch) -> None:
    conn = _install(monkeypatch, [
        _Result([_perm_row()]),       # list_permissions
        _Result([_liaison_row()]),    # list_dept_liaisons
        _Result([(100, "教务处", "JWC")]),  # list_orgs
    ])
    res = kb_permission.list_permissions()
    assert res["ok"], res
    data = res["data"]
    assert data["editable"] is True
    item = data["items"][0]
    assert item["permId"] == 1
    assert item["userNo"] == "D001"
    assert item["dept"] == "教务处"
    assert item["ktype"] == "policy"
    assert item["canDelete"] == 1 and item["canDownload"] == 0
    joined = " ".join(conn.sql).lower()
    assert "sys_user" in joined and "sys_org" in joined and "kb_permission" in joined


def test_mysql_list_permissions_filters_by_user(mysql_mode, monkeypatch) -> None:
    _install(monkeypatch, [
        _Result([_perm_row()]),       # list_permissions(user_id 过滤)
        _Result([_liaison_row()]),    # list_dept_liaisons
        _Result([(100, "教务处", "JWC")]),  # list_orgs
    ])
    res = kb_permission.list_permissions(user_id=10)
    assert res["ok"]
    assert res["data"]["items"][0]["userId"] == 10


def test_mysql_grant_resolves_and_dedups(mysql_mode, monkeypatch) -> None:
    # 顺序：get_user(10) → dept_exists(100) → find_permission(None) → insert → get_permission
    conn = _install(monkeypatch, [
        _Result([(10, "D001", "教务联络员")]),   # get_user
        _Result([(100,)]),                        # dept_exists
        _Result([], rowcount=0),                  # find_permission 未命中
        _Result(inserted=9),                      # INSERT
        _Result([_perm_row({"permId": 9, "userNo": "D001", "realName": "教务联络员"})]),  # get_permission
    ])
    p = Principal(user_no="admin", role="管理员", user_id=1, authenticated=True)
    res = kb_permission.grant(
        {"userId": 10, "deptId": 100, "ktype": "policy", "canUpload": 1, "canDelete": 1}, p
    )
    assert res["ok"], res
    assert res["data"]["permId"] == 9
    sql_text = " ".join(conn.sql).replace("`", "")
    assert "INSERT INTO kb_permission" in sql_text


def test_mysql_grant_duplicate_returns_409(mysql_mode, monkeypatch) -> None:
    _install(monkeypatch, [
        _Result([(10, "D001", "教务联络员")]),   # get_user
        _Result([(100,)]),                        # dept_exists
        _Result([(7,)]),                          # find_permission 命中 perm_id=7
    ])
    p = Principal(user_no="admin", role="管理员", user_id=1, authenticated=True)
    res = kb_permission.grant({"userId": 10, "deptId": 100, "ktype": "policy", "canView": 1}, p)
    assert res["ok"] is False
    assert res["status"] == 409


def test_mysql_grant_requires_at_least_one_action(mysql_mode, monkeypatch) -> None:
    _install(monkeypatch, [])  # 不应产生任意 SQL 即被拦下
    p = Principal(user_no="admin", role="管理员", user_id=1, authenticated=True)
    res = kb_permission.grant({"userId": 10, "deptId": 100, "ktype": "policy"}, p)
    assert res["ok"] is False
    assert res["status"] == 400
    assert "至少勾选" in res["error"]


def test_mysql_update_permission_renames_flags(mysql_mode, monkeypatch) -> None:
    # get_permission → update → get_permission
    conn = _install(monkeypatch, [
        _Result([_perm_row()]),
        _Result([], rowcount=1),
        _Result([_perm_row({"canDownload": 1, "canDelete": 0})]),
    ])
    res = kb_permission.update(1, {"canDownload": 1, "canDelete": 0})
    assert res["ok"], res
    assert res["data"]["canDownload"] == 1
    sql_text = " ".join(conn.sql).replace("`", "")
    assert "UPDATE kb_permission" in sql_text


def test_mysql_update_missing_returns_404(mysql_mode, monkeypatch) -> None:
    _install(monkeypatch, [_Result([])])
    res = kb_permission.update(99, {"canView": 1})
    assert res["ok"] is False
    assert res["status"] == 404


def test_mysql_revoke_ok(mysql_mode, monkeypatch) -> None:
    _install(monkeypatch, [
        _Result([_perm_row()]),   # get_permission
        _Result([], rowcount=1),  # DELETE
    ])
    res = kb_permission.revoke(1)
    assert res["ok"], res
    assert res["data"]["deleted"] is True


# ================================================================== can_operate 判定
def _perm(row: tuple) -> dict | None:
    """把 user_permission_for 的行转换成 dict，模拟 kb_repo 返回。"""
    return {
        "deptId": int(row[0]),
        "ktype": row[1],
        "canUpload": int(row[2] or 0),
        "canView": int(row[3] or 0),
        "canDownload": int(row[4] or 0),
        "canDelete": int(row[5] or 0),
        "canCreateSub": int(row[6] or 0),
    }


def test_can_operate_admin_and_unauth_always_pass(mysql_mode, monkeypatch) -> None:
    # 管理员恒过；AUTH_ENABLED=false 时一律放行
    monkeypatch.setattr(settings, "auth_enabled", False)
    admin = Principal(user_no="admin", role="管理员", user_id=1, authenticated=True)
    liaison = Principal(user_no="D001", role="部门联络员", user_id=10, authenticated=True)
    assert kb_permission.can_operate(admin, 100, "policy", "canDelete")
    assert kb_permission.can_operate(liaison, 100, "policy", "canUpload")  # auth 关闭 → 放行


def test_can_operate_liaison_with_grant(mysql_mode, monkeypatch) -> None:
    monkeypatch.setattr(settings, "auth_enabled", True)
    row = _perm_for_row({"deptId": 100, "ktype": "policy"}, v=[1, 1, 0, 1, 0])
    # 每次 can_operate 开一个新 db_session → 消费一个新结果
    _install(monkeypatch, [
        _Result([row]),  # canUpload
        _Result([row]),  # canDelete
        _Result([row]),  # canDownload
    ])
    p = Principal(user_no="D001", role="部门联络员", user_id=10, authenticated=True)
    assert kb_permission.can_operate(p, 100, "policy", "canUpload") is True
    assert kb_permission.can_operate(p, 100, "policy", "canDelete") is True
    assert kb_permission.can_operate(p, 100, "policy", "canDownload") is False


def test_can_operate_liaison_without_grant(mysql_mode, monkeypatch) -> None:
    monkeypatch.setattr(settings, "auth_enabled", True)
    _install(monkeypatch, [_Result([])])  # 无授权记录
    p = Principal(user_no="D001", role="部门联络员", user_id=10, authenticated=True)
    assert kb_permission.can_operate(p, 100, "policy", "canUpload") is False


def test_can_operate_non_liaison_forbidden(mysql_mode, monkeypatch) -> None:
    monkeypatch.setattr(settings, "auth_enabled", True)
    _install(monkeypatch, [])
    student = Principal(user_no="S001", role="本科生", user_id=20, authenticated=True)
    assert kb_permission.can_operate(student, 100, "policy", "canView") is False


def test_can_operate_unknown_action_false(mysql_mode, monkeypatch) -> None:
    monkeypatch.setattr(settings, "auth_enabled", True)
    _install(monkeypatch, [])
    admin = Principal(user_no="admin", role="管理员", user_id=1, authenticated=True)
    assert kb_permission.can_operate(admin, 100, "policy", "canFly") is False


def test_can_operate_widget_dept_or_ktype_match(mysql_mode, monkeypatch) -> None:
    """授权是全部部门(*)时，任意部门命中；ktype='*' 时任意类型命中。"""
    monkeypatch.setattr(settings, "auth_enabled", True)
    row = _perm_for_row({"deptId": 0, "ktype": "*"}, v=[0, 1, 0, 0, 0])
    _install(monkeypatch, [
        _Result([row]),  # canView
        _Result([row]),  # canUpload
    ])
    p = Principal(user_no="D001", role="部门联络员", user_id=10, authenticated=True)
    # 任意部门任意类型都能看（canView=1）
    assert kb_permission.can_operate(p, 999, "item", "canView") is True
    assert kb_permission.can_operate(p, 999, "item", "canUpload") is False