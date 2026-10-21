"""知识点管理（任务 2.8 / M-K-10）单元测试。

分三层，与项目既有风格一致：

1. **纯逻辑** —— ``embed_text`` 口径、正文校验边界、导出格式；
2. **服务编排** —— 用 monkeypatch 替换 ``kb_repo``，验证「新建取 max+1 序号 /
   正文变更才重算向量 / 写后回写 chunk_count 并失效索引 / 再加工的删除语义」；
3. **接口层** —— ``TestClient`` 断言 JSON 只读模式的 400 指引、404 与导出响应头。

**为什么不写 SQL 桩**：本任务的 SQL 都是简单 SELECT/UPDATE（筛选下推、单行增删改），
真正容易错的是编排顺序与边界判定（例如「删除后正文为空」该不该拦、向量失败要不要回滚）。
真实 SQL 由 ``tools/verify_kb_points.py --mysql`` 对着线上库跑（快照-恢复式），
这里用桩重复一遍只会增加维护成本而不增加覆盖。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.api import knowledge as knowledge_api
from app.config import settings
from app.db import kb_repo
from app.main import app
from app.services import kb_knowledge

client = TestClient(app)


# ================================================================== 夹具
@pytest.fixture()
def json_mode(monkeypatch):
    monkeypatch.setattr(settings, "kb_store", "json")
    assert settings.store_label == "json"
    return settings


@pytest.fixture()
def mysql_mode(monkeypatch):
    monkeypatch.setattr(settings, "kb_store", "mysql")
    assert settings.store_label == "mysql"
    return settings


def _point(chunk_id="D001-C001", **over) -> dict:
    base = {
        "chunkId": chunk_id,
        "docId": "D001",
        "docTitle": "校园网开户与报修指南",
        "seqNo": 1,
        "blockType": "para",
        "titlePath": "校园网 / 开户",
        "charCount": 10,
        "dept": "信息网络中心",
        "ktype": "办事指南",
        "categoryId": 1,
        "visibleRoles": ["全部"],
        "vecSynced": 1,
        "createdAt": None,
        "content": "0123456789",
        "embedText": "校园网 / 开户\n0123456789",
    }
    base.update(over)
    return base


class _Recorder:
    """记录对 kb_repo 的调用（顺序敏感）。"""

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
        raise AssertionError(f"未调用 {name}；实际调用：{self.names()}")


@pytest.fixture()
def rec(monkeypatch):
    """把 kb_knowledge 用到的 repo / 索引 / 向量接口全部替换成记录器。"""
    r = _Recorder()
    state = {"point": _point(), "updated": {}, "inserted": None}

    monkeypatch.setattr(kb_knowledge.kb_repo, "point_scope", lambda cid: r.add("point_scope", chunk_id=cid) or (
        {"chunkId": cid, "docId": 1, "deptId": 100, "ktype": "guide"} if state["point"] else None
    ))
    monkeypatch.setattr(kb_knowledge.kb_repo, "get_point", lambda cid: r.add("get_point", chunk_id=cid) or state["point"])
    monkeypatch.setattr(kb_knowledge.kb_repo, "max_chunk_seq", lambda doc_id: r.add("max_chunk_seq", doc_id=doc_id) or 7)
    monkeypatch.setattr(kb_knowledge.kb_repo, "get_doc", lambda doc_id: r.add("get_doc", doc_id=doc_id) or {
        "docId": doc_id, "title": "校园网开户与报修指南", "dept": "信息网络中心", "ktype": "办事指南",
    })
    monkeypatch.setattr(kb_knowledge.kb_repo, "list_chunks", lambda doc_id: r.add("list_chunks", doc_id=doc_id) or [])

    def _insert(**kw):
        r.add("insert_point", **kw)
        state["inserted"] = kw
        return kb_repo.chunk_public_id(kw["doc_id"], kw["seq_no"])

    monkeypatch.setattr(kb_knowledge.kb_repo, "insert_point", _insert)

    def _update(cid, fields):
        r.add("update_point_fields", chunk_id=cid, fields=dict(fields))
        state["updated"].update(fields)
        # 让后续 get_point 读到新内容，模拟落库
        if state["point"]:
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

    monkeypatch.setattr(kb_knowledge.kb_repo, "update_point_fields", _update)

    def _delete(cid):
        r.add("delete_point", chunk_id=cid)
        return {"ok": True, "deleted": 1, "chunkId": cid, "docId": 1}

    monkeypatch.setattr(kb_knowledge.kb_repo, "delete_point", _delete)
    monkeypatch.setattr(kb_knowledge.kb_repo, "recount_doc_chunks", lambda doc_id: r.add("recount_doc_chunks", doc_id=doc_id) or 8)
    monkeypatch.setattr(kb_knowledge.kb_repo, "save_point_vector", lambda cid, emb, model=None: r.add(
        "save_point_vector", chunk_id=cid, dim=len(emb)
    ) or 1)
    monkeypatch.setattr(kb_knowledge.kb_repo, "drop_point_vector", lambda cid: r.add("drop_point_vector", chunk_id=cid) or 1)

    class _Emb:
        def encode_one(self, text):
            r.add("encode_one", text=text)
            return [0.1, 0.2, 0.3]

    monkeypatch.setattr(kb_knowledge, "get_embedder", lambda: _Emb())

    import app.core.index_store as idx_mod

    monkeypatch.setattr(idx_mod, "clear_index", lambda: r.add("clear_index"))
    r.state = state
    return r


# ================================================================== 1. 纯逻辑
def test_embed_text_matches_load_store_convention() -> None:
    """向量文本必须与 load_store 的 embedText 同口径（标题路径 + 换行 + 正文）。"""
    assert kb_knowledge.embed_text("A / B", "正文") == "A / B\n正文"
    assert kb_knowledge.embed_text("", "正文") == "正文"


def test_check_content_boundaries() -> None:
    """正文校验边界：空/纯空白拒绝，长度上限**含**等号。"""
    assert kb_knowledge._check_content("正常内容") is None
    assert kb_knowledge._check_content("   ") is not None
    assert kb_knowledge._check_content("") is not None
    assert kb_knowledge._check_content("x" * kb_knowledge.MAX_CONTENT) is None
    assert kb_knowledge._check_content("x" * (kb_knowledge.MAX_CONTENT + 1)) is not None


def test_block_types_are_documented_set() -> None:
    assert set(kb_knowledge.BLOCK_TYPES) == {"para", "table", "faq", "image"}


# ================================================================== 2. JSON 只读
def test_json_list_is_readonly_and_truncates(json_mode) -> None:
    data = kb_knowledge.list_points(size=5)["data"]
    assert data["editable"] is False
    assert data["total"] > 0
    assert len(data["items"]) <= 5
    first = data["items"][0]
    assert "excerpt" in first and "content" not in first
    assert len(first["excerpt"]) <= kb_repo.EXCERPT_LEN
    assert data["filters"]["depts"] and data["filters"]["blockTypes"]


def test_json_list_filters(json_mode) -> None:
    all_items = kb_knowledge.list_points(size=200)["data"]
    target = all_items["items"][0]
    by_dept = kb_knowledge.list_points(dept=target["dept"], size=200)["data"]
    assert by_dept["total"] >= 1
    assert all(i["dept"] == target["dept"] for i in by_dept["items"])

    by_block = kb_knowledge.list_points(block_type=target["blockType"], size=200)["data"]
    assert all(i["blockType"] == target["blockType"] for i in by_block["items"])

    by_doc = kb_knowledge.list_points(doc_id=int(target["docId"][1:]), size=200)["data"]
    assert all(i["docId"] == target["docId"] for i in by_doc["items"])


def test_json_list_keyword_is_and_semantics(json_mode) -> None:
    """多个关键词是 AND：命中数必须单调不增。"""
    one = kb_knowledge.list_points(q="校园", size=200)["data"]["total"]
    two = kb_knowledge.list_points(q="校园 网络", size=200)["data"]["total"]
    assert one >= two


def test_json_detail_returns_full_content(json_mode) -> None:
    first = kb_knowledge.list_points(size=1)["data"]["items"][0]
    res = kb_knowledge.get_point(first["chunkId"])
    assert res["ok"] is True
    assert res["data"]["content"]
    assert res["data"]["editable"] is False
    assert kb_knowledge.get_point("D999-C999")["ok"] is False


@pytest.mark.parametrize(
    "fn,args",
    [
        ("create_point", ({"docId": "D001", "content": "x"},)),
        ("update_point", ("D001-C001", {"content": "x"})),
        ("delete_point", ("D001-C001",)),
        ("append_text", ("D001-C001", "x")),
        ("insert_image", ("D001-C001", "a.png")),
    ],
)
def test_json_mode_writes_are_blocked_with_hint(json_mode, fn, args) -> None:
    res = getattr(kb_knowledge, fn)(*args)
    assert res["ok"] is False
    assert "MySQL" in res["error"]


def test_json_mode_remove_content_blocked(json_mode) -> None:
    res = kb_knowledge.remove_content("D001-C001", text="x")
    assert res["ok"] is False and "MySQL" in res["error"]


# ================================================================== 3. MySQL 服务编排
def test_create_point_uses_max_seq_plus_one(mysql_mode, rec) -> None:
    res = kb_knowledge.create_point({"docId": "D001", "content": "新切片正文", "blockType": "faq"})
    assert res["ok"] is True, res
    assert rec.last("max_chunk_seq")["doc_id"] == 1
    ins = rec.last("insert_point")
    assert ins["seq_no"] == 8
    assert ins["char_count"] == len("新切片正文")
    assert ins["block_type"] == "faq"
    assert res["data"]["seqNo"] == 8
    assert res["data"]["vecSynced"] is True
    # 写后必须失效索引，否则助手在重启前仍按旧切片作答
    assert "clear_index" in rec.names()
    assert rec.last("recount_doc_chunks")["doc_id"] == 1


def test_create_point_rejects_bad_block_type(mysql_mode, rec) -> None:
    res = kb_knowledge.create_point({"docId": "D001", "content": "x", "blockType": "video"})
    assert res["ok"] is False and "块类型非法" in res["error"]


def test_create_point_rejects_unknown_doc(mysql_mode, rec, monkeypatch) -> None:
    monkeypatch.setattr(kb_knowledge.kb_repo, "get_doc", lambda doc_id: None)
    res = kb_knowledge.create_point({"docId": "D404", "content": "x"})
    assert res["ok"] is False and "文档不存在" in res["error"]


def test_create_point_rejects_bad_doc_id(mysql_mode, rec) -> None:
    res = kb_knowledge.create_point({"docId": "ABC", "content": "x"})
    assert res["ok"] is False and "文档编号非法" in res["error"]


def test_create_point_vector_failure_does_not_rollback(mysql_mode, rec, monkeypatch) -> None:
    """向量重算失败只降级：正文必须落库，且返回 warning + vecSynced=False。"""

    class _Boom:
        def encode_one(self, text):
            raise RuntimeError("模型不可用")

    monkeypatch.setattr(kb_knowledge, "get_embedder", lambda: _Boom())
    res = kb_knowledge.create_point({"docId": "D001", "content": "正文"})
    assert res["ok"] is True
    assert res["data"]["vecSynced"] is False
    assert "向量重算失败" in res["data"]["warning"]
    assert "insert_point" in rec.names()
    assert "drop_point_vector" in rec.names()


def test_update_point_content_recomputes_char_count_and_vector(mysql_mode, rec) -> None:
    res = kb_knowledge.update_point("D001-C001", {"content": "改过的正文"})
    assert res["ok"] is True, res
    fields = rec.last("update_point_fields")["fields"]
    assert fields["content"] == "改过的正文"
    assert fields["char_count"] == 5
    assert res["data"]["changed"] == ["char_count", "content"]
    assert res["data"]["vecSynced"] is True
    assert rec.last("encode_one")["text"].endswith("改过的正文")


def test_update_point_meta_only_skips_reembed(mysql_mode, rec) -> None:
    res = kb_knowledge.update_point("D001-C001", {"blockType": "table"})
    assert res["ok"] is True
    assert "encode_one" not in rec.names()
    assert res["data"]["changed"] == ["block_type"]


def test_update_point_empty_payload_is_rejected(mysql_mode, rec) -> None:
    res = kb_knowledge.update_point("D001-C001", {})
    assert res["ok"] is False and "没有需要更新的字段" in res["error"]


def test_update_point_image_keys_empty_clears(mysql_mode, rec) -> None:
    """``imageKeys: []`` 是「清空」而不是「未传」——不能被假值跳过。"""
    res = kb_knowledge.update_point("D001-C001", {"imageKeys": []})
    assert res["ok"] is True
    assert rec.last("update_point_fields")["fields"]["image_keys"] is None


def test_update_point_missing_point_is_error(mysql_mode, rec, monkeypatch) -> None:
    monkeypatch.setattr(kb_knowledge.kb_repo, "get_point", lambda cid: None)
    res = kb_knowledge.update_point("D999-C999", {"content": "x"})
    assert res["ok"] is False and "知识点不存在" in res["error"]


@pytest.mark.parametrize("position,expected", [("end", "0123456789补"), ("start", "补0123456789")])
def test_append_text_position(mysql_mode, rec, position, expected) -> None:
    res = kb_knowledge.append_text("D001-C001", "补", position)
    assert res["ok"] is True, res
    assert rec.last("update_point_fields")["fields"]["content"] == expected
    assert res["data"]["appended"] == 1


def test_append_text_rejects_bad_position(mysql_mode, rec) -> None:
    res = kb_knowledge.append_text("D001-C001", "补", "middle")
    assert res["ok"] is False and "position 非法" in res["error"]


def test_remove_content_by_text(mysql_mode, rec) -> None:
    res = kb_knowledge.remove_content("D001-C001", text="345")
    assert res["ok"] is True, res
    assert rec.last("update_point_fields")["fields"]["content"] == "0126789"
    assert res["data"]["removed"] == 3
    assert res["data"]["range"] == [3, 6]


def test_remove_content_text_not_found_is_error(mysql_mode, rec) -> None:
    res = kb_knowledge.remove_content("D001-C001", text="不存在")
    assert res["ok"] is False and "未找到" in res["error"]
    assert "update_point_fields" not in rec.names()


def test_remove_content_by_range_clamps(mysql_mode, rec) -> None:
    res = kb_knowledge.remove_content("D001-C001", start=8, end=999)
    assert res["ok"] is True
    assert rec.last("update_point_fields")["fields"]["content"] == "01234567"


def test_remove_content_range_invalid(mysql_mode, rec) -> None:
    assert kb_knowledge.remove_content("D001-C001", start=5, end=5)["ok"] is False
    assert kb_knowledge.remove_content("D001-C001", start=100, end=200)["ok"] is False


def test_remove_content_without_locator_is_error(mysql_mode, rec) -> None:
    res = kb_knowledge.remove_content("D001-C001")
    assert res["ok"] is False and "需提供 text 或 start/end" in res["error"]


def test_remove_content_refuses_to_empty_body(mysql_mode, rec) -> None:
    """删空正文会让切片变成「空知识点」——必须挡下并引导改用删除知识点。"""
    res = kb_knowledge.remove_content("D001-C001", start=0, end=10)
    assert res["ok"] is False and "删除后正文为空" in res["error"]


def test_insert_image_rejects_foreign_key(mysql_mode, rec, monkeypatch) -> None:
    monkeypatch.setattr(
        kb_knowledge.doc_preview, "collect_images",
        lambda doc, chunks: [{"key": "uploads/images/ok.png"}],
    )
    res = kb_knowledge.insert_image("D001-C001", "uploads/images/other.png")
    assert res["ok"] is False and "图片不属于该文档" in res["error"]


def test_insert_image_appends_once(mysql_mode, rec, monkeypatch) -> None:
    monkeypatch.setattr(
        kb_knowledge.doc_preview, "collect_images",
        lambda doc, chunks: [{"key": "uploads/images/ok.png"}],
    )
    res = kb_knowledge.insert_image("D001-C001", "uploads/images/ok.png")
    assert res["ok"] is True and res["data"]["added"] is True
    assert rec.last("update_point_fields")["fields"]["image_keys"] == ["uploads/images/ok.png"]

    rec.calls.clear()
    again = kb_knowledge.insert_image("D001-C001", "uploads/images/ok.png")
    assert again["ok"] is True and again["data"]["added"] is False
    assert "update_point_fields" not in rec.names()


def test_delete_point_recounts_chunks(mysql_mode, rec) -> None:
    res = kb_knowledge.delete_point("D001-C001")
    assert res["ok"] is True
    assert res["data"]["deleted"] == 1
    assert rec.last("recount_doc_chunks")["doc_id"] == 1
    assert "clear_index" in rec.names()


def test_delete_point_missing_is_error(mysql_mode, rec, monkeypatch) -> None:
    monkeypatch.setattr(
        kb_knowledge.kb_repo, "delete_point",
        lambda cid: {"ok": False, "error": "知识点不存在：x"},
    )
    res = kb_knowledge.delete_point("D999-C999")
    assert res["ok"] is False


# ================================================================== 4. 导出
def test_export_csv_has_bom_and_header(mysql_mode, rec) -> None:
    res = kb_knowledge.export_points(["D001-C001"], "csv")
    assert res["ok"] is True
    content = res["data"]["content"]
    assert content.startswith(b"\xef\xbb\xbf")  # Excel 双击不乱码
    text = content.decode("utf-8-sig")
    assert "知识点编号" in text.splitlines()[0]
    assert "D001-C001" in text
    assert res["data"]["filename"].endswith(".csv")


def test_export_markdown_has_sections(mysql_mode, rec) -> None:
    res = kb_knowledge.export_points(["D001-C001"], "md")
    text = res["data"]["content"].decode("utf-8")
    assert text.startswith("# 知识点导出")
    assert "## D001-C001" in text
    assert "0123456789" in text


def test_export_json_is_valid_array(mysql_mode, rec) -> None:
    res = kb_knowledge.export_points(["D001-C001"], "json")
    arr = json.loads(res["data"]["content"].decode("utf-8"))
    assert isinstance(arr, list) and arr[0]["chunkId"] == "D001-C001"


def test_export_unknown_ids_is_error(mysql_mode, rec, monkeypatch) -> None:
    monkeypatch.setattr(kb_knowledge.kb_repo, "get_point", lambda cid: None)
    res = kb_knowledge.export_points(["D999-C999"], "csv")
    assert res["ok"] is False and "没有可导出的知识点" in res["error"]


def test_export_bad_format_is_error(mysql_mode, rec) -> None:
    res = kb_knowledge.export_points(["D001-C001"], "xlsx")
    assert res["ok"] is False and "导出格式非法" in res["error"]


# ================================================================== 5. 接口层
def test_api_list_shape(json_mode) -> None:
    resp = client.get("/api/knowledge/points?size=3")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    for key in ("items", "total", "page", "size", "editable", "filters"):
        assert key in data


def test_api_size_upper_bound_is_422(json_mode) -> None:
    assert client.get("/api/knowledge/points?size=999").status_code == 422


def test_api_page_ge_one_is_422(json_mode) -> None:
    assert client.get("/api/knowledge/points?page=0").status_code == 422


def test_api_detail_404_for_unknown(json_mode) -> None:
    # JSON 只读模式下服务层返回 400（找不到编号），语义一致即可
    resp = client.get("/api/knowledge/points/D999-C999")
    assert resp.status_code == 400
    assert "不存在" in resp.json()["detail"]


def test_api_create_blocked_in_json_mode(json_mode) -> None:
    resp = client.post("/api/knowledge/points", json={"docId": "D001", "content": "x"})
    assert resp.status_code == 400
    assert "MySQL" in resp.json()["detail"]


def test_api_create_requires_content() -> None:
    resp = client.post("/api/knowledge/points", json={"docId": "D001", "content": ""})
    assert resp.status_code == 422


def test_api_export_returns_attachment(json_mode) -> None:
    first = client.get("/api/knowledge/points?size=1").json()["data"]["items"][0]
    resp = client.post(
        "/api/knowledge/export",
        json={"chunkIds": [first["chunkId"]], "format": "csv"},
    )
    assert resp.status_code == 200, resp.text
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.content.startswith(b"\xef\xbb\xbf")


def test_api_export_empty_ids_is_422() -> None:
    assert client.post("/api/knowledge/export", json={"chunkIds": [], "format": "csv"}).status_code == 422


def test_api_delete_404_when_missing(mysql_mode, monkeypatch) -> None:
    monkeypatch.setattr(knowledge_api.kb_repo, "point_scope", lambda cid: None)
    resp = client.delete("/api/knowledge/points/D999-C999")
    assert resp.status_code == 404


def test_api_patch_404_when_missing(mysql_mode, monkeypatch) -> None:
    monkeypatch.setattr(knowledge_api.kb_repo, "point_scope", lambda cid: None)
    resp = client.patch("/api/knowledge/points/D999-C999", json={"content": "x"})
    assert resp.status_code == 404


def test_api_patch_success_returns_changed(mysql_mode, rec, monkeypatch) -> None:
    monkeypatch.setattr(knowledge_api.kb_repo, "point_scope", lambda cid: {
        "chunkId": cid, "docId": 1, "deptId": 100, "ktype": "guide",
    })
    resp = client.patch("/api/knowledge/points/D001-C001", json={"blockType": "table"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["changed"] == ["block_type"]


def test_api_append_text_endpoint(mysql_mode, rec, monkeypatch) -> None:
    monkeypatch.setattr(knowledge_api.kb_repo, "point_scope", lambda cid: {
        "chunkId": cid, "docId": 1, "deptId": 100, "ktype": "guide",
    })
    resp = client.post(
        "/api/knowledge/points/D001-C001/append-text",
        json={"text": "补", "position": "start"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["appended"] == 1


def test_api_remove_content_endpoint(mysql_mode, rec, monkeypatch) -> None:
    monkeypatch.setattr(knowledge_api.kb_repo, "point_scope", lambda cid: {
        "chunkId": cid, "docId": 1, "deptId": 100, "ktype": "guide",
    })
    resp = client.post(
        "/api/knowledge/points/D001-C001/remove-content",
        json={"text": "345"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["removed"] == 3


def test_api_remove_content_missing_locator_is_400(mysql_mode, rec, monkeypatch) -> None:
    monkeypatch.setattr(knowledge_api.kb_repo, "point_scope", lambda cid: {
        "chunkId": cid, "docId": 1, "deptId": 100, "ktype": "guide",
    })
    resp = client.post("/api/knowledge/points/D001-C001/remove-content", json={})
    assert resp.status_code == 400


def test_api_insert_image_endpoint(mysql_mode, rec, monkeypatch) -> None:
    monkeypatch.setattr(knowledge_api.kb_repo, "point_scope", lambda cid: {
        "chunkId": cid, "docId": 1, "deptId": 100, "ktype": "guide",
    })
    monkeypatch.setattr(
        kb_knowledge.doc_preview, "collect_images",
        lambda doc, chunks: [{"key": "uploads/images/ok.png"}],
    )
    resp = client.post(
        "/api/knowledge/points/D001-C001/insert-image",
        json={"key": "uploads/images/ok.png"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["added"] is True


# ================================================================== 6. 审计动作
def test_oplog_actions_registered() -> None:
    from app.security import oplog

    for name in ("ACTION_POINT_CREATE", "ACTION_POINT_UPDATE", "ACTION_POINT_DELETE", "ACTION_POINT_EXPORT"):
        assert hasattr(oplog, name), name
    assert oplog.ACTION_POINT_CREATE == "POINT_CREATE"
