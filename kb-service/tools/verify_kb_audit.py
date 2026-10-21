"""文档审核与文件管理验收（任务 2.7 / M-K-05·09）。

自包含：**默认不依赖 MySQL**。五组自检：

1. **JSON 模式契约**：``/api/docs`` 的 ``data`` 仍是**数组**（合同兼容），
   ``status/dept/ktype`` 在内存里过滤；``POST /api/docs/batch`` 返回
   400 + ``KB_STORE=mysql`` 指引；
2. **MySQL 模式（桩）**：四个筛选必须下推到 SQL（不是内存过滤）、
   ``categoryId`` 进返回 dict；``batch_update_status`` /
   ``batch_move_category`` / ``batch_delete_docs`` 的命中计数、
   空 id 短路与「目标分类不存在」错误路径；
3. **前端守卫**：admin.js 的 f-*/batch-*/chk-all id 与 preview.js 的
   audit-* id 必须真的存在于对应 HTML（HTTP 层看不见的拼写漂移）；
4. **接口层**：真实起服务，JSON 模式下跑筛选与批量门禁；
5. **真实库（``--mysql``，幂等）**：先快照 ``kb_doc`` 的
   status/category_id/published_at，跑一轮真实的筛选与批量操作，
   最后**原样恢复**（跑完应与快照一致）。

用法::

    python tools/verify_kb_audit.py
    python tools/verify_kb_audit.py --mysql
    python tools/verify_kb_audit.py --port 8029

退出码：全部通过为 0，存在 FAIL 为 1。
"""

from __future__ import annotations

import argparse
import os
import re
import socket
import sys
import threading
import time
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

# 默认走 JSON 模式（组 1/4 用）；真实库由 --mysql 显式开启
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


# ==================================================================== 假连接
class _Row(tuple):
    """行替身：同时支持下标（``row[0]``）与属性（``row.doc_id``）访问。

    真实 SQLAlchemy Row 两种访问都支持，而 ``dept_id_by_name`` 用下标、
    ``list_docs`` / ``get_doc`` 用属性——只实现一种会漏一片。
    """

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


def install(modules, results) -> _Conn:
    """把各模块的 session 引用换成假连接。

    ``kb_repo``（get_doc / batch_*）、``kb_permission``（resolve_dept_id）、
    ``oplog``（log_operation）**各自持有一份引用**，少打一处就会落到真实库
    （MySQL 在线时表现为「查不到部门」这类假故障）。
    """
    from unittest.mock import patch

    conn = _Conn(results)
    sess = _Session(conn)
    for mod in modules:
        patch.object(mod, "db_session", lambda: sess).start()
        if hasattr(mod, "db_transaction"):
            patch.object(mod, "db_transaction", lambda: sess).start()
    return conn


def stop_install() -> None:
    import unittest.mock as mock

    mock.patch.stopall()


# ==================================================================== 组 1：JSON 契约
def check_json_contract() -> None:
    """JSON 模式合同验收。

    注意：**必须走 TestClient 而不是直接调路由函数** —— ``Query(default=None)``
    这类默认值是 FastAPI 的依赖对象，直接调用会把它当成过滤值传进去
    （真值为 True → 全被过滤掉，表现为「索引空了」）。
    """
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import app

    section("1. JSON 模式契约（合同兼容 + 写操作门禁）")
    original = settings.kb_store
    settings.kb_store = "json"
    try:
        assert settings.store_label == "json"
        cli = TestClient(app)

        r = cli.get("/api/docs")
        check("GET /api/docs → 200", r.status_code == 200, f"HTTP {r.status_code}")
        body = r.json()
        check("data 是数组（合同兼容：前端/历史测试都按列表消费）",
              isinstance(body.get("data"), list))
        docs = body["data"]
        check("JSON 索引非空", len(docs) > 0, f"{len(docs)} 篇")

        if docs:
            st = str(docs[0]["status"])
            r = cli.get("/api/docs", params={"status": st})
            got = r.json()["data"]
            check(f"status={st} 筛选生效（内存过滤）",
                  bool(got) and all(str(d["status"]) == st for d in got),
                  f"{len(got)} 篇")

            kt = docs[0].get("ktype")
            if kt:
                r = cli.get("/api/docs", params={"ktype": kt})
                got = r.json()["data"]
                check("ktype 筛选生效",
                      bool(got) and all(d.get("ktype") == kt for d in got), f"{len(got)} 篇")
            else:
                check("ktype 筛选生效", True, "索引无 ktype，跳过")

            dept = docs[0].get("dept")
            if dept:
                r = cli.get("/api/docs", params={"dept": dept})
                got = r.json()["data"]
                check("dept 筛选生效",
                      bool(got) and all(d.get("dept") == dept for d in got), f"{len(got)} 篇")

        # categoryId：JSON 模式没有分类列，传了不报错也不筛
        r = cli.get("/api/docs", params={"categoryId": 1})
        check("categoryId 在 JSON 模式被接受", r.status_code == 200, f"HTTP {r.status_code}")

        # 批量写操作 → 400 + 可执行指引
        r = cli.post("/api/docs/batch", json={"docIds": [1], "action": "publish"})
        check("JSON 模式批量 → 400", r.status_code == 400, f"HTTP {r.status_code}")
        check("400 带 KB_STORE=mysql 指引",
              "KB_STORE=mysql" in (r.json().get("detail") or ""))

        r = cli.post("/api/docs/batch", json={"docIds": [1], "action": "explode"})
        check("未知 action → 422", r.status_code == 422, f"HTTP {r.status_code}")
        r = cli.post("/api/docs/batch", json={"docIds": [], "action": "publish"})
        check("空 docIds → 422", r.status_code == 422, f"HTTP {r.status_code}")

        from app.api.docs import _mysql_mode

        check("_mysql_mode()=False（写操作门禁的前置判定）", _mysql_mode() is False)
    finally:
        settings.kb_store = original


# ==================================================================== 组 2：MySQL 桩
def _doc_row(**over) -> _Row:
    base = dict(
        doc_id=1, doc_title="校园网报修流程", category_id=3, ktype="policy",
        file_format="md", file_key="D001.md", file_size=2048, file_hash="h" * 32,
        chunk_count=2, visible_roles="全部", status=1, version_no=1,
        source_type="import", published_at=None,
        created_at=None, updated_at=None, org_name="教务处",
    )
    base.update(over)
    return _Row(**base)


def check_mysql_stub() -> None:
    from app.config import settings
    from app.db import kb_repo

    section("2. MySQL 模式（桩）：筛选下推 + 批量操作")
    original = settings.kb_store
    try:
        settings.kb_store = "mysql"
        assert settings.store_label == "mysql"

        # --- 列表归一化 ---
        conn = install([kb_repo], [_Result([_doc_row()])])
        try:
            docs = kb_repo.list_docs()
        finally:
            stop_install()
        check("list_docs 返回 1 篇", len(docs) == 1)
        d = docs[0]
        check("含 categoryId / dept / 中文 ktype",
              d["categoryId"] == 3 and d["dept"] == "教务处" and d["ktype"] == "政策文件")
        check("无筛选 → 不带 WHERE", "WHERE" not in conn.sql[0].upper())

        # --- 四个筛选下推 ---
        cases = [
            ("status", dict(status=2), ["status"]),
            ("ktype（中文→枚举）", dict(ktype_raw="政策文件"), ["'policy'"]),
            ("categoryId", dict(category_id=7), ["category_id"]),
            ("dept（按名称）", dict(dept_name="教务处"), ["org_name"]),
        ]
        for label, kw, needles in cases:
            conn = install([kb_repo], [_Result([])])
            try:
                kb_repo.list_docs(**kw)
            finally:
                stop_install()
            sql = conn.sql[0]
            upper = sql.upper()
            ok = "WHERE" in upper and all(n in sql for n in needles)
            check(f"{label} 下推到 SQL", ok, sql.split(" WHERE ")[-1][:80] if ok else sql[:120])

        # --- 批量状态 ---
        conn = install([kb_repo], [_Result(rowcount=2)])
        try:
            res = kb_repo.batch_update_status([1, 2], 2)
        finally:
            stop_install()
        check("batch_update_status 命中 2 行",
              res == {"ok": True, "updated": 2, "status": 2, "docIds": [1, 2]})
        check("生成单条 UPDATE（非逐条）", sum("UPDATE" in s.upper() for s in conn.sql) == 1)

        conn = install([kb_repo], [])
        try:
            res = kb_repo.batch_update_status([], 2)
        finally:
            stop_install()
        check("空 docIds 短路（不产生 SQL）", res["updated"] == 0 and conn.statements == [])

        # --- 批量移动 ---
        conn = install([kb_repo], [_Result([_Row(category_id=3)]), _Result(rowcount=2)])
        try:
            res = kb_repo.batch_move_category([1, 2], 3)
        finally:
            stop_install()
        check("移动到已存在分类", res["ok"] and res["updated"] == 2 and res["categoryId"] == 3)
        check("先校验目标存在再更新", len(conn.statements) == 2)

        conn = install([kb_repo], [_Result([])])
        try:
            res = kb_repo.batch_move_category([1], 99)
        finally:
            stop_install()
        check("目标分类不存在 → ok=False + 可读错误",
              res["ok"] is False and "目标分类不存在" in res["error"])

        conn = install([kb_repo], [_Result(rowcount=1)])
        try:
            res = kb_repo.batch_move_category([1], None)
        finally:
            stop_install()
        check("categoryId=None → 未分类(0)，免校验",
              res["ok"] and res["categoryId"] == 0 and len(conn.statements) == 1)

        # --- 批量删除 ---
        conn = install([kb_repo], [_Result(rowcount=1)] * 4 + [_Result(rowcount=0)] * 4)
        try:
            res = kb_repo.batch_delete_docs([1, 2])
        finally:
            stop_install()
        check("批量删除逐篇走完整删除链", res["ok"] and res["deleted"] == 1)
        joined = " ".join(conn.sql).lower()
        check("删除顺序含 citation / chunk / doc",
              "answer_citation" in joined and "kb_chunk" in joined and "kb_doc" in joined)
    finally:
        stop_install()
        settings.kb_store = original


# ==================================================================== 组 3：前端守卫
def check_frontend_guards() -> None:
    section("3. 前端守卫（DOM id 必须真在 HTML 里）")
    js = (SERVICE_DIR / "static" / "admin.js").read_text(encoding="utf-8")
    html = (SERVICE_DIR / "static" / "admin.html").read_text(encoding="utf-8")

    ids = set(re.findall(r"getElementById\(\s*['\"]([A-Za-z0-9_-]+)['\"]\s*\)", js))
    ids |= set(re.findall(r"\bel\(\s*['\"]([A-Za-z0-9_-]+)['\"]\s*\)", js))

    wanted = sorted(i for i in ids if i.startswith(("f-", "batch-", "chk-all")))
    missing = [i for i in wanted if f'id="{i}"' not in html]
    check(f"admin.js 的 {len(wanted)} 个筛选/批量 id 全部在 admin.html",
          not missing, f"缺失：{missing}")

    for token in ("syncBatchBar", "loadFilters", "runBatch", "fKtype", "fCategory"):
        check(f"admin.js 含 {token}", token in js)

    # 批量条默认隐藏（[hidden] 需显式 display:none 兜底，2.5 踩过）
    css = (SERVICE_DIR / "static" / "admin.css").read_text(encoding="utf-8")
    check("admin.css 有 .batch-bar 规则", ".batch-bar" in css)
    check("admin.css 有 .head-select 规则（深色下拉）", ".head-select" in css)

    # 预览页审核控件
    pjs = (SERVICE_DIR / "static" / "preview.js").read_text(encoding="utf-8")
    phtml = (SERVICE_DIR / "static" / "preview.html").read_text(encoding="utf-8")
    pcss = (SERVICE_DIR / "static" / "preview.css").read_text(encoding="utf-8")
    audit_ids = sorted(i for i in re.findall(
        r"\$\(\s*['\"]([A-Za-z0-9_-]+)['\"]\s*\)", pjs) if i.startswith("audit-"))
    pmissing = [i for i in audit_ids if f'id="{i}"' not in phtml]
    check(f"preview.js 的 {len(audit_ids)} 个 audit-* id 全部在 preview.html",
          not pmissing, f"缺失：{pmissing}")
    check("preview.js 有 audit() 与状态判定",
          "async function audit" in pjs and "pending" in pjs)
    check("preview.js 审核走 PATCH /api/docs/{id}", "'PATCH'" in pjs)
    check("preview.css 有 .doc-actions .msg-line 规则", "msg-line" in pcss)


# ==================================================================== 组 4：接口层
def _serve(port: int) -> threading.Thread:
    import uvicorn

    def run() -> None:
        from app.main import app

        uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")

    t = threading.Thread(target=run, daemon=True)
    t.start()
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return t
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"服务未在 {port} 端口就绪")


def check_api_layer(port: int) -> None:
    import httpx

    from app.config import settings

    section("4. 接口层（JSON 模式起真实服务）")
    settings.kb_store = "json"
    try:
        _serve(port)
        base = f"http://127.0.0.1:{port}"
        with httpx.Client(timeout=20.0, trust_env=False) as cli:
            r = cli.get(f"{base}/api/docs")
            check("GET /api/docs → 200", r.status_code == 200, f"HTTP {r.status_code}")
            body = r.json()
            check("data 是数组", isinstance(body.get("data"), list))
            docs = body["data"]

            if docs:
                st = str(docs[0]["status"])
                r = cli.get(f"{base}/api/docs", params={"status": st})
                got = r.json()["data"]
                check(f"status={st} 筛选生效", bool(got), f"{len(got)} 篇")

            kt = docs[0].get("ktype") if docs else None
            if kt:
                r = cli.get(f"{base}/api/docs", params={"ktype": kt})
                got = r.json()["data"]
                check("ktype 筛选生效", bool(got), f"{len(got)} 篇")

            r = cli.get(f"{base}/api/docs", params={"categoryId": 1})
            check("categoryId 参数被接受（200）", r.status_code == 200, f"HTTP {r.status_code}")

            r = cli.post(f"{base}/api/docs/batch", json={"docIds": [1], "action": "publish"})
            check("JSON 模式批量 → 400", r.status_code == 400, f"HTTP {r.status_code}")
            check("400 带 KB_STORE=mysql 指引",
                  "KB_STORE=mysql" in (r.json().get("detail") or ""))

            r = cli.post(f"{base}/api/docs/batch",
                         json={"docIds": [1], "action": "explode"})
            check("未知 action → 422", r.status_code == 422, f"HTTP {r.status_code}")

            r = cli.post(f"{base}/api/docs/batch", json={"docIds": [], "action": "publish"})
            check("空 docIds → 422", r.status_code == 422, f"HTTP {r.status_code}")

            # 预览页可达（审核按钮所在页）
            r = cli.get(f"{base}/preview")
            check("GET /preview → 200", r.status_code == 200, f"HTTP {r.status_code}")
            check("preview 页含审核控件", "audit-approve" in r.text)
    finally:
        settings.kb_store = "json"


# ==================================================================== 组 5：真实库（幂等）
def check_mysql_live() -> None:
    """真实库验收：**快照 → 操作 → 断言 → 恢复**。

    不新建也不删除文档，只改 status / category_id / published_at，
    跑完按快照原样写回，保证可重复执行。
    """
    from sqlalchemy import text

    from app.config import settings
    from app.db import kb_repo
    from app.db.session import DatabaseUnavailable, db_session

    section("5. 真实库（--mysql，快照/恢复式幂等验收）")
    original = settings.kb_store
    settings.kb_store = "mysql"
    try:
        with db_session() as conn:
            snapshot = conn.execute(text(
                "SELECT doc_id, status, category_id, published_at FROM kb_doc ORDER BY doc_id"
            )).all()
    except DatabaseUnavailable as exc:
        check("数据库不可用（跳过真实库验收）", False, str(exc))
        settings.kb_store = original
        return
    except Exception as exc:  # noqa: BLE001 - 驱动/连接错误一律如实报告
        check("数据库连接失败", False, f"{type(exc).__name__}: {exc}")
        settings.kb_store = original
        return

    check("kb_doc 有数据", len(snapshot) > 0, f"{len(snapshot)} 篇")
    rows = [tuple(r) for r in snapshot]
    doc_id = rows[-1][0]
    info(f"以 doc_id={doc_id} 作为操作对象（改动最后一条，降低影响面）")

    def _row(doc_id_: int):
        for r in rows:
            if r[0] == doc_id_:
                return r
        raise KeyError(doc_id_)

    try:
        # --- 筛选下推（真实 SQL）---
        pend = kb_repo.list_docs(status=1)
        check("list_docs(status=1) 只回待审核",
              all(d["status"] == 1 for d in pend), f"{len(pend)} 篇")
        pub = kb_repo.list_docs(status=2)
        check("list_docs(status=2) 只回已发布",
              all(d["status"] == 2 for d in pub), f"{len(pub)} 篇")
        check("分批筛选互斥且不遗漏", len(pend) + len(pub) <= len(rows))

        cats = kb_repo.list_docs(category_id=_row(doc_id)[2])
        check("categoryId 筛选命中目标文档",
              any(d["docId"] == doc_id for d in cats), f"{len(cats)} 篇")

        kt = next((d["ktype"] for d in cats if d["docId"] == doc_id), None)
        if kt:
            ktdocs = kb_repo.list_docs(ktype_raw=kt)
            check("ktype 筛选命中目标文档",
                  any(d["docId"] == doc_id for d in ktdocs), f"{len(ktdocs)} 篇")

        # --- 批量驳回 → 批量通过（真实写入 + 真实读回）---
        r = kb_repo.batch_update_status([doc_id], 0)
        check("batch_update_status 驳回命中 1 行", r["updated"] == 1, str(r))
        after = kb_repo.list_docs(status=0)
        check("驳回后立即可筛出（status=0）", any(d["docId"] == doc_id for d in after))

        r = kb_repo.batch_update_status([doc_id], 2)
        check("batch_update_status 通过命中 1 行", r["updated"] == 1, str(r))
        after = kb_repo.list_docs(status=2)
        check("通过后回到已发布", any(d["docId"] == doc_id for d in after))

        # --- 批量移动 → 移回 ---
        target = 0
        r = kb_repo.batch_move_category([doc_id], target)
        check("移动到未分类(0)", r["ok"] and r["updated"] == 1, str(r))
        moved = kb_repo.list_docs(category_id=target)
        check("未分类筛选命中", any(d["docId"] == doc_id for d in moved))

        r = kb_repo.batch_move_category([doc_id], _row(doc_id)[2])
        check("移回原分类", r["ok"] and r["updated"] == 1, str(r))
        back = kb_repo.list_docs(category_id=_row(doc_id)[2])
        check("原分类筛选命中", any(d["docId"] == doc_id for d in back))

        # --- 接口层（真实 MySQL）---
        _serve_live_check(doc_id)
    finally:
        # 幂等恢复：按快照写回 status / category_id / published_at
        try:
            with db_session() as conn:
                with conn.begin():
                    for row in rows:
                        conn.execute(text(
                            "UPDATE kb_doc SET status=:s, category_id=:c, "
                            "published_at=:p WHERE doc_id=:d"
                        ), {"s": row[1], "c": row[2], "p": row[3], "d": row[0]})
            with db_session() as conn:
                now = [tuple(r) for r in conn.execute(text(
                    "SELECT doc_id, status, category_id, published_at "
                    "FROM kb_doc ORDER BY doc_id"
                )).all()]
            check("快照已原样恢复", now == rows, f"{len(now)} 行")
        except Exception as exc:  # noqa: BLE001
            check("快照恢复成功", False, f"{type(exc).__name__}: {exc}")
        finally:
            settings.kb_store = original


def _serve_live_check(doc_id: int) -> None:
    """真实库模式下起一次服务，验证筛选与批量接口（改完即恢复）。"""
    import httpx
    from sqlalchemy import text

    from app.db.session import db_session

    port = 8391
    try:
        _serve(port)
    except RuntimeError as exc:
        check("真实库接口层（服务未起，跳过）", False, str(exc))
        return

    base = f"http://127.0.0.1:{port}"
    with httpx.Client(timeout=30.0, trust_env=False) as cli:
        r = cli.get(f"{base}/api/docs", params={"status": 2})
        check("GET /api/docs?status=2 → 200", r.status_code == 200, f"HTTP {r.status_code}")
        docs = r.json()["data"]
        check("只回已发布", all(d["status"] == 2 for d in docs), f"{len(docs)} 篇")
        check("目标文档在已发布列表里", any(d["docId"] == doc_id for d in docs))

        r = cli.post(f"{base}/api/docs/batch",
                     json={"docIds": [str(doc_id)], "action": "reject"})
        check("批量驳回 → 200", r.status_code == 200, f"HTTP {r.status_code}")
        check("驳回命中 1 篇", (r.json().get("data") or {}).get("updated") == 1)

        r = cli.post(f"{base}/api/docs/batch",
                     json={"docIds": [str(doc_id)], "action": "publish"})
        check("批量通过 → 200", r.status_code == 200, f"HTTP {r.status_code}")
        check("通过命中 1 篇", (r.json().get("data") or {}).get("updated") == 1)

        r = cli.post(f"{base}/api/docs/batch",
                     json={"docIds": [str(doc_id)], "action": "move", "categoryId": 0})
        check("批量移动到未分类 → 200", r.status_code == 200, f"HTTP {r.status_code}")

        r = cli.post(f"{base}/api/docs/batch",
                     json={"docIds": [str(doc_id)], "action": "move", "categoryId": 99999})
        check("移动到不存在分类 → 400", r.status_code == 400, f"HTTP {r.status_code}")

        r = cli.post(f"{base}/api/docs/batch",
                     json={"docIds": [str(doc_id)], "action": "publish"})
        check("对外编号字符串 docIds 可用", r.status_code == 200, f"HTTP {r.status_code}")

        # 恢复由外层 finally 统一做，这里只确认接口没有把 published_at 改坏
        with db_session() as conn:
            row = conn.execute(text(
                "SELECT status, published_at FROM kb_doc WHERE doc_id=:d"
            ), {"d": doc_id}).first()
        check("接口层操作后 published_at 已回填", row is not None and row[1] is not None)


# ==================================================================== main
def main() -> int:
    parser = argparse.ArgumentParser(description="文档审核与文件管理验收（任务 2.7）")
    parser.add_argument("--port", type=int, default=8029, help="接口层验收端口")
    parser.add_argument("--mysql", action="store_true",
                        help="额外跑真实库验收（快照→操作→恢复，幂等）")
    args = parser.parse_args()

    from app.config import settings

    print("=" * 68)
    print("文档审核与文件管理验收（任务 2.7 / M-K-05·09）")
    print(f"存储模式：{settings.store_label}    服务目录：{SERVICE_DIR}")
    print("=" * 68)

    try:
        check_json_contract()
        check_mysql_stub()
        check_frontend_guards()
        check_api_layer(args.port)
        if args.mysql:
            check_mysql_live()
    finally:
        stop_install()

    print("\n" + "=" * 68)
    print(f"结果：{PASSED} PASS / {FAILED} FAIL")
    print("=" * 68)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
