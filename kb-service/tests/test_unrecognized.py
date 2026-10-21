"""未识别问题闭环测试（任务 3.11 / M-S-07～09）。

覆盖：问题归一化、三种入池来源、同问聚合与筛选、语义聚类、业务域分类、
详情追溯、批量处理、CSV 导出、反哺草稿（MySQL 路径打桩）与接口契约。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.core import pipeline
from app.core.agent_runtime import AgentRuntime
from app.core.index_store import IndexSnapshot
from app.db import chatlog, unrecognized_store
from app.main import app
from app.services import unrecognized_admin

client = TestClient(app)


@dataclass
class _Gen:
    answer: str = "请携带校园卡前往信息中心办理。"
    citations: list = field(default_factory=list)
    confidence: float = 0.8
    refused: bool = False
    usage: dict = field(default_factory=dict)
    model: str = "mock"
    latency_ms: int = 12
    first_token_ms: int | None = None
    model_code: str = ""
    model_source: str = ""


def _seed(
    question: str = "校园卡怎么补办？",
    *,
    source: str = unrecognized_store.SOURCE_LOW_CONFIDENCE,
    role: str = "本科生",
    channel: str = "portal-web",
    confidence: float = 0.2,
    session_id: str = "S-U1",
    msg_id: int | None = None,
) -> int:
    return unrecognized_store.note_unrecognized(
        session_id=session_id,
        question=question,
        role=role,
        confidence=confidence,
        source=source,
        msg_id=msg_id,
        agent_id=3,
        channel_code=channel,
        dept="信息中心",
    )


# ------------------------------------------------------------------ 归一化与分类
def test_normalize_question_collapses_width_case_and_punct() -> None:
    assert unrecognized_store.normalize_question("  校园卡 怎么补办？！ ") == "校园卡怎么补办"
    assert unrecognized_store.normalize_question("ＷＩＦＩ怎么连？") == "wifi怎么连"
    assert unrecognized_store.normalize_question("How  to  apply?") == "howtoapply"


def test_classify_domain_by_keywords() -> None:
    assert unrecognized_admin.classify_domain("校园卡丢了怎么补办") == "财务缴费"
    assert unrecognized_admin.classify_domain("图书馆能借几本书") == "图书馆"
    assert unrecognized_admin.classify_domain("选课什么时候开始") == "教务教学"
    assert unrecognized_admin.classify_domain("今天天气怎么样") == "其他"


# ------------------------------------------------------------------ 入池与聚合
def test_note_dedupes_same_session_and_message() -> None:
    first = _seed(msg_id=11)
    second = _seed(msg_id=11)
    assert first == second
    assert len(unrecognized_store.list_records()) == 1


def test_note_respects_switch(monkeypatch) -> None:
    monkeypatch.setattr(settings, "unrecognized_enabled", False)
    assert _seed() is None
    assert unrecognized_store.list_records() == []


def test_aggregate_counts_same_normalized_question() -> None:
    _seed("校园卡怎么补办？", channel="portal-web")
    _seed("校园卡怎么补办", channel="wechat", role="研究生", session_id="S-U2")
    _seed("图书馆借书期限", channel="portal-web", session_id="S-U3")

    data = unrecognized_admin.list_questions()
    assert data["total"] == 2
    assert data["recordTotal"] == 3
    card = next(item for item in data["items"] if "校园卡" in item["question"])
    assert card["count"] == 2
    assert set(card["roles"]) == {"本科生", "研究生"}
    assert set(card["channels"]) == {"portal-web", "wechat"}
    assert card["idsTruncated"] is False
    assert card["firstAt"] and card["lastAt"]


def test_filters_by_state_source_role_and_channel() -> None:
    rid = _seed()
    _seed("图书馆借书期限", channel="wechat", role="研究生", session_id="S-U2")
    unrecognized_store.update_records([rid], state=unrecognized_store.STATE_IGNORED)

    pending = unrecognized_admin.list_questions(state=unrecognized_store.STATE_PENDING)
    assert pending["total"] == 1
    ignored = unrecognized_admin.list_questions(state=unrecognized_store.STATE_IGNORED)
    assert ignored["total"] == 1
    by_role = unrecognized_admin.list_questions(role="研究生")
    assert by_role["total"] == 1
    by_channel = unrecognized_admin.list_questions(channel_code="wechat")
    assert by_channel["total"] == 1
    by_source = unrecognized_admin.list_questions(source=unrecognized_store.SOURCE_REFUSED)
    assert by_source["total"] == 0


def test_semantic_cluster_groups_variants(monkeypatch) -> None:
    _seed("校园卡怎么补办", session_id="S-U1")
    _seed("校园卡丢失如何补办", session_id="S-U2")
    _seed("图书馆借书期限", session_id="S-U3")

    class _Emb:
        def encode(self, texts):
            vectors = []
            for text in texts:
                if "校园卡" in text:
                    vectors.append([1.0, 0.0, 0.0])
                else:
                    vectors.append([0.0, 1.0, 0.0])
            return vectors

    monkeypatch.setattr(unrecognized_admin, "get_embedder", lambda: _Emb())
    unrecognized_admin.reset_cluster_cache()
    data = unrecognized_admin.list_questions()
    card_items = [item for item in data["items"] if "校园卡" in item["question"]]
    assert len(card_items) == 2
    assert card_items[0]["clusterId"] == card_items[1]["clusterId"]
    assert any(cluster["questionCount"] == 2 for cluster in data["clusters"])


# ------------------------------------------------------------------ 详情 / 批量 / 导出
def test_detail_traces_session_messages() -> None:
    chatlog.append_message("S-D", "user", "校园卡丢了怎么办", user_role="本科生")
    ai = chatlog.append_message(
        "S-D",
        "ai",
        "请到信息中心补办。",
        confidence=0.2,
        refused=False,
        citations=[{"chunkId": "D001-C001", "docId": 1, "n": 1}],
    )
    rid = unrecognized_store.note_unrecognized(
        session_id="S-D",
        question="校园卡丢了怎么办",
        msg_id=ai["msgId"],
        source=unrecognized_store.SOURCE_LOW_CONFIDENCE,
        confidence=0.2,
    )
    data = unrecognized_admin.detail(rid)["data"]
    assert data["session"]["messageCount"] == 2
    assert data["questionMessage"]["content"] == "校园卡丢了怎么办"
    assert data["answerMessage"]["content"] == "请到信息中心补办。"
    assert data["answerMessage"]["citations"][0]["chunkId"] == "D001-C001"
    assert data["domain"] == "财务缴费"
    assert data["variantCount"] == 1


def test_detail_missing_returns_404_shape() -> None:
    result = unrecognized_admin.detail(99999)
    assert result["ok"] is False
    assert result["status"] == 404


def test_batch_ignore_reopen_and_classify() -> None:
    rid = _seed()
    ignored = unrecognized_admin.batch([rid], None, "ignore")
    assert ignored["ok"] and ignored["data"]["updated"] == 1
    assert unrecognized_store.get_record(rid)["state"] == unrecognized_store.STATE_IGNORED

    reopened = unrecognized_admin.batch(None, ["校园卡怎么补办"], "reopen")
    assert reopened["ok"] and reopened["data"]["updated"] == 1
    assert unrecognized_store.get_record(rid)["state"] == unrecognized_store.STATE_PENDING

    classified = unrecognized_admin.batch([rid], None, "classify", dept="学生工作处")
    assert classified["ok"]
    assert unrecognized_store.get_record(rid)["dept"] == "学生工作处"


def test_export_csv_has_bom_and_rows() -> None:
    _seed("校园卡怎么补办？")
    _seed("校园卡怎么补办？", session_id="S-U2")
    body, rows, truncated = unrecognized_admin.export_csv()
    assert body.startswith("\ufeff")
    assert rows == 1
    assert truncated is False
    assert "校园卡怎么补办" in body
    assert "待处理" in body


# ------------------------------------------------------------------ 入池触发（问答链路）
def test_save_qa_pools_refused_and_low_confidence(monkeypatch) -> None:
    chatlog.save_qa("问题A", "本科生", _Gen(refused=True, confidence=0.15), "S-P1")
    chatlog.save_qa("问题B", "本科生", _Gen(confidence=0.2), "S-P2")
    chatlog.save_qa("问题C", "本科生", _Gen(confidence=0.9), "S-P3")

    records = {r["question"]: r for r in unrecognized_store.list_records()}
    assert records["问题A"]["source"] == unrecognized_store.SOURCE_REFUSED
    assert records["问题B"]["source"] == unrecognized_store.SOURCE_LOW_CONFIDENCE
    assert "问题C" not in records


def test_save_qa_records_attribution(monkeypatch) -> None:
    chatlog.save_qa(
        "宿舍报修找谁",
        "本科生",
        _Gen(refused=True),
        "S-P4",
        agent_id=7,
        channel_code="dorm-web",
        dept="后勤保障处",
    )
    rec = unrecognized_store.list_records()[0]
    assert rec["agentId"] == "7"
    assert rec["channelCode"] == "dorm-web"
    assert rec["dept"] == "后勤保障处"


def test_sensitive_question_is_not_pooled(monkeypatch) -> None:
    from app.security import sensitive

    monkeypatch.setattr(sensitive, "check_question", lambda question: "敏感词")
    ans = pipeline.answer_question("任意问题", role="本科生")
    assert ans.refused is True
    assert unrecognized_store.list_records() == []


def test_empty_recall_pools_weak_recall_with_attribution(monkeypatch) -> None:
    monkeypatch.setattr(pipeline, "get_index", lambda: IndexSnapshot())
    runtime = AgentRuntime(agent_id=7, depts=("学工处",))
    ans = pipeline.answer_question(
        "宿舍报修找谁", runtime=runtime, channel_code="dorm-web"
    )
    assert ans.refused is True
    records = unrecognized_store.list_records()
    assert len(records) == 1
    rec = records[0]
    assert rec["sessionId"] == ans.session_id
    assert rec["msgId"] == ans.msg_id
    assert rec["source"] == unrecognized_store.SOURCE_WEAK_RECALL
    assert rec["agentId"] == "7"
    assert rec["channelCode"] == "dorm-web"
    assert rec["dept"] == "学工处"


# ------------------------------------------------------------------ 反哺草稿
def test_feedback_requires_mysql(monkeypatch) -> None:
    rid = _seed()
    result = unrecognized_admin.feedback(ids=[rid])
    assert result["ok"] is False
    assert "MySQL" in result["error"]


def test_feedback_creates_draft_and_marks_records(monkeypatch) -> None:
    rec1 = {
        "id": 1,
        "question": "校园卡怎么补办",
        "questionKey": "校园卡怎么补办",
        "dept": "信息中心",
        "newDocId": None,
    }
    rec2 = {**rec1, "id": 2}
    captured: dict = {}
    updated: dict = {}
    monkeypatch.setattr(unrecognized_admin, "settings", SimpleNamespace(store_label="mysql"))
    monkeypatch.setattr(unrecognized_store, "get_record", lambda rid: {1: rec1, 2: rec2}.get(rid))

    def fake_ingest(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "docId": 9001, "chunkCount": 1}

    def fake_update(ids, **kwargs):
        updated["ids"] = list(ids)
        updated.update(kwargs)
        return len(ids)

    monkeypatch.setattr(unrecognized_admin.kb_admin, "ingest_uploaded", fake_ingest)
    monkeypatch.setattr(unrecognized_admin.kb_repo, "list_chunks", lambda doc_id: [{"chunkId": "D9001-C001"}])
    monkeypatch.setattr(unrecognized_admin.kb_repo, "doc_public_id", lambda doc_id: "D9001")
    monkeypatch.setattr(unrecognized_store, "update_records", fake_update)

    result = unrecognized_admin.feedback(ids=[1, 2], handler_id=9)
    assert result["ok"] is True
    assert captured["status"] == 0, "反哺必须生成草稿，不能直接发布"
    assert captured["dept"] == "信息中心"
    assert captured["filename"].endswith(".md")
    assert updated["state"] == unrecognized_store.STATE_FED_BACK
    assert updated["new_doc_id"] == 9001
    assert updated["new_chunk_id"] == "D9001-C001"
    assert result["data"]["publicId"] == "D9001"


def test_feedback_rejects_duplicate(monkeypatch) -> None:
    rec = {
        "id": 1,
        "question": "校园卡怎么补办",
        "questionKey": "校园卡怎么补办",
        "dept": "信息中心",
        "newDocId": 8001,
    }
    monkeypatch.setattr(unrecognized_admin, "settings", SimpleNamespace(store_label="mysql"))
    monkeypatch.setattr(unrecognized_store, "get_record", lambda rid: rec)
    result = unrecognized_admin.feedback(ids=[1])
    assert result["ok"] is False
    assert result["status"] == 409


# ------------------------------------------------------------------ 接口契约
def test_api_list_detail_batch_and_export() -> None:
    rid = _seed("校园卡怎么补办？")
    _seed("校园卡怎么补办？", session_id="S-U2")

    listing = client.get("/api/admin/unrecognized", params={"state": 0}).json()
    assert listing["ok"] is True
    assert listing["data"]["total"] == 1
    assert listing["data"]["items"][0]["count"] == 2

    filters = client.get("/api/admin/unrecognized/filters").json()
    assert filters["ok"] is True
    assert any(item["code"] == "portal-web" for item in filters["data"]["channels"])

    detail = client.get(f"/api/admin/unrecognized/{rid}").json()
    assert detail["ok"] is True

    batch = client.post(
        "/api/admin/unrecognized/batch",
        json={"ids": [rid], "action": "ignore"},
    ).json()
    assert batch["ok"] is True and batch["data"]["updated"] == 1

    export = client.get("/api/admin/unrecognized/export")
    assert export.status_code == 200
    assert export.text.startswith("\ufeff")


def test_api_feedback_json_mode_returns_400() -> None:
    rid = _seed()
    res = client.post(
        "/api/admin/unrecognized/feedback",
        json={"ids": [rid]},
    )
    assert res.status_code == 400
    assert "MySQL" in res.json()["detail"]


def test_api_unknown_record_returns_404() -> None:
    res = client.get("/api/admin/unrecognized/99999")
    assert res.status_code == 404
