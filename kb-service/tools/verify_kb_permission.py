"""知识库部门授权验收（任务 2.6 / M-K-03）。

自包含：**默认不依赖 MySQL**。四组自检：

1. **JSON 模式（只读推导）**：``list_permissions`` 空、``editable=False``，
   写操作返回 400 + 指引；
2. **MySQL 模式（桩）**：用场景驱动的假连接覆盖 ``kb_permission`` 全链路——
   授权矩阵 join、按名/按 id 解析、同键判重 409、更新/撤销；
3. **权限判定 ``can_operate``**：管理员恒过、AUTH_ENABLED=false 放行、
   部门联络员需授权位、其他角色 403、未知操作位 False、全部门/全类型通配；
4. **接口层契约 + 前端守卫**：真实起服务跑 ``/api/admin/kb-permissions`` 全链路
   + 状态码（200 / 400 / 401 / 403），并断言 admin.js 的 perm-* id 全在 admin.html。

用法::

    python tools/verify_kb_permission.py
    python tools/verify_kb_permission.py --port 8028

退出码：全部通过为 0，存在 FAIL 为 1。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import threading
import time
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


def info(text: str) -> None:
    print(f"         {text}")


# ==================================================================== 组 1：JSON 只读
def check_json_read_only() -> None:
    from app.config import settings
    from app.security.deps import Principal
    from app.services import kb_permission

    section("1. JSON 模式（只读推导）")
    original = settings.kb_store
    try:
        settings.kb_store = "json"
        res = kb_permission.list_permissions()
        check("可返回授权清单", res["ok"], str(res.get("error") or ""))
        data = res.get("data") or {}
        check("JSON 模式 editable=False", data.get("editable") is False)
        check("items 空数组", data.get("items") == [] and data.get("total") == 0)
        check("仍下发操作位模板", data.get("actions") == list(kb_permission.ACTIONS))

        p = Principal(user_no="admin", role="管理员", user_id=1, authenticated=True)
        r = kb_permission.grant({"userId": 10, "canView": 1}, p)
        check("grant 在 JSON 模式返回失败 + 指引",
              r["ok"] is False and "MySQL" in (r.get("error") or ""))
        r = kb_permission.update(1, {"canView": 1})
        check("update 在 JSON 模式返回失败 + 指引",
              r["ok"] is False and "MySQL" in (r.get("error") or ""))
        r = kb_permission.revoke(1)
        check("revoke 在 JSON 模式返回失败 + 指引",
              r["ok"] is False and "MySQL" in (r.get("error") or ""))
    finally:
        settings.kb_store = original


# ==================================================================== 组 2：MySQL 桩
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
                f"已收到的第 {len(self.statements)} 条语句：{_sql(stmt)[:220]}"
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
    except Exception:  # noqa: BLE001
        return str(stmt.compile())


def install(module, results) -> _Conn:
    from unittest.mock import patch
    from app.db import kb_repo

    conn = _Conn(results)
    sess = _Session(conn)
    patch.object(module, "db_session", lambda: sess).start()
    if hasattr(module, "db_transaction"):
        patch.object(module, "db_transaction", lambda: sess).start()
    # kb_repo 自己持有一份 db_session（list_permissions 等查询直接用它）
    patch.object(kb_repo, "db_session", lambda: sess).start()
    return conn


def stop_install() -> None:
    import unittest.mock as mock

    mock.patch.stopall()


def _perm_row(over: dict | None = None) -> tuple:
    from datetime import datetime

    base = {
        "permId": 1, "userId": 10, "deptId": 100, "ktype": "policy",
        "canUpload": 1, "canView": 1, "canDownload": 0, "canDelete": 1,
        "canCreateSub": 0, "userNo": "D001", "realName": "教务处联络员",
        "dept": "教务处", "createdBy": 1,
        "createdAt": datetime(2026, 10, 1, 9, 0), "updatedAt": datetime(2026, 10, 1, 9, 30),
    }
    base.update(over or {})
    return tuple(base[k] for k in (
        "permId", "userId", "deptId", "ktype", "canUpload", "canView",
        "canDownload", "canDelete", "canCreateSub", "userNo", "realName",
        "dept", "createdBy", "createdAt", "updatedAt"))


def _liaison_row(over: dict | None = None) -> tuple:
    base = {"userId": 10, "userNo": "D001", "realName": "教务处联络员", "orgId": 100}
    base.update(over or {})
    return tuple(base[k] for k in ("userId", "userNo", "realName", "orgId"))


def check_mysql_stub() -> None:
    from app.config import settings
    from app.db import kb_repo
    from app.security.deps import Principal
    from app.services import kb_permission

    section("2. MySQL 模式（桩）")
    original = settings.kb_store
    try:
        settings.kb_store = "mysql"
        assert settings.store_label == "mysql"

        # 列表（join + liaisons + orgs）
        conn = install(kb_permission, [
            _Result([_perm_row()]),
            _Result([_liaison_row()]),
            _Result([(100, "教务处", "JWC")]),
        ])
        try:
            res = kb_permission.list_permissions()
        finally:
            stop_install()
        check("列表读取成功", res["ok"], str(res.get("error")))
        check("MySQL 模式 editable=True", res["data"]["editable"] is True)
        item = res["data"]["items"][0]
        check("行含用户/部门/类型/操作位", item["userNo"] == "D001"
              and item["dept"] == "教务处" and item["ktype"] == "policy"
              and item["canDelete"] == 1 and item["canDownload"] == 0)
        joined = " ".join(conn.sql).lower()
        check("列表 join sys_user + sys_org",
              "sys_user" in joined and "sys_org" in joined and "kb_permission" in joined)

        # 授予（含判重/插入）
        conn = install(kb_permission, [
            _Result([(10, "D001", "教务联络员")]),  # get_user
            _Result([(100,)]),                      # dept_exists
            _Result([], rowcount=0),                # find_permission
            _Result(inserted=9),                    # INSERT
            _Result([_perm_row({"permId": 9})]),    # get_permission
        ])
        try:
            p = Principal(user_no="admin", role="管理员", user_id=1, authenticated=True)
            res = kb_permission.grant(
                {"userId": 10, "deptId": 100, "ktype": "policy", "canUpload": 1, "canDelete": 1}, p
            )
        finally:
            stop_install()
        check("授予成功", res["ok"], str(res.get("error")))
        sql_text = " ".join(conn.sql).replace("`", "")
        check("存在 INSERT INTO kb_permission", "INSERT INTO kb_permission" in sql_text)

        # 判重 409
        install(kb_permission, [
            _Result([(10, "D001", "教务联络员")]),
            _Result([(100,)]),
            _Result([(7,)]),  # find_permission 命中
        ])
        try:
            p = Principal(user_no="admin", role="管理员", user_id=1, authenticated=True)
            res = kb_permission.grant({"userId": 10, "deptId": 100, "ktype": "policy", "canView": 1}, p)
        finally:
            stop_install()
        check("同用户同部门同类型 → 409", res["ok"] is False and res["status"] == 409)

        # 更新
        conn = install(kb_permission, [
            _Result([_perm_row()]),
            _Result([], rowcount=1),
            _Result([_perm_row({"canDownload": 1, "canDelete": 0})]),
        ])
        try:
            res = kb_permission.update(1, {"canDownload": 1, "canDelete": 0})
        finally:
            stop_install()
        check("更新操作位成功", res["ok"] and res["data"]["canDownload"] == 1)
        check("存在 UPDATE kb_permission",
              "UPDATE kb_permission" in " ".join(conn.sql).replace("`", ""))

        # 撤销
        install(kb_permission, [
            _Result([_perm_row()]),
            _Result([], rowcount=1),
        ])
        try:
            res = kb_permission.revoke(1)
        finally:
            stop_install()
        check("撤销成功", res["ok"] and res["data"]["deleted"] is True)
    finally:
        stop_install()
        settings.kb_store = original


# ==================================================================== 组 3：can_operate
def _perm_for_row(over: dict, v: list) -> tuple:
    return tuple([over.get("deptId", 100), over.get("ktype", "policy")] + list(v))


def check_can_operate() -> None:
    from app.config import settings
    from app.services import kb_permission
    from app.security.deps import Principal

    section("3. 权限判定 can_operate")
    original_auth = settings.auth_enabled
    original_store = settings.kb_store
    try:
        settings.kb_store = "mysql"

        # AUTH_ENABLED=false → 一律放行
        settings.auth_enabled = False
        liaison = Principal(user_no="D001", role="部门联络员", user_id=10, authenticated=True)
        check("AUTH_ENABLED=false 时放行（开发/演示）",
              kb_permission.can_operate(liaison, 100, "policy", "canUpload"))

        settings.auth_enabled = True
        admin = Principal(user_no="admin", role="管理员", user_id=1, authenticated=True)
        check("管理员恒放行", kb_permission.can_operate(admin, 100, "policy", "canDelete"))

        # 联络员有授权
        row = _perm_for_row({"deptId": 100, "ktype": "policy"}, v=[1, 1, 0, 1, 0])
        # 每次 can_operate 开一个新 db_session → 消费一个新结果
        install(_module(), [_Result([row])])
        ok1 = kb_permission.can_operate(liaison, 100, "policy", "canUpload") is True
        stop_install()
        install(_module(), [_Result([row])])
        ok2 = kb_permission.can_operate(liaison, 100, "policy", "canDelete") is True
        stop_install()
        install(_module(), [_Result([row])])
        ok3 = kb_permission.can_operate(liaison, 100, "policy", "canDownload") is False
        stop_install()
        check("授权位命中：canUpload/canDelete=1、canDownload=0", ok1 and ok2 and ok3)

        # 全部门 / 全类型通配
        row = _perm_for_row({"deptId": 0, "ktype": "*"}, v=[0, 1, 0, 0, 0])
        install(_module(), [_Result([row])])
        ok_view = kb_permission.can_operate(liaison, 999, "item", "canView") is True
        stop_install()
        install(_module(), [_Result([row])])
        ok_up = kb_permission.can_operate(liaison, 999, "item", "canUpload") is False
        stop_install()
        check("全部门+全类型授权：任意部门任意类型 canView 生效", ok_view and ok_up)

        # 无授权 → False
        install(_module(), [_Result([])])
        check("无授权记录 → 拒绝",
              kb_permission.can_operate(liaison, 100, "policy", "canUpload") is False)
        stop_install()

        # 非联络员 → False（在 DB 查询前即短路，无需桩）
        student = Principal(user_no="S001", role="本科生", user_id=20, authenticated=True)
        check("非联络员角色 → 拒绝",
              kb_permission.can_operate(student, 100, "policy", "canView") is False)

        # 未知操作位 → False（在 DB 查询前即短路，无需桩）
        check("未知操作位 → False",
              kb_permission.can_operate(admin, 100, "policy", "canFly") is False)
    finally:
        stop_install()
        settings.auth_enabled = original_auth
        settings.kb_store = original_store


def _module():
    """返回 kb_permission 模块（延迟导入，避免顶部硬依赖）。"""
    from app.services import kb_permission

    return kb_permission


# ==================================================================== 组 4：接口层 + 前端守卫
def _serve(port: int) -> threading.Thread:
    import uvicorn

    def run() -> None:
        from app.main import app

        uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")

    t = threading.Thread(target=run, daemon=True)
    t.start()
    import socket

    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return t
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"服务未在 {port} 端口就绪")


def check_api_and_frontend(port: int) -> None:
    import httpx

    from app.config import settings
    from app.services import kb_permission

    section("4. 接口层契约 + 前端守卫")

    # 前端守卫：admin.js 的 perm-* id 必须存在于 admin.html
    js = (SERVICE_DIR / "static" / "admin.js").read_text(encoding="utf-8")
    html = (SERVICE_DIR / "static" / "admin.html").read_text(encoding="utf-8")
    ids = set(re.findall(r"getElementById\(\s*['\"]([A-Za-z0-9_-]+)['\"]\s*\)", js))
    perm_ids = sorted(i for i in ids if i.startswith("perm-"))
    missing = sorted(i for i in perm_ids if f'id="{i}"' not in html)
    check("admin.js 的 perm-* id 全部存在于 admin.html",
          not missing, f"缺失：{missing}")
    check("admin.js 含授权渲染逻辑",
          "renderPerm" in js and "loadPerms" in js and "grantPerm" in js and "revokePerm" in js)

    base = f"http://127.0.0.1:{port}"
    settings.kb_store = "json"
    try:
        t = _serve(port)
        with httpx.Client(timeout=20.0, trust_env=False) as cli:
            r = cli.get(f"{base}/api/admin/kb-permissions")
            check("GET /api/admin/kb-permissions → 200", r.status_code == 200, f"HTTP {r.status_code}")
            body = r.json()
            check("响应封装 ok=true", body.get("ok") is True)
            check("JSON 模式 editable=false", (body.get("data") or {}).get("editable") is False)

            # 写操作 JSON 模式 → 400 + 指引（HTTPException detail）
            r = cli.post(f"{base}/api/admin/kb-permissions",
                         json={"userId": 10, "canView": 1})
            check("POST 在 JSON 模式 → 400", r.status_code == 400, f"HTTP {r.status_code}")
            check("POST 400 响应带 MySQL 指引",
                  "MySQL" in (r.json().get("detail") or ""))
    finally:
        settings.kb_store = "json"


# ==================================================================== main
def main() -> int:
    parser = argparse.ArgumentParser(description="知识库部门授权验收（任务 2.6）")
    parser.add_argument("--port", type=int, default=8028, help="接口层验收用的端口")
    args = parser.parse_args()

    from app.config import settings

    print("=" * 68)
    print("知识库部门授权验收（任务 2.6 / M-K-03）")
    print(f"存储模式：{settings.store_label}    数据目录：{settings.base_dir}")
    print("=" * 68)

    try:
        check_json_read_only()
        check_mysql_stub()
        check_can_operate()
        check_api_and_frontend(args.port)
    finally:
        stop_install()

    print("\n" + "=" * 68)
    print(f"结果：{PASSED} PASS / {FAILED} FAIL")
    print("=" * 68)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())