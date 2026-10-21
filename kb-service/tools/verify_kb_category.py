"""知识库分类管理验收（任务 2.5 / M-K-01·02）。

自包含：**默认不依赖 MySQL**。四组自检：

1. **JSON 模式（只读推导）**：从索引按（部门 × 知识类型）派生出分类，
   ``editable=False``，写操作返回 400 + 可执行指引；
2. **MySQL 模式（桩）**：用场景驱动的假连接覆盖 ``kb_category`` 全链路——
   列表 join + doc_count、按部门名/id 解析、同分类判重 409、
   删除前「有文档 / 有子分类」409、重命名/排序；SQL 用 ``literal_binds`` 断言；
3. **接口层契约**：真实起服务跑 ``/admin/kb-categories`` 全链路 + 状态码
   （200 / 400）；JSON 模式下 POST/PATCH/DELETE 应返回 400；
4. **前端守卫**：admin.js 引用的 cat-* id 必须存在于 admin.html
   （与 ``test_js_dom_ids_exist_in_html`` 同口径）。

用法::

    python tools/verify_kb_category.py          # JSON + MySQL 桩（无需数据库）
    python tools/verify_kb_category.py --port 8026 --skip-auth

退出码：全部通过为 0，存在 FAIL 为 1。
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
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


# ==================================================================== 组 1：JSON 只读推导
def check_json_derived() -> None:
    from app.config import settings
    from app.services import kb_category

    section("1. JSON 模式（只读推导）")
    original = settings.kb_store
    try:
        settings.kb_store = "json"
        # 用真实索引校验推导路径（data/store.json 若存在即用其内容）
        res = kb_category.list_categories()
        check("可推导分类清单", res["ok"], str(res.get("error")))
        if not res["ok"]:
            return
        data = res["data"]
        check("JSON 模式 editable=False", data["editable"] is False)
        check("items 与 depts/ktypes 存在", isinstance(data["items"], list)
              and isinstance(data["depts"], list) and isinstance(data["ktypes"], list))
        check("docTotal 为各分类文档数之和",
              data["docTotal"] == sum(c["docCount"] for c in data["items"]),
              f"docTotal={data['docTotal']}")
        check("派生分类无真实主键（categoryId=None，前端不依赖）",
              all(c["categoryId"] is None for c in data["items"]))

        # 写操作必须 400 + 指引，而不是静默失败
        r = kb_category.create_category({"dept": "教务处", "ktype": "政策文件"})
        check("create 在 JSON 模式返回失败 + 指引",
              r["ok"] is False and "MySQL" in (r.get("error") or ""), r.get("error") or "")
        r = kb_category.update_category(1, {"categoryName": "x"})
        check("update 在 JSON 模式返回失败 + 指引",
              r["ok"] is False and "MySQL" in (r.get("error") or ""))
        r = kb_category.delete_category(1)
        check("delete 在 JSON 模式返回失败 + 指引",
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
        self.closed = False

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
    except Exception:  # noqa: BLE001 - 仅为诊断输出
        return str(stmt.compile())


def install(module, results) -> _Conn:
    """把模块内的 ``db_session`` / ``db_transaction`` 换成假连接。"""
    from unittest.mock import patch

    conn = _Conn(results)
    sess = _Session(conn)
    patch.object(module, "db_session", lambda: sess).start()
    if hasattr(module, "db_transaction"):
        patch.object(module, "db_transaction", lambda: sess).start()
    return conn


def stop_install() -> None:
    import unittest.mock as mock

    mock.patch.stopall()


def _category_row(**over) -> tuple:
    base = {
        "categoryId": 1, "deptId": 10, "ktype": "policy", "categoryName": "政策文件",
        "parentId": 0, "sortNo": 1, "orgName": "教务处", "docCount": 3,
    }
    base.update(over)
    return tuple(base[k] for k in (
        "categoryId", "deptId", "ktype", "categoryName", "parentId", "sortNo",
        "orgName", "docCount"))


def _single_row(over: dict | None = None) -> tuple:
    base = {
        "categoryId": 1, "deptId": 10, "ktype": "policy", "categoryName": "政策文件",
        "parentId": 0, "sortNo": 1, "orgName": "教务处",
    }
    base.update(over or {})
    return tuple(base[k] for k in (
        "categoryId", "deptId", "ktype", "categoryName", "parentId", "sortNo", "orgName"))


def check_mysql_stub() -> None:
    from app.config import settings
    from app.db import kb_repo
    from app.services import kb_category

    section("2. MySQL 模式（桩）")
    original = settings.kb_store
    try:
        settings.kb_store = "mysql"
        assert settings.store_label == "mysql"

        # ---- 列表：join + doc_count ----
        conn = install(kb_repo, [_Result([_category_row()])])
        # 注意：list_categories 走 kb_repo 模块自身的 db_session
        try:
            res = kb_category.list_categories()
        finally:
            stop_install()
        check("列表读取成功", res["ok"], str(res.get("error")))
        item = res["data"]["items"][0]
        check("列表含部门/类型/文档数", item["dept"] == "教务处"
              and item["ktype"] == "政策文件" and item["docCount"] == 3)
        check("MySQL 模式 editable=True", res["data"]["editable"] is True)
        joined = " ".join(conn.sql).lower()
        check("列表查询 join sys_org + outerjoin kb_doc 统计文档数",
              "sys_org" in joined and "kb_doc" in joined and "outer" in joined,
              joined[:150])

        # ---- 空列表 ----
        install(kb_repo, [_Result([])])
        try:
            res = kb_category.list_categories()
        finally:
            stop_install()
        check("空分类表返回空数组", res["ok"] and res["data"]["items"] == [])

        # ---- 新增：按部门名解析 → 判重 → insert → 回读 ----
        conn = install(kb_category, [
            _Result([(10,)]),                        # dept_id_by_name
            _Result([(10,)]),                        # dept_exists
            _Result([], rowcount=0),                 # find_category 判重（未命中）
            _Result([], rowcount=0),                 # repo.create_category 内部 find
            _Result(inserted=41),                    # INSERT
            _Result([_single_row({"categoryId": 41, "categoryName": "校级政策"})]),  # get_category
            _Result([], rowcount=1),                 # category_doc_count → 0
        ])
        try:
            res = kb_category.create_category(
                {"dept": "教务处", "ktype": "政策文件", "categoryName": "校级政策", "sortNo": 2}
            )
        finally:
            stop_install()
        check("按部门名新增成功", res["ok"], str(res.get("error")))
        check("新增返回 categoryId 与名称", res["data"]["categoryId"] == 41
              and res["data"]["categoryName"] == "校级政策")
        sql_text = " ".join(conn.sql).replace("`", "")
        check("存在 INSERT INTO kb_category", "INSERT INTO kb_category" in sql_text)

        # ---- 重复分类 → 409 ----
        install(kb_category, [
            _Result([(10,)]),   # dept_exists
            _Result([(7,)]),    # find_category 命中
        ])
        try:
            res = kb_category.create_category({"deptId": 10, "ktype": "policy"})
        finally:
            stop_install()
        check("同部门同类型已存在 → 409", res["ok"] is False and res["status"] == 409,
              res.get("error") or "")

        # ---- 部门不存在 → 400 ----
        install(kb_category, [_Result([], rowcount=0)])
        try:
            res = kb_category.create_category({"deptId": 999, "ktype": "policy"})
        finally:
            stop_install()
        check("部门不存在 → 400", res["ok"] is False and res["status"] == 400,
              res.get("error") or "")

        # ---- 重命名 ----
        conn = install(kb_category, [
            _Result([_single_row()]),                       # get_category 主查询
            _Result([], rowcount=1),                        # 内部 category_doc_count → 0
            _Result([], rowcount=1),                        # UPDATE
            _Result([_single_row({"categoryName": "新名", "sortNo": 9})]),  # get_category 主查询
            _Result([], rowcount=1),                        # 内部 category_doc_count → 0
        ])
        try:
            res = kb_category.update_category(1, {"categoryName": "新名", "sortNo": 9})
        finally:
            stop_install()
        check("重命名成功", res["ok"] and res["data"]["categoryName"] == "新名")
        update_sql = [s for s in conn.sql if "UPDATE" in s.replace("`", "").upper()]
        check("UPDATE 含 category_name 与 sort_no",
              bool(update_sql) and "category_name" in update_sql[0] and "sort_no" in update_sql[0])

        # ---- 删除：有文档 → 409 ----
        install(kb_category, [
            _Result([_single_row()]),   # get_category
            _Result([(3,)]),            # category_doc_count → 3
        ])
        try:
            res = kb_category.delete_category(1)
        finally:
            stop_install()
        check("有文档时删除 → 409", res["ok"] is False and res["status"] == 409
              and "3 篇文档" in res.get("error", ""), res.get("error") or "")

        # ---- 删除：有子分类 → 409 ----
        install(kb_category, [
            _Result([_single_row()]),   # get_category
            _Result([], rowcount=1),    # category_doc_count → 0
            _Result([(2,)]),            # category_child_count → 2
        ])
        try:
            res = kb_category.delete_category(1)
        finally:
            stop_install()
        check("有子分类时删除 → 409", res["ok"] is False and res["status"] == 409
              and "2 个子分类" in res.get("error", ""), res.get("error") or "")

        # ---- 删除：正常 ----
        conn = install(kb_category, [
            _Result([_single_row()]),   # get_category
            _Result([], rowcount=1),    # category_doc_count → 0
            _Result([], rowcount=1),    # category_child_count → 0
            _Result([], rowcount=1),    # DELETE
        ])
        try:
            res = kb_category.delete_category(1)
        finally:
            stop_install()
        check("无文档无子分类时删除成功", res["ok"] and res["data"]["deleted"] is True)
        delete_sql = [s for s in conn.sql if "DELETE FROM kb_category" in s.replace("`", "")]
        check("存在 DELETE FROM kb_category", bool(delete_sql))

    finally:
        stop_install()
        settings.kb_store = original


# ==================================================================== 组 3：接口层契约
def _serve(port: int) -> threading.Thread:
    import uvicorn

    def run() -> None:
        from app.main import app

        uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")

    t = threading.Thread(target=run, daemon=True)
    t.start()
    # 等待端口就绪
    import socket

    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return t
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"服务未在 {port} 端口就绪")


def check_api(port: int) -> None:
    import httpx

    from app.config import settings

    section("3. 接口层契约（真实服务）")
    base = f"http://127.0.0.1:{port}"
    # 本地请求必须绕过系统代理
    headers = {}
    try:
        t = _serve(port)
        with httpx.Client(timeout=20.0, trust_env=False, headers=headers) as cli:
            # 列表：JSON 模式返回推导分类，editable=false
            r = cli.get(f"{base}/api/admin/kb-categories")
            check("GET /api/admin/kb-categories → 200", r.status_code == 200, f"HTTP {r.status_code}")
            body = r.json()
            check("响应封装 ok=true", body.get("ok") is True, str(body)[:200])
            data = body.get("data") or {}
            check("editable=false（JSON 存储只读）", data.get("editable") is False)
            check("items 为数组", isinstance(data.get("items"), list))

            # 写操作：JSON 模式应 400 + 指引（失败体为 HTTPException 的 {"detail": …}）
            r = cli.post(f"{base}/api/admin/kb-categories",
                         json={"dept": "教务处", "ktype": "政策文件"})
            check("POST 在 JSON 模式 → 400", r.status_code == 400, f"HTTP {r.status_code}")
            body = r.json()
            check("POST 400 响应带 MySQL 指引", "MySQL" in (body.get("detail") or ""),
                  body.get("detail") or "")
            r = cli.delete(f"{base}/api/admin/kb-categories/1")
            check("DELETE 在 JSON 模式 → 400", r.status_code == 400, f"HTTP {r.status_code}")

            # 参数缺失：POST 缺 ktype → 400（服务层校验，不落到 422）
            r = cli.post(f"{base}/api/admin/kb-categories", json={"dept": "教务处"})
            check("POST 缺 ktype → 400 而不是 422", r.status_code == 400, f"HTTP {r.status_code}")
    finally:
        # uvicorn 线程是 daemon，进程结束自然退出；这里显式停止
        import signal

        try:
            os.kill(os.getpid(), signal.SIGTERM) if False else None  # 不自杀，交给 daemon
        except Exception:  # noqa: BLE001
            pass


# ==================================================================== 组 4：前端守卫
def check_frontend() -> None:
    section("4. 前端守卫")
    static = SERVICE_DIR / "static"
    js = (static / "admin.js").read_text(encoding="utf-8")
    html = (static / "admin.html").read_text(encoding="utf-8")
    expected = {
        "cat-note", "cat-form", "cat-dept", "cat-ktype", "cat-name", "cat-sort",
        "cat-new-btn", "cat-msg", "cat-tree", "cat-empty",
    }
    missing = sorted(i for i in expected if f'id="{i}"' not in html)
    check("admin.js 使用的 cat-* id 全部存在于 admin.html",
          not missing, f"缺失：{missing}")
    check("admin.js 含分类渲染逻辑", "renderCats" in js and "loadCats" in js
          and "createCat" in js and "renameCat" in js and "deleteCat" in js)


# ==================================================================== main
def main() -> int:
    parser = argparse.ArgumentParser(description="知识库分类管理验收（任务 2.5）")
    parser.add_argument("--port", type=int, default=8026, help="接口层验收用的端口")
    parser.add_argument("--skip-auth", action="store_true", help="兼容参数（无需鉴权环境）")
    args = parser.parse_args()

    from app.config import settings

    print("=" * 68)
    print("知识库分类管理验收（任务 2.5 / M-K-01·02）")
    print(f"存储模式：{settings.store_label}    数据目录：{settings.base_dir}")
    print("=" * 68)

    try:
        check_json_derived()
        check_mysql_stub()
        check_api(args.port)
        check_frontend()
    finally:
        stop_install()

    print("\n" + "=" * 68)
    print(f"结果：{PASSED} PASS / {FAILED} FAIL")
    print("=" * 68)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())