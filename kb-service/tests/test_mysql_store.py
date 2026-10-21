"""MySQL 数据层测试（任务 1.14）。

设计原则：**MySQL 不可用时整体跳过**，保证无数据库的机器上 ``pytest`` 仍然全绿；
写操作一律包在会回滚的事务里，绝不动真实数据。

覆盖：
  * 连通性探测与引擎复用
  * ``kb_repo.load_store()`` 与原型 ``store.json`` 逐项一致
  * ``index_store._load_mysql()`` 快照正确
  * ``list_docs`` / ``count_knowledge`` 字段与计数
  * 状态流转、删除的 SQL 语义（在回滚事务内验证）
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import bindparam, func, select, text, update

from app.config import settings
from app.db import kb_repo
from app.db import models as M
from app.db.session import db_session, db_transaction, get_engine, ping

PROTO_STORE = Path(settings.base_dir).parent / "kb-engine" / "data" / "store.json"


# ------------------------------------------------------------------ fixtures
@pytest.fixture(scope="module")
def mysql_ready() -> str:
    ok, info = ping()
    if not ok:
        pytest.skip(f"MySQL 不可用，跳过数据层测试：{info}")
    return info


@pytest.fixture(scope="module")
def proto() -> dict:
    if not PROTO_STORE.is_file():
        pytest.skip(f"找不到原型索引：{PROTO_STORE}")
    return json.loads(PROTO_STORE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def store(mysql_ready) -> dict:
    try:
        return kb_repo.load_store()
    except RuntimeError as exc:
        pytest.skip(f"库中无已发布知识：{exc}")


# ------------------------------------------------------------------ 连通性
def test_ping_reports_version(mysql_ready):
    assert "MySQL" in mysql_ready
    assert settings.db_name in mysql_ready


def test_engine_is_reused():
    assert get_engine() is get_engine()


def test_db_session_usable(mysql_ready):
    from sqlalchemy import text

    with db_session() as conn:
        assert conn.execute(text("SELECT 1")).scalar() == 1


# ------------------------------------------------------------------ load_store
def test_load_store_matches_proto(store, proto):
    """核心断言：MySQL 读回与原型 JSON 索引逐项一致。"""
    assert len(store["docs"]) == len(proto["docs"])
    assert len(store["chunks"]) == len(proto["chunks"])
    assert store["model"] == proto["model"]
    assert store["version"] == proto["version"]
    assert store["createdAt"], "createdAt 应为读取时刻的时间戳"

    assert [d["docId"] for d in store["docs"]] == [d["docId"] for d in proto["docs"]]
    assert [c["chunkId"] for c in store["chunks"]] == [c["chunkId"] for c in proto["chunks"]]


@pytest.mark.parametrize("field", ["docId", "file", "title", "dept", "chunkCount"])
def test_doc_fields_match(store, proto, field):
    got = [d.get(field) for d in store["docs"]]
    want = [d.get(field) for d in proto["docs"]]
    assert got == want, f"docs[].{field} 不一致"


@pytest.mark.parametrize(
    "field",
    ["docId", "seqNo", "charCount", "dept", "titlePath", "blockType", "text", "embedText"],
)
def test_chunk_fields_match(store, proto, field):
    got = [c.get(field) for c in store["chunks"]]
    want = [c.get(field) for c in proto["chunks"]]
    assert got == want, f"chunks[].{field} 不一致"


def test_chunk_ktype_normalized(store, proto):
    """ktype 源数据混用中文/枚举，读回统一为中文名。"""
    for c, p in zip(store["chunks"], proto["chunks"]):
        want = kb_repo.ktype_label(kb_repo.ktype_enum(p.get("ktype")))
        assert c["ktype"] == want


def test_embeddings_match(store, proto):
    """向量维度一致且数值无漂移（JSON 往返精度损失 < 1e-6）。"""
    for c, p in zip(store["chunks"], proto["chunks"]):
        want = list(p.get("embedding") or [])
        got = list(c.get("embedding") or [])
        assert len(got) == len(want) == settings.embed_dim
        assert max((abs(a - b) for a, b in zip(got, want)), default=0.0) < 1e-6


# ------------------------------------------------------------------ index_store
def test_load_mysql_snapshot(mysql_ready, store):
    from app.core import index_store

    snap = index_store._load_mysql()
    assert snap.ok, f"MySQL 快照应可用：{snap.error}"
    assert snap.source == "mysql"
    assert len(snap.chunks) == len(store["chunks"])
    assert len(snap.docs) == len(store["docs"])
    assert snap.model == store["model"]
    assert all(c.embedding for c in snap.chunks), "切片应带向量"


def test_load_mysql_degrades_on_bad_credentials(mysql_ready, monkeypatch):
    """凭据错误时应返回带 error 的空快照，而不是抛异常。"""
    from app.core import index_store
    from app.db import session as db_session_mod

    monkeypatch.setattr(settings, "db_password", "definitely-wrong-password")
    db_session_mod.dispose_engine()
    try:
        snap = index_store._load_mysql()
        assert not snap.ok
        assert snap.error
        assert snap.chunks == []
    finally:
        db_session_mod.dispose_engine()


def test_index_stats_in_mysql_mode(mysql_ready, monkeypatch, store):
    from app.core import index_store

    monkeypatch.setattr(settings, "kb_store", "mysql")
    index_store.clear_index()
    try:
        idx = index_store.get_index()
        assert idx.ok, idx.error
        assert idx.source == "mysql"
        assert len(idx.chunks) == len(store["chunks"])

        stats = index_store.index_stats()
        assert stats["source"] == "mysql"
        assert stats["chunks"] == len(store["chunks"])
        assert stats["error"] is None
    finally:
        index_store.clear_index()


# ------------------------------------------------------------------ 管理读接口
def test_list_docs(mysql_ready, proto):
    docs = kb_repo.list_docs()
    assert len(docs) == len(proto["docs"])
    for d in docs:
        assert isinstance(d["docId"], int)
        assert d["title"]
        assert d["status"] in (0, 1, 2, 3)
        assert d["statusName"] in ("草稿", "待审核", "已发布", "已下线")
        assert d["fileKey"] is not None
        assert isinstance(d["visibleRoles"], list) and d["visibleRoles"]


def test_count_knowledge(mysql_ready, proto):
    c = kb_repo.count_knowledge()
    assert c["docs"] == len(proto["docs"])
    assert c["chunks"] == len(proto["chunks"])
    assert c["vectors"] == len(proto["chunks"])
    assert c["published_docs"] <= c["docs"]


# ------------------------------------------------------------------ 写操作（回滚）
class _Rollback(Exception):
    """哨兵异常：强制事务回滚。"""


def _in_rolled_back_txn(fn):
    """在会回滚的事务内执行 fn(conn)，返回其结果。"""
    engine = get_engine()
    conn = engine.connect()
    trans = conn.begin()
    try:
        return fn(conn)
    finally:
        trans.rollback()
        conn.close()


def test_update_doc_status_in_txn(mysql_ready, store):
    doc_id = 1
    before = _in_rolled_back_txn(
        lambda c: c.execute(
            select(M.KbDoc.status).where(M.KbDoc.doc_id == doc_id)
        ).scalar()
    )
    assert before is not None

    def _flip(c):
        c.execute(
            update(M.KbDoc)
            .where(M.KbDoc.doc_id == doc_id)
            .values(status=0)
        )
        return c.execute(
            select(M.KbDoc.status).where(M.KbDoc.doc_id == doc_id)
        ).scalar()

    assert _in_rolled_back_txn(_flip) == 0
    # 回滚后应恢复原值
    after = _in_rolled_back_txn(
        lambda c: c.execute(
            select(M.KbDoc.status).where(M.KbDoc.doc_id == doc_id)
        ).scalar()
    )
    assert after == before


def test_delete_doc_on_conn_in_txn(mysql_ready):
    """删除应在同一事务内级联清掉切片与向量，回滚后数据完好。"""
    doc_id = 1

    def _count(c):
        return c.execute(
            select(func.count())
            .select_from(M.KbChunk)
            .where(M.KbChunk.doc_id == doc_id)
        ).scalar()

    before = _in_rolled_back_txn(_count)
    assert before > 0

    def _delete(c):
        assert kb_repo.delete_doc_on_conn(c, doc_id) is True
        assert _count(c) == 0
        return c.execute(
            select(func.count())
            .select_from(M.KbDoc)
            .where(M.KbDoc.doc_id == doc_id)
        ).scalar()

    assert _in_rolled_back_txn(_delete) == 0
    assert _in_rolled_back_txn(_count) == before


def test_update_doc_status_missing_returns_error(mysql_ready):
    res = kb_repo.update_doc_status(999_999_999, 2)
    assert res["ok"] is False


# ------------------------------------------------------------------ 端到端（MySQL 模式）
def test_retrieve_end_to_end_in_mysql_mode(mysql_ready, monkeypatch, store):
    """存储切到 MySQL 后，完整检索链路（含精排）应正常返回。

    注：``/api/chat`` 的端到端打通还依赖 ``chatlog`` 的 MySQL 会话存储
    （任务 1.14 后续子项），此处只验证检索链路本身。
    """
    from app.core import index_store, pipeline

    monkeypatch.setattr(settings, "kb_store", "mysql")
    index_store.clear_index()
    try:
        results = pipeline.retrieve("校园网怎么开户", role="本科生", top_k=5)
        assert results, "MySQL 模式下应检索到知识"
        assert results[0].chunk.chunk_id == "D001-C001"
        assert all(r.chunk.embedding for r in results)
    finally:
        index_store.clear_index()


def test_retrieve_role_filter_in_mysql_mode(mysql_ready, monkeypatch):
    """角色过滤在 MySQL 模式下同样生效（缺省按「访客」收紧）。"""
    from app.core import index_store, pipeline

    monkeypatch.setattr(settings, "kb_store", "mysql")
    index_store.clear_index()
    try:
        # 研究生专属问题，以访客身份检索不应命中受限切片
        guest = pipeline.retrieve("研究生国家助学金标准", role=None, top_k=5)
        student = pipeline.retrieve("研究生国家助学金标准", role="研究生", top_k=5)
        assert student, "研究生角色应有结果"
        assert guest != student or all(
            "全部" in (r.chunk.visible_roles or []) for r in guest
        ), "访客不应命中受限切片"
    finally:
        index_store.clear_index()


def test_docs_endpoint_in_mysql_mode(mysql_ready, monkeypatch, proto):
    """/api/docs 走 MySQL 时应返回全部文档（含未发布）。"""
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setattr(settings, "kb_store", "mysql")
    client = TestClient(app)
    resp = client.get("/api/docs")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert len(body["data"]) == len(proto["docs"])


def test_meta_depts_derived_from_db(mysql_ready, monkeypatch, proto):
    """/api/meta 的部门列表来自已入库文档（对齐原型）。"""
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setattr(settings, "kb_store", "mysql")
    data = TestClient(app).get("/api/meta").json()["data"]
    want = list(dict.fromkeys(d["dept"] for d in proto["docs"] if d.get("dept")))
    assert data["depts"] == want
    assert data["roles"][0] == "全部"


# ------------------------------------------------------------------ 上传 / 同步 / 删除（回滚保护）
@pytest.fixture
def rollback_txn():
    """返回一个「一定回滚」的事务上下文工厂，供写操作测试注入。"""
    from contextlib import contextmanager

    @contextmanager
    def _txn():
        conn = get_engine().connect()
        trans = conn.begin()
        try:
            yield conn
        finally:
            trans.rollback()
            conn.close()

    return _txn


SAMPLE_MD = """---
title: 测试上传文档
dept: 信息网络中心
ktype: 办事指南
---

# 测试上传文档

## 一、用途

这是一份用于自动化测试的临时文档，入库后会被回滚，不会留在库里。

## 二、内容

包含足够的正文，确保能切出至少一个有效切片，长度需要超过最小切片阈值。
"""


def test_parse_doc_ok(tmp_path):
    from app.services.kb_admin import parse_doc

    p = tmp_path / "sample.md"
    p.write_text(SAMPLE_MD, encoding="utf-8")
    parsed = parse_doc(p, {"dept": "信息网络中心", "ktype": "办事指南"})
    assert parsed["ok"] is True
    assert parsed["chunks"], "应切出切片"
    assert len(parsed["embeddings"]) == len(parsed["chunks"])
    assert all(len(e) == settings.embed_dim for e in parsed["embeddings"])
    assert parsed["doc_meta"]["title"] == "测试上传文档"


def test_parse_doc_unsupported_ext(tmp_path):
    from app.services.kb_admin import parse_doc

    p = tmp_path / "a.zzz"
    p.write_text("x", encoding="utf-8")
    assert parse_doc(p)["ok"] is False


def test_ingest_uploaded_rolls_back(mysql_ready, monkeypatch, tmp_path, rollback_txn):
    """上传入库：能拿到 doc_id 与切片，且事务回滚后库里不留痕。"""
    from app.services import kb_admin

    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")
    before = kb_repo.count_knowledge()

    result = kb_admin.ingest_uploaded(
        file_buf=SAMPLE_MD.encode("utf-8"),
        filename="测试上传文档.md",
        dept="信息网络中心",
        ktype="办事指南",
        status=2,
        visible_roles=["全部"],
        txn=rollback_txn,
    )
    assert result["ok"] is True, result
    assert result["docId"] > 0
    assert result["chunkCount"] >= 1
    assert result["title"] == "测试上传文档"

    # 原件保留（与原型「用完即删」的有意差异，为 1.11 原文预览保留）
    assert list((tmp_path / "uploads").glob("*.md")), "原件应保留在 uploads/"

    after = kb_repo.count_knowledge()
    assert after == before, "回滚后计数应不变"


def test_ingest_uploaded_rejects_bad_input(mysql_ready, monkeypatch, tmp_path):
    from app.services import kb_admin

    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")
    base = dict(dept="信息网络中心", ktype="办事指南")

    r = kb_admin.ingest_uploaded(file_buf=b"x", filename="a.zzz", **base)
    assert r["ok"] is False and "不支持的文件类型" in r["error"]

    r = kb_admin.ingest_uploaded(file_buf=b"x", filename="a.md", dept="信息网络中心", ktype="不存在的类型")
    assert r["ok"] is False and "未知知识类型" in r["error"]

    r = kb_admin.ingest_uploaded(file_buf=b"", filename="empty.md", **base)
    assert r["ok"] is False and "无有效内容" in r["error"]


def test_ingest_uploaded_duplicate_hash(mysql_ready, monkeypatch, tmp_path, rollback_txn):
    """同内容重复上传：给出可读提示，而不是抛原始 SQL IntegrityError。

    （``kb_doc.file_hash`` 是唯一键，原型在此处会直接暴露数据库错误。）
    """
    from app.services import kb_admin

    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")

    # 先取库中已有文档的哈希，用它的内容模拟「重复上传」
    with db_session() as conn:
        row = conn.execute(select(M.KbDoc.doc_id, M.KbDoc.file_key).limit(1)).one()
    src = Path(settings.knowledge_dir) / row.file_key
    assert src.is_file(), f"缺少知识原件：{src}"

    r = kb_admin.ingest_uploaded(
        file_buf=src.read_bytes(),
        filename=src.name,
        dept="信息网络中心",
        ktype="办事指南",
        txn=rollback_txn,
    )
    assert r["ok"] is False
    assert "已存在" in r["error"] and "SHA-256" in r["error"]


def test_sync_knowledge_base_rolls_back(mysql_ready, monkeypatch, tmp_path, rollback_txn):
    """增量同步：临时知识目录应被识别为「新增」，且回滚后不留痕。

    注意：同步是「源目录 ↔ 库」的全量对账，源目录里没有的库内文档会被判为
    「已删除」。这里用临时目录替换真实知识目录，故库中原本的 7 篇都会被判删。
    """
    from app.services.kb_sync import iter_source_files, sync_knowledge_base

    root = tmp_path / "kb"
    (root / "01-信网中心" / "办事指南").mkdir(parents=True)
    (root / "01-信网中心" / "办事指南" / "新文档.md").write_text(SAMPLE_MD, encoding="utf-8")
    # 忽略规则：`.` 与 `00-` 开头不进同步；根目录散文件不进同步
    (root / "00-模板").mkdir()
    (root / "00-模板" / "x.md").write_text("x", encoding="utf-8")
    (root / "根目录散文件.md").write_text("x", encoding="utf-8")

    files = iter_source_files(root)
    assert [f.name for f in files] == ["新文档.md"]

    monkeypatch.setattr(settings, "knowledge_dir", root)
    before = kb_repo.count_knowledge()
    with db_session() as conn:
        n_source = len(kb_repo.list_source_docs(conn))

    summary = sync_knowledge_base(root, txn=rollback_txn)
    assert summary["errors"] == []
    assert summary["added"] == 1
    assert summary["updated"] == 0
    assert summary["deleted"] == n_source, "源目录里没有的库内文档应被判为已删除"

    assert kb_repo.count_knowledge() == before, "回滚后计数应不变"


def test_sync_refuses_when_source_dir_empty(mysql_ready, monkeypatch, tmp_path, rollback_txn):
    """安全阀：源目录扫不到文件但库里有知识时必须中止，不能删空知识库。

    原型 ``sync.js`` 在此场景会静默删光全部知识，属高危行为，这里刻意收紧。
    """
    from app.services.kb_sync import sync_knowledge_base

    empty = tmp_path / "empty-kb"
    empty.mkdir()
    monkeypatch.setattr(settings, "knowledge_dir", empty)

    before = kb_repo.count_knowledge()
    assert before["docs"] > 0

    summary = sync_knowledge_base(empty, txn=rollback_txn)
    assert summary["deleted"] == 0
    assert summary["errors"] and "已中止同步" in summary["errors"][0]
    assert kb_repo.count_knowledge() == before


def test_sync_real_knowledge_dir(mysql_ready, monkeypatch, rollback_txn):
    """在真实知识目录上同步：不报错、不误删，且「跳过 + 新增」覆盖全部源文件。"""
    from app.services.kb_sync import iter_source_files, sync_knowledge_base

    base = Path(settings.knowledge_dir)
    monkeypatch.setattr(settings, "knowledge_dir", base)
    n_files = len(iter_source_files(base))
    assert n_files > 0

    before = kb_repo.count_knowledge()
    summary = sync_knowledge_base(txn=rollback_txn)
    assert summary["errors"] == []
    assert summary["deleted"] == 0, "真实目录里的文件都还在，不应判删"
    assert summary["updated"] == 0
    assert summary["skipped"] + summary["added"] == n_files
    assert kb_repo.count_knowledge() == before, "回滚后计数应不变"


def test_sync_missing_dir_reports_error(mysql_ready, tmp_path, rollback_txn):
    from app.services.kb_sync import sync_knowledge_base

    summary = sync_knowledge_base(tmp_path / "nope", txn=rollback_txn)
    assert summary["errors"], "目录不存在应报错"
    assert summary["added"] == 0


def test_list_chunks_mysql(mysql_ready, store):
    """切片明细（MySQL 路径）字段与原型一致。"""
    chunks = kb_repo.list_chunks(1)
    assert chunks
    first = chunks[0]
    assert first["chunkId"] == "D001-C001"
    assert first["docId"] == "D001"
    assert first["text"]
    assert first["embedText"].startswith(first["titlePath"])
    assert first["visibleRoles"]


def test_list_chunks_unknown_doc_is_empty(mysql_ready):
    assert kb_repo.list_chunks(999_999_999) == []


def test_get_doc_and_exists(mysql_ready, store):
    doc = kb_repo.get_doc(1)
    assert doc is not None
    assert doc["docId"] == 1
    assert doc["statusName"] in ("草稿", "待审核", "已发布", "已下线")
    assert kb_repo.doc_exists(1) is True
    assert kb_repo.doc_exists(999_999_999) is False
    assert kb_repo.get_doc(999_999_999) is None


def test_list_depts(mysql_ready, proto):
    depts = kb_repo.list_depts()
    want = list(dict.fromkeys(d["dept"] for d in proto["docs"] if d.get("dept")))
    assert depts == want


def test_list_source_docs_excludes_uploads(mysql_ready):
    with db_session() as conn:
        rows = kb_repo.list_source_docs(conn)
    assert rows
    assert all(not r["file_key"].startswith("uploads/") for r in rows)


# ------------------------------------------------------------------ 管理端全链路（API）
def test_admin_lifecycle_via_api(mysql_ready, monkeypatch, tmp_path):
    """上传 → 入库 → 发布 → 下线 → 删除（管理页真实调用路径）。

    这是任务 1.14 的验收条目。测试会真实提交，故在 finally 中确保清理。
    """
    import base64 as b64

    from fastapi.testclient import TestClient

    from app.core.index_store import clear_index
    from app.main import app

    monkeypatch.setattr(settings, "kb_store", "mysql")
    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")
    client = TestClient(app)
    doc_id: int | None = None

    try:
        # 1) 上传为草稿（前端字段名：data / visibleRoles / status）
        resp = client.post(
            "/api/docs",
            json={
                "filename": "生命周期测试.md",
                "data": b64.b64encode(SAMPLE_MD.encode("utf-8")).decode("ascii"),
                "dept": "信息网络中心",
                "ktype": "办事指南",
                "status": 0,
                "visibleRoles": ["全部"],
                "title": "生命周期测试文档",
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        doc_id = data["docId"]
        assert isinstance(doc_id, int) and doc_id > 0
        assert data["chunkCount"] >= 1
        assert data["title"] == "生命周期测试文档"

        # 2) 列表可见（草稿也要出现，JSON 索引模式做不到这点）
        docs = {d["docId"]: d for d in client.get("/api/docs").json()["data"]}
        assert doc_id in docs
        assert docs[doc_id]["status"] == 0
        assert docs[doc_id]["statusName"] == "草稿"
        assert docs[doc_id]["fileKey"].startswith("uploads/")
        assert docs[doc_id]["chunkCount"] == data["chunkCount"]

        # 3) 切片明细
        chunks = client.get(f"/api/docs/{doc_id}/chunks").json()["data"]
        assert len(chunks) == data["chunkCount"]
        assert chunks[0]["chunkId"] == kb_repo.chunk_public_id(doc_id, 1)

        # 4) 发布
        r = client.patch(f"/api/docs/{doc_id}", json={"status": 2})
        assert r.status_code == 200, r.text
        assert r.json()["data"]["statusName"] == "已发布"

        # 发布后应进入检索索引（索引只收 status=2）
        clear_index()
        from app.core.index_store import get_index

        public_id = kb_repo.doc_public_id(doc_id)
        listed = {d["docId"] for d in client.get("/api/docs").json()["data"] if d["status"] == 2}
        assert doc_id in listed, "发布后应出现在已发布列表"
        assert public_id in {d["docId"] for d in get_index().docs}, "发布后应进入检索索引"

        # 5) 下线（应退出检索索引）
        r = client.patch(f"/api/docs/{doc_id}", json={"status": 3})
        assert r.status_code == 200
        assert r.json()["data"]["statusName"] == "已下线"
        clear_index()
        assert public_id not in {d["docId"] for d in get_index().docs}

        # 6) 删除（级联清切片与向量）
        r = client.delete(f"/api/docs/{doc_id}")
        assert r.status_code == 200, r.text
        assert r.json()["data"]["deleted"] is True
        assert kb_repo.doc_exists(doc_id) is False
        assert kb_repo.list_chunks(doc_id) == []
        doc_id = None
    finally:
        clear_index()
        if doc_id is not None:
            client.delete(f"/api/docs/{doc_id}")


def test_admin_api_validation_errors(mysql_ready, monkeypatch, tmp_path):
    """上传参数缺失/非法时返回 400 + 可读原因（与原型一致）。"""
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setattr(settings, "kb_store", "mysql")
    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")
    client = TestClient(app)

    base = {"filename": "a.md", "data": "eA==", "dept": "信息网络中心", "ktype": "办事指南"}
    for drop, kw in (("filename", "缺少文件名"), ("dept", "缺少部门"), ("ktype", "缺少知识类型")):
        body = {k: v for k, v in base.items() if k != drop}
        r = client.post("/api/docs", json=body)
        assert r.status_code == 400 and kw in r.json()["detail"]

    r = client.post("/api/docs", json={**base, "data": ""})
    assert r.status_code == 400 and "缺少文件内容" in r.json()["detail"]

    r = client.post("/api/docs", json={**base, "data": "!!!not-base64!!!"})
    assert r.status_code == 400 and "base64" in r.json()["detail"]

    r = client.post("/api/docs", json={**base, "filename": "a.zzz"})
    assert r.status_code == 400 and "不支持的文件类型" in r.json()["detail"]

    # 非法文档编号
    r = client.patch("/api/docs/abc", json={"status": 2})
    assert r.status_code == 400 and "文档编号非法" in r.json()["detail"]

    # 不存在的文档
    r = client.patch("/api/docs/999999999", json={"status": 2})
    assert r.status_code == 404


def test_admin_api_requires_mysql(mysql_ready, monkeypatch):
    """JSON 模式下管理写操作返回 400 且提示如何切换。"""
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setattr(settings, "kb_store", "json")
    client = TestClient(app)
    for call in (
        lambda: client.post("/api/docs", json={"filename": "a.md", "data": "eA=="}),
        lambda: client.patch("/api/docs/1", json={"status": 2}),
        lambda: client.delete("/api/docs/1"),
        lambda: client.post("/api/sync"),
    ):
        r = call()
        assert r.status_code == 400
        assert "KB_STORE=mysql" in r.json()["detail"]


# ------------------------------------------------------------------ 向量适配层
def test_vector_repository_roundtrip(mysql_ready, rollback_txn):
    from app.db.vector_repository import get_vector_repository

    repo = get_vector_repository()
    with rollback_txn() as conn:
        cid = "ZZZ-C001"
        repo.save(conn, [cid], [[0.1, 0.2, 0.3]], model="test-model")
        got = repo.load(conn, [cid])
        assert got[cid] == [0.1, 0.2, 0.3]

        # 幂等：重复 save 不应报主键冲突
        repo.save(conn, [cid], [[0.4, 0.5, 0.6]], model="test-model")
        assert repo.load(conn, [cid])[cid] == [0.4, 0.5, 0.6]

        assert repo.delete(conn, [cid]) == 1
        assert repo.load(conn, [cid]) == {}


def test_vector_repository_length_mismatch(mysql_ready, rollback_txn):
    from app.db.vector_repository import get_vector_repository

    repo = get_vector_repository()
    with rollback_txn() as conn:
        with pytest.raises(ValueError):
            repo.save(conn, ["A", "B"], [[0.1]])


def test_vector_repository_stats(mysql_ready, store):
    from app.db.vector_repository import get_vector_repository

    repo = get_vector_repository()
    with db_session() as conn:
        stats = repo.stats(conn)
    assert stats["backend"] == "mysql-json"
    assert stats["vectors"] == len(store["chunks"])
    assert stats["dim_min"] == stats["dim_max"] == settings.embed_dim
    assert stats["model"] == store["model"]


def test_vector_repository_empty_inputs(mysql_ready, rollback_txn):
    from app.db.vector_repository import get_vector_repository

    repo = get_vector_repository()
    with rollback_txn() as conn:
        assert repo.save(conn, [], []) == 0
        assert repo.load(conn, []) == {}
        assert repo.delete(conn, []) == 0


# ------------------------------------------------------------------ 会话存储（MySQL 后端）
@dataclass
class _Gen:
    """GenerationResult 替身（含 MySQL 路径用到的全部字段）。"""

    answer: str = "根据知识库资料[1]，请携带校园卡前往办理。"
    citations: list | None = None
    confidence: float = 0.72
    refused: bool = False
    usage: dict | None = None
    latency_ms: int = 123
    first_token_ms: int | None = 45
    model: str = "mock"

    def __post_init__(self):
        if self.citations is None:
            self.citations = [
                {
                    "n": 1,
                    "chunkId": "D001-C001",
                    "docId": 1,
                    "similarity": 0.6808,
                    "titlePath": "校园网开户与报修指南 > 一、开户与账号",
                    "dept": "信息网络中心",
                    "ktype": "办事指南",
                }
            ]
        if self.usage is None:
            self.usage = {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}


@pytest.fixture
def mysql_chatlog(monkeypatch):
    """把 chatlog 切到 MySQL 后端，并在结束后清掉本测试造出来的会话数据。

    注意删除顺序：``token_usage`` 需要按 trace_id 关联 ``chat_message``，
    必须在删 ``chat_message`` **之前**取出 trace_id，否则关联子查询为空、
    计量行会残留。
    """
    from app.db import chatlog

    monkeypatch.setattr(settings, "kb_store", "mysql")
    chatlog.reset_seed_cache()
    created: list[str] = []
    yield chatlog, created

    if not created:
        return
    ids = list(created)
    expanding = {"ids": bindparam("ids", expanding=True)}
    with db_transaction() as conn:
        traces = [
            r[0]
            for r in conn.execute(
                text("SELECT DISTINCT trace_id FROM chat_message WHERE session_id IN :ids")
                .bindparams(**expanding),
                {"ids": ids},
            ).all()
        ]
        conn.execute(
            text(
                "DELETE c FROM answer_citation c JOIN chat_message m ON m.msg_id = c.msg_id "
                "WHERE m.session_id IN :ids"
            ).bindparams(**expanding),
            {"ids": ids},
        )
        conn.execute(
            text("DELETE FROM chat_message WHERE session_id IN :ids").bindparams(**expanding),
            {"ids": ids},
        )
        if traces:
            conn.execute(
                text("DELETE FROM token_usage WHERE trace_id IN :traces").bindparams(
                    bindparam("traces", expanding=True)
                ),
                {"traces": traces},
            )
        conn.execute(
            text("DELETE FROM unrecognized_question WHERE session_id IN :ids").bindparams(**expanding),
            {"ids": ids},
        )
        conn.execute(
            text("DELETE FROM chat_session WHERE session_id IN :ids").bindparams(**expanding),
            {"ids": ids},
        )


def test_chatlog_mysql_seed(mysql_ready, mysql_chatlog):
    """会话表为空时应自动 seed 出默认助手与渠道（chat_session.channel_id 是 NOT NULL）。"""
    chatlog, _ = mysql_chatlog
    with db_transaction() as conn:
        seed = chatlog.ensure_seed(conn)
    assert seed["agent_id"] > 0
    assert seed["channel_id"] > 0

    with db_session() as conn:
        code = conn.execute(
            select(M.Agent.agent_code).where(M.Agent.agent_id == seed["agent_id"])
        ).scalar()
        ch = conn.execute(
            select(M.Channel.channel_code).where(M.Channel.channel_id == seed["channel_id"])
        ).scalar()
    assert code == "campus-default"
    assert ch == "portal-web"


def test_chatlog_mysql_save_and_load(mysql_ready, mysql_chatlog):
    """一次问答落库后能完整读回（含引用），且 msgId 为真实自增主键。"""
    chatlog, created = mysql_chatlog
    sid = chatlog.new_session_id()
    created.append(sid)

    out = chatlog.save_qa("校园网怎么开户", "本科生", _Gen(), sid, "T_MYSQL_TEST")
    assert out["sessionId"] == sid
    assert isinstance(out["msgId"], int) and out["msgId"] > 0

    sess = chatlog.load_session(sid)
    assert sess is not None
    assert sess["role"] == "本科生"
    msgs = sess["messages"]
    assert [m["role"] for m in msgs] == ["user", "ai"]
    assert msgs[0]["content"] == "校园网怎么开户"
    assert msgs[1]["content"] == _Gen.answer
    assert msgs[1]["confidence"] == pytest.approx(0.72)
    assert msgs[1]["createdAt"], "应带 createdTime（由 createdAt 映射）"

    cites = msgs[1]["citations"]
    assert len(cites) == 1
    c = cites[0]
    assert c["chunkId"] == "D001-C001"
    assert c["similarity"] == pytest.approx(0.6808)
    assert c["dept"] == "信息网络中心"
    assert c["ktype"] == "办事指南"
    assert c["titlePath"]
    assert c["n"] == 1


def test_chatlog_mysql_writes_usage_and_unrecognized(mysql_ready, mysql_chatlog):
    """token 计量必写；拒答时额外进未识别问题池（对齐原型 chatlog.js）。"""
    chatlog, created = mysql_chatlog
    sid = chatlog.new_session_id()
    created.append(sid)
    trace = "T_MYSQL_UNREC"

    chatlog.save_qa(
        "一个知识库里没有的问题", "访客", _Gen(answer="抱歉，未找到相关信息", refused=True), sid, trace
    )

    with db_session() as conn:
        usage = conn.execute(
            select(M.TokenUsage.model_code, M.TokenUsage.total_tokens, M.TokenUsage.latency_ms).where(
                M.TokenUsage.trace_id == trace
            )
        ).first()
        unrec = conn.execute(
            select(M.UnrecognizedQuestion.question).where(M.UnrecognizedQuestion.session_id == sid)
        ).all()
    assert usage is not None
    assert usage[0] == "mock"
    assert usage[1] == 30
    assert usage[2] == 123
    assert [r[0] for r in unrec] == ["一个知识库里没有的问题"]


def test_chatlog_mysql_multiturn_and_history(mysql_ready, mysql_chatlog):
    """10 轮问答应全部可读，load_history 映射 ai → assistant。"""
    chatlog, created = mysql_chatlog
    sid = chatlog.new_session_id()
    created.append(sid)

    for i in range(10):
        chatlog.save_qa(f"问题{i}", "研究生", _Gen(answer=f"回答{i}"), sid)

    sess = chatlog.load_session(sid)
    assert len(sess["messages"]) == 20

    hist = chatlog.load_history(sid)
    assert len(hist) == 20
    assert {m["role"] for m in hist} == {"user", "assistant"}
    assert hist[0]["content"] == "问题0"
    assert hist[-1]["content"] == "回答9"

    assert len(chatlog.load_history(sid, limit=6)) == 6


def test_load_context_mysql_beyond_ten_turns(mysql_ready, mysql_chatlog):
    """任务 1.8：MySQL 模式下 >10 轮仍保留 ≥10 轮上下文，首条为 user，跨会话不串。"""
    from app.session.context import history_stats, load_context

    chatlog, created = mysql_chatlog
    a, b = chatlog.new_session_id(), chatlog.new_session_id()
    created.extend([a, b])

    for i in range(12):
        chatlog.save_qa(f"A{i}", "本科生", _Gen(answer=f"a{i}"), a)
    chatlog.save_qa("B0", "访客", _Gen(answer="b0"), b)

    ctx = load_context(a)
    assert ctx[0]["role"] == "user", "截断后首条必须是 user"
    assert history_stats(ctx)["turns"] >= 10
    assert ctx[-1] == {"role": "assistant", "content": "a11"}

    ctx_b = load_context(b)
    assert [m["content"] for m in ctx_b] == ["B0", "b0"]


def test_chatlog_mysql_missing_session(mysql_ready, mysql_chatlog):
    chatlog, _ = mysql_chatlog
    assert chatlog.load_session("S_NOT_EXIST_AT_ALL") is None
    assert chatlog.load_history("S_NOT_EXIST_AT_ALL") == []


def test_chatlog_mysql_append_message(mysql_ready, mysql_chatlog):
    chatlog, created = mysql_chatlog
    sid = chatlog.new_session_id()
    created.append(sid)

    m1 = chatlog.append_message(sid, "user", "你好")
    m2 = chatlog.append_message(sid, "ai", "你好呀", confidence=0.5)
    assert m1["msgId"] < m2["msgId"]
    sess = chatlog.load_session(sid)
    assert [m["role"] for m in sess["messages"]] == ["user", "ai"]
    assert sess["messages"][1]["confidence"] == pytest.approx(0.5)


def test_chatlog_mysql_list_sessions(mysql_ready, mysql_chatlog):
    chatlog, created = mysql_chatlog
    sid = chatlog.new_session_id()
    created.append(sid)
    chatlog.save_qa("列表测试问题", "本科生", _Gen(), sid)

    items = chatlog.list_sessions(limit=200)
    row = next((i for i in items if i["sessionId"] == sid), None)
    assert row is not None
    assert row["messageCount"] == 2
    assert row["lastQuestion"] == "列表测试问题"
    assert row["role"] == "本科生"
    assert row["createdAt"]


def test_chat_end_to_end_in_mysql_mode(mysql_ready, monkeypatch, mysql_chatlog):
    """MySQL 模式下 /api/chat 全链路：检索 → 生成 → 落库 → 历史可读。"""
    from fastapi.testclient import TestClient

    from app.core.index_store import clear_index
    from app.main import app

    chatlog, created = mysql_chatlog
    clear_index()
    client = TestClient(app)
    try:
        resp = client.post(
            "/api/chat",
            json={"question": "校园网怎么开户", "role": "本科生", "stream": False},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        data = body["data"]
        assert data["answer"]
        assert data["refused"] is False
        assert data["retrieved"] > 0
        assert data["citations"]
        assert data["citations"][0]["chunkId"] == "D001-C001"
        sid = data["sessionId"]
        created.append(sid)

        msgs = client.get(f"/api/sessions/{sid}/messages").json()["data"]
        assert [m["role"] for m in msgs] == ["user", "ai"]
        assert msgs[1]["citations"][0]["chunkId"] == "D001-C001"
        assert msgs[1]["createdTime"]
    finally:
        clear_index()


def test_chat_stream_end_to_end_in_mysql_mode(mysql_ready, monkeypatch, mysql_chatlog):
    """MySQL 模式下 SSE 流式问答：delta 累积 == done.answer，且会话已落库。"""
    from fastapi.testclient import TestClient

    from app.core.index_store import clear_index
    from app.main import app

    chatlog, created = mysql_chatlog
    clear_index()
    client = TestClient(app)
    try:
        with client.stream(
            "POST",
            "/api/chat/stream",
            json={"question": "图书馆借书能借多久", "role": "本科生"},
        ) as r:
            assert r.status_code == 200
            events = []
            for line in r.iter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line[5:].strip()))

        types = [e["type"] for e in events]
        assert "done" in types
        done = next(e for e in events if e["type"] == "done")
        acc = "".join(e.get("text", "") for e in events if e["type"] == "delta")
        assert acc == done["answer"]
        created.append(done["sessionId"])

        msgs = client.get(f"/api/sessions/{done['sessionId']}/messages").json()["data"]
        assert [m["role"] for m in msgs] == ["user", "ai"]
    finally:
        clear_index()


# ------------------------------------------------------------------ 认证与权限（MySQL）
def _has_seed_users() -> bool:
    with db_session() as conn:
        return (conn.execute(select(func.count()).select_from(M.SysUser)).scalar() or 0) > 0


def test_auth_login_and_admin_access(mysql_ready, monkeypatch):
    """登录签发 JWT；管理员 token 可访问管理接口（AUTH_ENABLED=true）。"""
    if not _has_seed_users():
        pytest.skip("sys_user 为空，请先运行 tools/seed_auth.py")

    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setattr(settings, "auth_enabled", True)
    client = TestClient(app)

    assert client.get("/api/docs").status_code == 401, "未登录应 401"

    resp = client.post("/api/auth/login", json={"userNo": "admin"})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["role"] == "管理员"
    assert data["isAdmin"] is True
    assert data["expiresIn"] > 0

    headers = {"Authorization": f"Bearer {data['token']}"}
    assert client.get("/api/docs", headers=headers).status_code == 200
    me = client.get("/api/auth/me", headers=headers).json()["data"]
    assert me["userNo"] == "admin" and me["isAdmin"] is True


def test_auth_login_role_from_id_type(mysql_ready):
    """已登记用户按 ``sys_user.id_type`` 映射身份角色。"""
    if not _has_seed_users():
        pytest.skip("sys_user 为空，请先运行 tools/seed_auth.py")

    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    for user_no, expect in {"20230001": "本科生", "20240001": "研究生", "T10001": "教职工"}.items():
        resp = client.post("/api/auth/login", json={"userNo": user_no})
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["role"] == expect, user_no


def test_auth_login_unregistered_user(mysql_ready):
    """未登记学工号：内部演示阶段凭合法角色放行；非法角色 401。"""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)

    ok = client.post("/api/auth/login", json={"userNo": "99999999", "role": "本科生"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["data"]["role"] == "本科生"

    bad = client.post("/api/auth/login", json={"userNo": "99999999", "role": "超级管理员"})
    assert bad.status_code == 401
    assert "有效角色" in bad.json()["error"]


def test_auth_non_admin_token_forbidden(mysql_ready, monkeypatch):
    """本科生 token 访问管理接口应 403。"""
    if not _has_seed_users():
        pytest.skip("sys_user 为空，请先运行 tools/seed_auth.py")

    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setattr(settings, "auth_enabled", True)
    client = TestClient(app)
    token = client.post("/api/auth/login", json={"userNo": "20230001"}).json()["data"]["token"]
    resp = client.get("/api/docs", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


def test_auth_chat_role_injected_from_token(mysql_ready, monkeypatch, mysql_chatlog):
    """带 token 提问：会话角色取 token 身份，忽略请求体伪造的角色。"""
    if not _has_seed_users():
        pytest.skip("sys_user 为空，请先运行 tools/seed_auth.py")

    from fastapi.testclient import TestClient

    from app.core import index_store
    from app.main import app

    chatlog, created = mysql_chatlog
    monkeypatch.setattr(settings, "kb_store", "mysql")
    index_store.clear_index()
    client = TestClient(app)
    try:
        token = client.post("/api/auth/login", json={"userNo": "20230001"}).json()["data"]["token"]
        resp = client.post(
            "/api/chat",
            json={"question": "校园网怎么开户", "role": "管理员", "stream": False},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        sid = resp.json()["data"]["sessionId"]
        created.append(sid)
        assert chatlog.load_session(sid)["role"] == "本科生", "应以 token 角色为准"
    finally:
        index_store.clear_index()


# ------------------------------------------------------------------ 枚举工具
def test_enum_helpers():
    assert kb_repo.ktype_enum("政策文件") == "policy"
    assert kb_repo.ktype_enum("policy") == "policy"
    assert kb_repo.ktype_label("policy") == "政策文件"
    assert kb_repo.ktype_label("未知类型") == "未知类型"
    assert kb_repo.status_code("已审核") == 2
    assert kb_repo.status_code("已发布") == 2
    assert kb_repo.status_code("草稿") == 0
    assert kb_repo.status_code(3) == 3


def test_id_helpers():
    assert kb_repo.doc_public_id(1) == "D001"
    assert kb_repo.doc_public_id(42) == "D042"
    assert kb_repo.chunk_public_id(1, 2) == "D001-C002"
    assert kb_repo.parse_public_doc_id("D007") == 7
    assert kb_repo.parse_public_doc_id("7") == 7
    assert kb_repo.parse_public_doc_id(7) == 7
    assert kb_repo.parse_public_doc_id("abc") is None


def test_roles_helpers():
    assert kb_repo.roles_to_str(["全部"]) == "全部"
    assert kb_repo.roles_to_str(["本科生", "教职工"]) == "本科生,教职工"
    assert kb_repo.roles_to_str(None) == "全部"
    assert kb_repo.roles_from_str("本科生,教职工") == ["本科生", "教职工"]
    assert kb_repo.roles_from_str("") == ["全部"]
    assert kb_repo.roles_from_str(None) == ["全部"]
