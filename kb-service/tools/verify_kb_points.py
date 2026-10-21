"""知识点管理验收（任务 2.8 / M-K-10）。

自包含：**默认不依赖 MySQL**。五组自检：

1. **JSON 只读契约**：``/api/knowledge/points`` 返回
   ``{items,total,page,size,editable,filters}``；``editable=False``；
   列表只带 ``excerpt``（正文由详情接口给）；关键词多词 AND；写操作
   返回 400 + ``KB_STORE=mysql`` 指引；
2. **服务编排（桩）**：新建取 ``max(seq)+1``、正文变更才重算向量、
   向量失败不回滚内容、写后回写 ``chunk_count`` 并失效索引、
   再加工的删除语义（按片段/按区间/删空拒绝）；
3. **前端守卫**：points.js 引用的 id 必须真在 points.html 里；
   ``/points`` 必须在 ``app.mount("/")`` **之前**注册（否则被静态挂载吞掉）；
   管理页要有入口链接；
4. **接口层**：真实起服务，JSON 模式下跑列表/详情/导出/写门禁，
   并确认 ``/points`` 页面与静态资源可达；
5. **真实库（``--mysql``，幂等）**：先快照目标文档的 ``kb_chunk`` +
   向量 + ``chunk_count``，跑一轮真实的「新建 → 编辑 → 追加 → 删内容 →
   导出 → 删除」，最后**原样恢复**。

用法::

    python tools/verify_kb_points.py
    python tools/verify_kb_points.py --mysql
    python tools/verify_kb_points.py --port 8031

退出码：全部通过为 0，存在 FAIL 为 1。
"""

from __future__ import annotations

import argparse
import json
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


# ==================================================================== 组 1：JSON 只读契约
def check_json_contract() -> None:
    from app.config import settings
    from app.services import kb_knowledge

    section("1. JSON 只读契约（列表/详情/写门禁）")
    original = settings.kb_store
    try:
        settings.kb_store = "json"
        check("store_label=json", settings.store_label == "json")

        res = kb_knowledge.list_points(size=5)
        check("list_points ok", res.get("ok") is True, str(res.get("error"))[:60])
        data = res["data"]
        for key in ("items", "total", "page", "size", "editable", "filters"):
            check(f"data 含 {key}", key in data)
        check("只读模式 editable=False", data["editable"] is False)
        check("列表非空", data["total"] > 0, f"{data['total']} 条")
        check("列表只带 excerpt", "excerpt" in data["items"][0] and "content" not in data["items"][0])
        check("excerpt 不超过 240 字", len(data["items"][0]["excerpt"]) <= 240)
        check("filters 有部门可选值", bool(data["filters"]["depts"]),
              f"{data['filters']['depts'][:3]}")
        check("filters 有块类型可选值", bool(data["filters"]["blockTypes"]))

        first = data["items"][0]
        detail = kb_knowledge.get_point(first["chunkId"])
        check("详情返回全文", detail.get("ok") is True and bool(detail["data"]["content"]))
        check("详情 editable=False", detail["data"]["editable"] is False)
        check("不存在的知识点 → 失败", kb_knowledge.get_point("D999-C999").get("ok") is False)

        # 筛选
        by_dept = kb_knowledge.list_points(dept=first["dept"], size=200)["data"]
        check("dept 筛选生效",
              bool(by_dept["items"]) and all(i["dept"] == first["dept"] for i in by_dept["items"]),
              f"{by_dept['total']} 条")
        by_block = kb_knowledge.list_points(block_type=first["blockType"], size=200)["data"]
        check("blockType 筛选生效",
              all(i["blockType"] == first["blockType"] for i in by_block["items"]),
              f"{by_block['total']} 条")
        by_doc = kb_knowledge.list_points(doc_id=int(first["docId"][1:]), size=200)["data"]
        check("docId 筛选生效",
              bool(by_doc["items"]) and all(i["docId"] == first["docId"] for i in by_doc["items"]),
              f"{by_doc['total']} 条")

        one = kb_knowledge.list_points(q="校园", size=200)["data"]["total"]
        two = kb_knowledge.list_points(q="校园 网络", size=200)["data"]["total"]
        check("关键词多词 AND（命中数单调不增）", one >= two, f"{one} → {two}")

        # 分页
        page1 = kb_knowledge.list_points(page=1, size=2)["data"]
        page2 = kb_knowledge.list_points(page=2, size=2)["data"]
        ids1 = [i["chunkId"] for i in page1["items"]]
        ids2 = [i["chunkId"] for i in page2["items"]]
        check("分页不重复", not (set(ids1) & set(ids2)), f"{ids1} / {ids2}")

        # 写门禁
        for label, res in (
            ("新建", kb_knowledge.create_point({"docId": "D001", "content": "x"})),
            ("编辑", kb_knowledge.update_point("D001-C001", {"content": "x"})),
            ("删除", kb_knowledge.delete_point("D001-C001")),
            ("追加", kb_knowledge.append_text("D001-C001", "x")),
            ("删内容", kb_knowledge.remove_content("D001-C001", text="x")),
            ("插图", kb_knowledge.insert_image("D001-C001", "a.png")),
        ):
            check(f"{label} → 400 指引", res.get("ok") is False and "MySQL" in (res.get("error") or ""))
    finally:
        settings.kb_store = original


# ==================================================================== 组 2：服务编排（桩）
class _Rec:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def add(self, name: str, **kw) -> None:
        self.calls.append((name, kw))

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    def last(self, name: str) -> dict:
        for n, kw in reversed(self.calls):
            if n == name:
                return kw
        return {}


def _point(chunk_id: str = "D001-C001", **over) -> dict:
    base = {
        "chunkId": chunk_id, "docId": "D001", "docTitle": "校园网开户与报修指南",
        "seqNo": 1, "blockType": "para", "titlePath": "校园网 / 开户",
        "charCount": 10, "dept": "信息网络中心", "ktype": "办事指南",
        "categoryId": 1, "visibleRoles": ["全部"], "vecSynced": 1, "createdAt": None,
        "content": "0123456789",
    }
    base.update(over)
    return base


def check_service_orchestration() -> None:
    """用 mock 替换 repo，验证编排顺序与边界（不碰数据库）。"""
    from unittest.mock import patch

    import app.core.index_store as idx_mod
    from app.config import settings
    from app.services import kb_knowledge

    section("2. 服务编排（新建/编辑/再加工/删除）")
    original = settings.kb_store
    rec = _Rec()
    state = {"point": _point()}

    class _Emb:
        def encode_one(self, text):
            rec.add("encode_one", text=text)
            return [0.1] * 8

    class _Boom:
        def encode_one(self, text):
            raise RuntimeError("模型不可用")

    def _update(cid, fields):
        rec.add("update_point_fields", chunk_id=cid, fields=dict(fields))
        if "content" in fields:
            state["point"]["content"] = fields["content"]
            state["point"]["charCount"] = fields["char_count"]
        if "title_path" in fields:
            state["point"]["titlePath"] = fields["title_path"]
        if "image_keys" in fields:
            if fields["image_keys"]:
                state["point"]["imageKeys"] = list(fields["image_keys"])
            else:
                state["point"].pop("imageKeys", None)
        return True

    settings.kb_store = "mysql"
    try:
        with patch.multiple(
            kb_knowledge.kb_repo,
            point_scope=lambda cid: {"chunkId": cid, "docId": 1, "deptId": 100, "ktype": "guide"},
            get_point=lambda cid: state["point"],
            get_doc=lambda doc_id: {"docId": doc_id, "title": "T", "dept": "信息网络中心",
                                    "ktype": "办事指南"},
            list_chunks=lambda doc_id: [],
            max_chunk_seq=lambda doc_id: rec.add("max_chunk_seq", doc_id=doc_id) or 7,
            insert_point=lambda **kw: (rec.add("insert_point", **kw)
                                       or f"D{kw['doc_id']:03d}-C{kw['seq_no']:03d}"),
            update_point_fields=_update,
            delete_point=lambda cid: (rec.add("delete_point", chunk_id=cid)
                                      or {"ok": True, "deleted": 1, "chunkId": cid, "docId": 1}),
            recount_doc_chunks=lambda doc_id: rec.add("recount_doc_chunks", doc_id=doc_id) or 8,
            save_point_vector=lambda cid, emb, model=None: rec.add(
                "save_point_vector", chunk_id=cid, dim=len(emb)) or 1,
            drop_point_vector=lambda cid: rec.add("drop_point_vector", chunk_id=cid) or 1,
        ), patch.object(kb_knowledge, "get_embedder", lambda: _Emb()), \
                patch.object(idx_mod, "clear_index", lambda: rec.add("clear_index")), \
                patch.object(kb_knowledge.doc_preview, "collect_images",
                             lambda doc, chunks: [{"key": "uploads/images/ok.png"}]):

            # --- 新建：序号 = max+1
            res = kb_knowledge.create_point({"docId": "D001", "content": "新切片正文"})
            ins = rec.last("insert_point")
            check("新建 ok", res.get("ok") is True, str(res.get("error"))[:60])
            check("新建序号 = max(seq)+1", ins.get("seq_no") == 8, f"seq_no={ins.get('seq_no')}")
            check("新建 char_count = 正文长度", ins.get("char_count") == len("新切片正文"))
            check("新建后失效索引", "clear_index" in rec.names())
            check("新建后回写 chunk_count", rec.last("recount_doc_chunks").get("doc_id") == 1)
            check("新建后写向量", rec.last("save_point_vector").get("dim") == 8)

            check("非法块类型被拒",
                  kb_knowledge.create_point({"docId": "D001", "content": "x",
                                             "blockType": "video"}).get("ok") is False)
            check("非法文档编号被拒",
                  kb_knowledge.create_point({"docId": "ABC", "content": "x"}).get("ok") is False)
            check("超长正文被拒",
                  kb_knowledge.create_point({"docId": "D001",
                                             "content": "x" * 200001}).get("ok") is False)

            # --- 编辑：正文变 → 重算；仅元信息 → 不重算
            rec.calls.clear()
            res = kb_knowledge.update_point("D001-C001", {"content": "改过的正文"})
            check("编辑正文 ok", res.get("ok") is True)
            check("编辑正文重算 char_count",
                  rec.last("update_point_fields")["fields"].get("char_count") == 5)
            check("编辑正文重算向量", "encode_one" in rec.names())
            rec.calls.clear()
            res = kb_knowledge.update_point("D001-C001", {"blockType": "table"})
            check("仅改块类型不重算向量", "encode_one" not in rec.names())
            check("空 payload 被拒", kb_knowledge.update_point("D001-C001", {}).get("ok") is False)

            # --- 再加工
            rec.calls.clear()
            res = kb_knowledge.append_text("D001-C001", "补", "end")
            check("追加到末尾",
                  rec.last("update_point_fields")["fields"].get("content") == "改过的正文补")
            rec.calls.clear()
            state["point"]["content"] = "改过的正文"
            res = kb_knowledge.append_text("D001-C001", "补", "start")
            check("插入到开头",
                  rec.last("update_point_fields")["fields"].get("content") == "补改过的正文")
            check("非法 position 被拒",
                  kb_knowledge.append_text("D001-C001", "x", "middle").get("ok") is False)

            rec.calls.clear()
            state["point"]["content"] = "0123456789"
            res = kb_knowledge.remove_content("D001-C001", text="345")
            check("按片段删除", res.get("ok") is True and res["data"]["removed"] == 3,
                  str(res.get("error"))[:40])
            check("按片段删除命中区间", res["data"]["range"] == [3, 6], str(res["data"].get("range")))
            check("片段不存在 → 拒绝",
                  kb_knowledge.remove_content("D001-C001", text="不存在").get("ok") is False)
            check("区间非法 → 拒绝",
                  kb_knowledge.remove_content("D001-C001", start=5, end=5).get("ok") is False)
            check("无定位方式 → 拒绝",
                  kb_knowledge.remove_content("D001-C001").get("ok") is False)
            state["point"]["content"] = "短"
            check("删空正文 → 拒绝",
                  kb_knowledge.remove_content("D001-C001", start=0, end=9).get("ok") is False)

            # --- 插图
            state["point"].pop("imageKeys", None)
            res = kb_knowledge.insert_image("D001-C001", "uploads/images/ok.png")
            check("插图成功", res.get("ok") is True and res["data"]["added"] is True)
            res = kb_knowledge.insert_image("D001-C001", "uploads/images/ok.png")
            check("重复插图不重复写入", res.get("ok") is True and res["data"]["added"] is False)
            res = kb_knowledge.insert_image("D001-C001", "uploads/images/other.png")
            check("外来图片被拒", res.get("ok") is False and "不属于该文档" in res["error"])

            # --- 删除
            rec.calls.clear()
            res = kb_knowledge.delete_point("D001-C001")
            check("删除 ok", res.get("ok") is True and res["data"]["deleted"] == 1)
            check("删除后回写 chunk_count", "recount_doc_chunks" in rec.names())

            # --- 导出
            state["point"] = _point(content="导出正文")
            csv_res = kb_knowledge.export_points(["D001-C001"], "csv")
            check("CSV 带 BOM（Excel 不乱码）",
                  csv_res["data"]["content"].startswith(b"\xef\xbb\xbf"))
            check("CSV 有中文表头", "知识点编号" in csv_res["data"]["content"].decode("utf-8-sig"))
            md_res = kb_knowledge.export_points(["D001-C001"], "md")
            check("Markdown 有分节",
                  md_res["data"]["content"].decode("utf-8").startswith("# 知识点导出"))
            js_res = kb_knowledge.export_points(["D001-C001"], "json")
            check("JSON 是合法数组",
                  isinstance(json.loads(js_res["data"]["content"].decode("utf-8")), list))
            check("非法格式被拒",
                  kb_knowledge.export_points(["D001-C001"], "xlsx").get("ok") is False)

            # --- 向量失败只降级
            with patch.object(kb_knowledge, "get_embedder", lambda: _Boom()):
                rec.calls.clear()
                res = kb_knowledge.update_point("D001-C001", {"content": "改"})
                check("向量失败仍保存正文", res.get("ok") is True)
                check("向量失败置 vecSynced=False", res["data"]["vecSynced"] is False)
                check("向量失败给出 warning", "向量重算失败" in (res["data"]["warning"] or ""))
                check("向量失败清掉旧向量", "drop_point_vector" in rec.names())
    finally:
        settings.kb_store = original


# ==================================================================== 组 3：前端守卫
def check_frontend_guards() -> None:
    section("3. 前端守卫（DOM id / 路由注册 / 入口链接）")
    static = SERVICE_DIR / "static"
    js = (static / "points.js").read_text(encoding="utf-8")
    html = (static / "points.html").read_text(encoding="utf-8")
    main_src = (SERVICE_DIR / "app" / "main.py").read_text(encoding="utf-8")
    admin_html = (static / "admin.html").read_text(encoding="utf-8")

    ids = set(re.findall(r"\$\(\s*['\"]([A-Za-z0-9_-]+)['\"]\s*\)", js))
    ids |= set(re.findall(r"getElementById\(\s*['\"]([A-Za-z0-9_-]+)['\"]\s*\)", js))
    check("points.js 提取到足够 id 引用", len(ids) >= 30, f"{len(ids)} 个")
    missing = sorted(i for i in ids if f'id="{i}"' not in html)
    check("points.js 引用的 id 全部存在", not missing, str(missing))

    for token in ("pt-op-append-toggle", "pt-op-remove-toggle", "pt-op-image-toggle",
                  "pt-export-csv", "pt-export-md", "pt-export-json", "pt-chk-all"):
        check(f"points.html 含 {token}", f'id="{token}"' in html)

    check("points 路由已注册", '"points": _static_dir / "points.html"' in main_src)
    if '"points": _static_dir' in main_src and 'app.mount("/"' in main_src:
        check("注册在 StaticFiles 挂载之前",
              main_src.index('"points": _static_dir') < main_src.index('app.mount("/"'))
    else:
        check("注册在 StaticFiles 挂载之前", False, "缺少注册或挂载语句")

    check("管理页有知识点入口", 'href="/points"' in admin_html)
    check("points.html 用相对路径引资源",
          'href="points.css"' in html and 'src="points.js"' in html)


# ==================================================================== 组 4：接口层
def _serve(port: int) -> threading.Thread:
    import uvicorn

    def run() -> None:
        from app.main import app

        uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")

    t = threading.Thread(target=run, daemon=True)
    t.start()
    deadline = time.time() + 25
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
    _serve(port)
    base = f"http://127.0.0.1:{port}"
    with httpx.Client(timeout=30.0, trust_env=False) as cli:
        r = cli.get(f"{base}/api/knowledge/points", params={"size": 3})
        check("GET /api/knowledge/points → 200", r.status_code == 200, f"HTTP {r.status_code}")
        data = r.json()["data"]
        check("data 结构完整",
              all(k in data for k in ("items", "total", "page", "size", "editable", "filters")))
        check("只读模式 editable=False", data["editable"] is False)
        check("返回条目不超过 size", len(data["items"]) <= 3, f"{len(data['items'])} 条")

        check("size 越界 → 422",
              cli.get(f"{base}/api/knowledge/points", params={"size": 999}).status_code == 422)
        check("page=0 → 422",
              cli.get(f"{base}/api/knowledge/points", params={"page": 0}).status_code == 422)

        cid = data["items"][0]["chunkId"]
        r = cli.get(f"{base}/api/knowledge/points/{cid}")
        check("详情 → 200", r.status_code == 200, f"HTTP {r.status_code}")
        check("详情含全文", bool(r.json()["data"]["content"]))

        r = cli.get(f"{base}/api/knowledge/points", params={"q": "校园"})
        check("关键词检索 → 200", r.status_code == 200 and r.json()["data"]["total"] >= 1,
              f"{r.json()['data']['total']} 条")

        r = cli.post(f"{base}/api/knowledge/points", json={"docId": "D001", "content": "x"})
        check("新建在只读模式 → 400", r.status_code == 400, f"HTTP {r.status_code}")
        check("400 带 KB_STORE=mysql 指引", "KB_STORE=mysql" in (r.json().get("detail") or ""))

        r = cli.post(f"{base}/api/knowledge/points", json={"docId": "D001", "content": ""})
        check("空正文 → 422", r.status_code == 422, f"HTTP {r.status_code}")

        r = cli.post(f"{base}/api/knowledge/export",
                     json={"chunkIds": [cid], "format": "csv"})
        check("导出 CSV → 200", r.status_code == 200, f"HTTP {r.status_code}")
        check("导出带 attachment 头", "attachment" in (r.headers.get("content-disposition") or ""))
        check("导出带 nosniff", r.headers.get("x-content-type-options") == "nosniff")
        check("导出 CSV 带 BOM", r.content.startswith(b"\xef\xbb\xbf"))
        check("导出文件名为 .csv", ".csv" in (r.headers.get("content-disposition") or ""))

        r = cli.post(f"{base}/api/knowledge/export", json={"chunkIds": [], "format": "csv"})
        check("空 chunkIds → 422", r.status_code == 422, f"HTTP {r.status_code}")

        r = cli.post(f"{base}/api/knowledge/export",
                     json={"chunkIds": ["D999-C999"], "format": "csv"})
        check("导出不存在编号 → 400", r.status_code == 400, f"HTTP {r.status_code}")

        # 页面与静态资源
        r = cli.get(f"{base}/points")
        check("GET /points → 200", r.status_code == 200, f"HTTP {r.status_code}")
        check("/points 含表格容器", 'id="pt-body"' in r.text)
        check("/points 引入 points.js", "points.js" in r.text)
        for asset in ("points.js", "points.css", "admin.css"):
            rr = cli.get(f"{base}/{asset}")
            check(f"静态资源 {asset} 可达", rr.status_code == 200, f"HTTP {rr.status_code}")

        r = cli.get(f"{base}/admin")
        check("管理页可达且含知识点入口", r.status_code == 200 and 'href="/points"' in r.text)


# ==================================================================== 组 5：真实库（幂等）
def _snapshot_doc(doc_id: int) -> dict:
    from sqlalchemy import text

    from app.db.session import db_session

    with db_session() as conn:
        res = conn.execute(
            text("SELECT * FROM kb_chunk WHERE doc_id=:d ORDER BY seq_no"), {"d": doc_id}
        )
        keys = list(res.keys())
        rows = [dict(zip(keys, r)) for r in res.all()]

        chunk_ids = [r["chunk_id"] for r in rows]
        vecs: list[dict] = []
        if chunk_ids:
            res2 = conn.execute(
                text("SELECT * FROM kb_chunk_vec_proto WHERE chunk_id IN :ids").bindparams(
                    __import__("sqlalchemy").bindparam("ids", expanding=True)
                ),
                {"ids": chunk_ids},
            )
            vkeys = list(res2.keys())
            vecs = [dict(zip(vkeys, r)) for r in res2.all()]

        cc = conn.execute(
            text("SELECT chunk_count FROM kb_doc WHERE doc_id=:d"), {"d": doc_id}
        ).scalar()
    return {"chunks": rows, "vecs": vecs, "chunkCount": int(cc or 0)}


def _restore_doc(doc_id: int, snap: dict) -> None:
    from sqlalchemy import text

    from app.db.session import db_transaction

    pattern = f"D{doc_id:03d}-C%"
    with db_transaction() as conn:
        conn.execute(
            text("DELETE FROM kb_chunk_vec_proto WHERE chunk_id LIKE :p"), {"p": pattern}
        )
        conn.execute(text("DELETE FROM kb_chunk WHERE doc_id=:d"), {"d": doc_id})
        for row in snap["chunks"]:
            cols = ", ".join(f"`{k}`" for k in row)
            ph = ", ".join(f":{k}" for k in row)
            conn.execute(text(f"INSERT INTO kb_chunk ({cols}) VALUES ({ph})"), row)
        for vec in snap["vecs"]:
            cols = ", ".join(f"`{k}`" for k in vec)
            ph = ", ".join(f":{k}" for k in vec)
            conn.execute(text(f"INSERT INTO kb_chunk_vec_proto ({cols}) VALUES ({ph})"), vec)
        conn.execute(
            text("UPDATE kb_doc SET chunk_count=:n WHERE doc_id=:d"),
            {"n": snap["chunkCount"], "d": doc_id},
        )


def check_mysql_live() -> None:
    """真实库验收：**快照 → 操作 → 断言 → 恢复**。

    只在既有文档下增删切片，不新建/删除文档；跑完把该文档的切片、
    向量与 ``chunk_count`` 原样写回，脚本可反复执行。
    """
    from sqlalchemy import text

    from app.config import settings
    from app.db.session import db_session
    from app.services import kb_knowledge

    section("5. 真实库（快照 → 操作 → 恢复）")
    original = settings.kb_store
    settings.kb_store = "mysql"
    snap: dict | None = None
    doc_id = 0
    try:
        with db_session() as conn:
            row = conn.execute(
                text("SELECT doc_id, doc_title, chunk_count FROM kb_doc ORDER BY doc_id LIMIT 1")
            ).first()
        if row is None:
            check("库内有文档可测", False, "kb_doc 为空")
            return
        doc_id = int(row[0])
        snap = _snapshot_doc(doc_id)
        info(f"目标文档 D{doc_id:03d}「{row[1]}」：快照 {len(snap['chunks'])} 切片 / "
             f"{len(snap['vecs'])} 向量 / chunk_count={snap['chunkCount']}")
        check("快照非空", len(snap["chunks"]) > 0)

        base_seq = max((c["seq_no"] for c in snap["chunks"]), default=0)

        # --- 列表（真实 SQL）
        res = kb_knowledge.list_points(doc_id=doc_id, size=200)
        check("真实库列表 ok", res.get("ok") is True, str(res.get("error"))[:80])
        items = res["data"]["items"]
        check("列表条数与快照一致", len(items) == len(snap["chunks"]),
              f"{len(items)} vs {len(snap['chunks'])}")
        check("列表带 categoryId 字段", "categoryId" in items[0])
        check("列表按序号升序", [i["seqNo"] for i in items] == sorted(i["seqNo"] for i in items))
        check("列表只带 excerpt", "excerpt" in items[0] and "content" not in items[0])
        check("editable=True", res["data"]["editable"] is True)

        q_word = (items[0]["excerpt"] or "")[:4].strip()
        if q_word:
            hit = kb_knowledge.list_points(q=q_word, size=200)["data"]
            check(f"关键词「{q_word}」检索有结果", hit["total"] >= 1, f"{hit['total']} 条")

        # --- 新建
        res = kb_knowledge.create_point(
            {"docId": f"D{doc_id:03d}", "content": "验收临时切片：请勿保留", "blockType": "para"}
        )
        check("真实库新建 ok", res.get("ok") is True, str(res.get("error"))[:80])
        new_id = (res.get("data") or {}).get("point", {}).get("chunkId")
        check("新切片序号 = 旧最大值+1",
              (res.get("data") or {}).get("seqNo") == base_seq + 1,
              f"{base_seq} → {(res.get('data') or {}).get('seqNo')}")
        check("新切片编号符合 Dxxx-Cxxx", bool(new_id) and re.fullmatch(r"D\d{3}-C\d{3}", new_id or ""))
        check("新建返回 vecSynced=True", (res.get("data") or {}).get("vecSynced") is True,
              str((res.get("data") or {}).get("warning"))[:60])

        with db_session() as conn:
            cnt = conn.execute(
                text("SELECT COUNT(*) FROM kb_chunk WHERE doc_id=:d"), {"d": doc_id}
            ).scalar()
            vec = conn.execute(
                text("SELECT dim FROM kb_chunk_vec_proto WHERE chunk_id=:c"), {"c": new_id}
            ).first()
            synced = conn.execute(
                text("SELECT vec_synced FROM kb_chunk WHERE chunk_id=:c"), {"c": new_id}
            ).scalar()
        check("库里确实多了 1 条切片", int(cnt) == len(snap["chunks"]) + 1, f"{cnt}")
        check("新切片有向量", vec is not None, f"dim={vec[0] if vec else None}")
        check("vec_synced=1", int(synced or 0) == 1)

        # --- 编辑（正文变 → 向量重算）
        res = kb_knowledge.update_point(new_id, {"content": "验收临时切片：已编辑", "titlePath": "验收/临时"})
        check("真实库编辑 ok", res.get("ok") is True, str(res.get("error"))[:80])
        with db_session() as conn:
            got = conn.execute(
                text("SELECT content, char_count, title_path FROM kb_chunk WHERE chunk_id=:c"),
                {"c": new_id},
            ).first()
        check("正文已落库", got is not None and got[0] == "验收临时切片：已编辑", str(got[0])[:20] if got else "")
        check("char_count 同步", got is not None and int(got[1]) == len("验收临时切片：已编辑"))
        check("标题路径已落库", got is not None and got[2] == "验收/临时")

        # --- 追加
        res = kb_knowledge.append_text(new_id, "（追加）", "end")
        check("真实库追加 ok", res.get("ok") is True)
        with db_session() as conn:
            content = conn.execute(
                text("SELECT content FROM kb_chunk WHERE chunk_id=:c"), {"c": new_id}
            ).scalar()
        check("追加内容落库", content == "验收临时切片：已编辑（追加）", str(content)[-12:])

        # --- 删内容
        res = kb_knowledge.remove_content(new_id, text="（追加）")
        check("真实库删内容 ok", res.get("ok") is True, str(res.get("error"))[:60])
        with db_session() as conn:
            content = conn.execute(
                text("SELECT content FROM kb_chunk WHERE chunk_id=:c"), {"c": new_id}
            ).scalar()
        check("删除后正文正确", content == "验收临时切片：已编辑", str(content))

        # --- 插图（用该文档已有图片键，没有就跳过）
        res = kb_knowledge.insert_image(new_id, "uploads/images/__nope__.png")
        check("外来图片键被拒", res.get("ok") is False and "不属于该文档" in (res.get("error") or ""))

        # --- 导出
        res = kb_knowledge.export_points([new_id], "csv")
        check("真实库导出 CSV ok", res.get("ok") is True)
        check("导出内容含新切片编号", new_id.encode() in res["data"]["content"])

        # --- 删除
        res = kb_knowledge.delete_point(new_id)
        check("真实库删除 ok", res.get("ok") is True and res["data"]["deleted"] == 1)
        with db_session() as conn:
            left = conn.execute(
                text("SELECT COUNT(*) FROM kb_chunk WHERE chunk_id=:c"), {"c": new_id}
            ).scalar()
            vleft = conn.execute(
                text("SELECT COUNT(*) FROM kb_chunk_vec_proto WHERE chunk_id=:c"), {"c": new_id}
            ).scalar()
            cc = conn.execute(
                text("SELECT chunk_count FROM kb_doc WHERE doc_id=:d"), {"d": doc_id}
            ).scalar()
        check("切片已删除", int(left) == 0)
        check("向量已删除", int(vleft) == 0)
        check("chunk_count 回到原值", int(cc or 0) == snap["chunkCount"],
              f"{cc} vs {snap['chunkCount']}")
    finally:
        if snap is not None:
            _restore_doc(doc_id, snap)
            after = _snapshot_doc(doc_id)
            ok = (len(after["chunks"]) == len(snap["chunks"])
                  and len(after["vecs"]) == len(snap["vecs"])
                  and after["chunkCount"] == snap["chunkCount"])
            check("快照恢复一致", ok,
                  f"切片 {len(after['chunks'])}/{len(snap['chunks'])} · "
                  f"向量 {len(after['vecs'])}/{len(snap['vecs'])} · "
                  f"chunk_count {after['chunkCount']}/{snap['chunkCount']}")
        settings.kb_store = original


# ==================================================================== main
def main() -> int:
    ap = argparse.ArgumentParser(description="知识点管理验收（任务 2.8 / M-K-10）")
    ap.add_argument("--mysql", action="store_true", help="追加真实库幂等验收（组 5）")
    ap.add_argument("--port", type=int, default=8031, help="组 4 起服务用的端口")
    ap.add_argument("--only", default="", help="只跑某一组：1/2/3/4/5")
    args = ap.parse_args()

    groups = {
        "1": ("JSON 契约", check_json_contract),
        "2": ("服务编排", check_service_orchestration),
        "3": ("前端守卫", check_frontend_guards),
        "4": ("接口层", lambda: check_api_layer(args.port)),
    }
    if args.mysql:
        groups["5"] = ("真实库", check_mysql_live)

    selected = [args.only] if args.only else list(groups)
    for key in selected:
        if key not in groups:
            print(f"未知分组：{key}")
            return 2
        groups[key][1]()

    print(f"\n{'=' * 60}")
    print(f"结果：{PASSED} PASS / {FAILED} FAIL")
    print("=" * 60)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
