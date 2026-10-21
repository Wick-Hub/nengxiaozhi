"""敏感词库与操作日志验收（任务 2.10 / 3.6 / M-SYS-07·08）。

自包含，四组检查：

1. **词表加载优先级**：MySQL 词库 → 文件 → 内置；内置意图词始终生效；
   缓存失效后新词立即生效；
2. **服务层（MySQL 桩）**：分页筛选、同词 409、状态切换、删除、非法入参；
   操作日志筛选下推与分页；
3. **接口层（真实服务）**：
   - 默认 JSON 模式：词库只读、写操作 400、操作日志空态；
   - ``--mysql``：新增词条 → 提问被风控 → 停用后放行 → 删除后复原，
     并校验操作日志可按动作筛选；
4. **前端守卫**：admin.js 引用的 sw-* / op-* id 均存在于 admin.html。

用法::

    python tools/verify_sensitive.py              # JSON + 桩（无需数据库）
    python tools/verify_sensitive.py --mysql      # 追加真实 MySQL 端到端
    python tools/verify_sensitive.py --port 8031

退出码：全部通过 0，存在 FAIL 1。
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

# settings 在导入期定型：默认 JSON；--mysql 时使用 .env 里的连接参数
if "--mysql" in sys.argv:
    os.environ["KB_STORE"] = "mysql"
else:
    os.environ.setdefault("KB_STORE", "json")
os.environ.setdefault("LLM_MOCK", "1")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")

try:  # pragma: no cover
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


# ==================================================================== 组 1：加载优先级
def check_loader(monkeypatch_attrs: dict) -> None:
    """加载优先级与缓存失效（临时替换 _load_db_words 与文件路径）。"""
    from app.config import settings
    from app.security import sensitive

    section("1. 词表加载优先级与缓存")

    original_db = sensitive._load_db_words
    original_file = settings.sensitive_words_file
    try:
        words = sensitive.load_words(force=True)
        check("内置意图词始终生效", "忽略以上" in words, f"{len(words)} 条")

        sensitive._load_db_words = lambda: ("库词",)
        words = sensitive.load_words(force=True)
        check("DB 词条优先生效", "库词" in words and sensitive.stats()["source"] == "db")

        sensitive._load_db_words = lambda: ()
        settings.sensitive_words_file = str(monkeypatch_attrs["words_file"])
        words = sensitive.load_words(force=True)
        check("DB 为空回退文件", "文件词" in words and sensitive.stats()["source"] == "file")

        sensitive._load_db_words = lambda: ("旧词",)
        sensitive.load_words(force=True)
        sensitive._load_db_words = lambda: ("新词",)
        check("缓存期内不重载", "新词" not in sensitive.load_words())
        sensitive.invalidate()
        check("invalidate 后新词立即命中", sensitive.check("新词") == "新词")
    finally:
        sensitive._load_db_words = original_db
        settings.sensitive_words_file = original_file
        sensitive.invalidate()


# ==================================================================== 组 2：服务层桩
class _Result:
    def __init__(self, rows=(), *, inserted=None, rowcount=0):
        self._rows = [tuple(r) for r in rows]
        self._inserted = inserted
        self.rowcount = rowcount

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._rows[0][0] if self._rows else None

    @property
    def inserted_primary_key(self):
        return (self._inserted,)


class _Conn:
    def __init__(self, results):
        self._queue = list(results)
        self.statements = []

    def execute(self, stmt, *a, **k):
        self.statements.append(stmt)
        if not self._queue:
            raise AssertionError("MySQL 桩：SQL 调用次数多于预期")
        return self._queue.pop(0)


class _Session:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *exc):
        return False


def _word_row(word_id=1, word="测试词", status=1):
    now = datetime(2026, 10, 20, 10, 0, 0)
    return (word_id, word, "sensitive", "自定义", status, "", None, now, now)


def check_service_stub() -> None:
    from app.config import settings
    from app.security import sensitive
    from app.services import sensitive_admin

    section("2. 服务层（MySQL 桩）")

    original_store = settings.kb_store
    original_loader = sensitive._load_db_words
    ok_sessions = []

    def install(results):
        conn = _Conn(results)
        sess = _Session(conn)
        sensitive_admin.db_session = lambda: sess
        sensitive_admin.db_transaction = lambda: sess
        ok_sessions.append(conn)
        return conn

    try:
        settings.kb_store = "mysql"
        sensitive._load_db_words = lambda: ()

        conn = install(
            [
                _Result([(2,)]),
                _Result([_word_row(2, "违规词"), _word_row(1, "旧词")]),
                _Result([("sensitive", 1, 2), ("keyword", 1, 3)]),
            ]
        )
        data = sensitive_admin.list_words(kind="sensitive", status=1, keyword="词", page=1, page_size=10)["data"]
        sql = "\n".join(str(s.compile(compile_kwargs={"literal_binds": True})) for s in conn.statements)
        check("列表返回 total/items/stats", data["total"] == 2 and len(data["items"]) == 2)
        check("筛选条件下推到 SQL", "sensitive_word.kind" in sql and "sensitive_word.word LIKE" in sql)

        install([_Result([_word_row(word="测试词")])])
        dup = sensitive_admin.create_word({"word": "测试词"})
        check("同名词条返回 409", dup.get("status") == 409, str(dup.get("error")))

        install([_Result([]), _Result([], inserted=7), _Result([_word_row(7, "新增词")])])
        created = sensitive_admin.create_word({"word": "新增词"})
        check("新增成功且缓存失效", created["ok"] and sensitive._words is None)

        install([_Result([_word_row(1, "词条")]), _Result([], rowcount=1), _Result([_word_row(1, "词条", 0)])])
        toggled = sensitive_admin.update_word(1, {"status": 0})
        check("停用后 status=0", toggled["ok"] and toggled["data"]["status"] == 0)

        install([_Result([])])
        check("不存在返回 404", sensitive_admin.update_word(99, {"status": 0}).get("status") == 404)
        check("空词条返回 400", sensitive_admin.create_word({"word": "  "}).get("status") == 400)

        from app.security import oplog

        conn = install(
            [
                _Result([(1,)]),
                _Result([(9, 5, "KB_UPLOAD", "kb_doc", "D001", {}, "", datetime(2026, 10, 20), "20230001", "张三")]),
                _Result([(5, "20230001", "张三")]),
            ]
        )
        oplog.db_transaction = lambda: _Session(conn)
        logs = oplog.query_operations(user_id=5, action="KB_UPLOAD", date_from="2026-10-01", page=1, page_size=10)
        osql = "\n".join(str(s.compile(compile_kwargs={"literal_binds": True})) for s in conn.statements)
        check("操作日志 total/操作人回填", logs["total"] == 1 and logs["items"][0]["operatorName"] == "张三")
        check("操作人/动作/时间筛选下推", "operation_log.user_id" in osql and "operation_log.created_at >=" in osql)
        check("操作日志分页参数生效", "LIMIT 10" in osql)
    finally:
        settings.kb_store = original_store
        sensitive._load_db_words = original_loader
        sensitive.invalidate()
        # 恢复被替换的模块属性（脚本退出即结束，不影响其他进程）
        import importlib

        importlib.reload(sensitive_admin)
        importlib.reload(__import__("app.security.oplog", fromlist=["oplog"]))


# ==================================================================== 组 3：接口层
def _serve(port: int):
    import socket

    import uvicorn

    def run():
        from app.main import app

        uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")

    threading.Thread(target=run, daemon=True).start()
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"服务未在 {port} 端口就绪")


def check_api(port: int, mysql: bool) -> None:
    import httpx

    section(f"3. 接口层（真实服务，{'MySQL 端到端' if mysql else 'JSON 只读'}）")
    _serve(port)
    base = f"http://127.0.0.1:{port}"
    created_id = None
    with httpx.Client(timeout=30.0, trust_env=False) as cli:
        r = cli.get(f"{base}/api/admin/sensitive-words?page=1&pageSize=5")
        body = r.json()
        check("GET 词库 200", r.status_code == 200 and body.get("ok") is True)

        if not mysql:
            check("JSON 模式只读", body["data"]["editable"] is False)
            check("JSON 模式展示生效词表", body["data"]["total"] >= 16, f"{body['data']['total']} 条")
            r = cli.post(f"{base}/api/admin/sensitive-words", json={"word": "验收词"})
            check("JSON 模式写入 400 + 指引", r.status_code == 400 and "MySQL" in r.text)
            r = cli.get(f"{base}/api/admin/operation-logs")
            data = r.json()["data"]
            check("操作日志空态且带筛选项", data["items"] == [] and len(data["actions"]) >= 20)
            return

        word = f"验收风控词{int(time.time())}"
        try:
            r = cli.post(f"{base}/api/admin/sensitive-words", json={"word": word, "category": "验收"})
            check("新增词条 200", r.status_code == 200 and r.json()["ok"] is True, f"HTTP {r.status_code}")
            created_id = r.json()["data"]["wordId"]
            check("新词默认启用", r.json()["data"]["status"] == 1)

            r = cli.post(f"{base}/api/chat", json={"question": f"请解释{word}", "role": "本科生"})
            ans = r.json().get("data") or {}
            check("新增后提问立即被风控", ans.get("refused") is True)

            r = cli.patch(f"{base}/api/admin/sensitive-words/{created_id}", json={"status": 0})
            check("停用词条 200", r.status_code == 200 and r.json()["data"]["status"] == 0)
            r = cli.post(f"{base}/api/chat", json={"question": f"请解释{word}", "role": "本科生"})
            ans = r.json().get("data") or {}
            check("停用后不再拦截", ans.get("refused") is not True)

            r = cli.get(
                f"{base}/api/admin/operation-logs",
                params={"action": "SENSITIVE_CREATE", "page": 1, "pageSize": 5},
            )
            logs = r.json()["data"]
            check("操作日志可按动作筛选", logs["total"] >= 1 and logs["items"][0]["action"] == "SENSITIVE_CREATE")
            check("操作日志带操作人与明细", "operatorName" in logs["items"][0] and logs["items"][0]["detail"])
        finally:
            if created_id is not None:
                r = cli.delete(f"{base}/api/admin/sensitive-words/{created_id}")
                check("清理测试词条", r.status_code == 200)
                r = cli.post(f"{base}/api/chat", json={"question": f"请解释{word}", "role": "本科生"})
                check("删除后不再拦截", (r.json().get("data") or {}).get("refused") is not True)


# ==================================================================== 组 4：前端守卫
def check_frontend() -> None:
    section("4. 前端守卫")
    static = SERVICE_DIR / "static"
    js = (static / "admin.js").read_text(encoding="utf-8")
    html = (static / "admin.html").read_text(encoding="utf-8")

    ids = [
        "sw-kind", "sw-status", "sw-q", "sw-count", "sw-note", "sw-form", "sw-word",
        "sw-form-kind", "sw-category", "sw-remark", "sw-add", "sw-msg", "sw-tbody",
        "sw-empty", "sw-pager", "op-user", "op-action", "op-from", "op-to", "op-count",
        "op-note", "op-tbody", "op-empty", "op-pager", "op-search", "op-reset", "op-refresh",
    ]
    missing = sorted(i for i in ids if f'id="{i}"' not in html)
    check("admin.html 包含全部 sw-*/op-* 节点", not missing, f"缺失：{missing}")
    for fn in ("loadWords", "renderWords", "addWord", "handleWordOp", "loadOperations", "renderOpLog"):
        check(f"admin.js 含 {fn}", fn in js)
    check("系统管理页签已挂载", 'data-tab="system"' in html and "initSystemPage" in js)


# ==================================================================== main
def main() -> int:
    parser = argparse.ArgumentParser(description="敏感词库与操作日志验收（任务 2.10 / 3.6）")
    parser.add_argument("--port", type=int, default=8031, help="接口层验收端口")
    parser.add_argument("--mysql", action="store_true", help="追加真实 MySQL 端到端（写入并清理测试词）")
    args = parser.parse_args()

    import tempfile

    from app.config import settings

    print("=" * 68)
    print("敏感词库与操作日志验收（任务 2.10 / 3.6 / M-SYS-07·08）")
    print(f"存储模式：{settings.store_label}    数据库：{settings.db_host}:{settings.db_port}/{settings.db_name}")
    print("=" * 68)

    with tempfile.TemporaryDirectory() as tmp:
        words_file = Path(tmp) / "words.txt"
        words_file.write_text("# 验收词表\n文件词\n", encoding="utf-8")
        check_loader({"words_file": words_file})

    check_service_stub()
    check_api(args.port, args.mysql)
    check_frontend()

    print("\n" + "=" * 68)
    print(f"结果：{PASSED} PASS / {FAILED} FAIL")
    print("=" * 68)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
