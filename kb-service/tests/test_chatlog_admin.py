"""会话日志管理测试（任务 2.1）。

覆盖：问答视图结构/排序/分页、关键词检索（问题与回答）、角色与时间筛选、
咨询用户视图聚合一致性、CSV 导出（表头/BOM/行数/与页面同口径）、
接口层契约与参数校验、鉴权 401/403。

只覆盖 **JSON 后端**（``KB_STORE=json``，用 tmp_path 隔离 sessions.json）。
MySQL 分支的同套断言由 ``tools/verify_chatlogs.py --store mysql`` 在有库环境执行。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import chatlog
from app.main import app
from app.security import auth
from app.services import chatlog_admin

client = TestClient(app)


@dataclass
class _Gen:
    """最小 GenerationResult 替身（chatlog.save_qa 只用到这几个字段）。"""

    answer: str = "根据知识库资料[1]，请携带校园卡前往办理。"
    citations: list = field(default_factory=lambda: [{"n": 1, "chunkId": "d1::0"}])
    confidence: float = 0.72
    refused: bool = False


@pytest.fixture(autouse=True)
def _isolate_store(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(chatlog, "SESSIONS_PATH", tmp_path / "sessions.json")
    yield


@pytest.fixture
def seeded() -> dict:
    """写入可控的三会话样本：本科生×2、访客×1、研究生×1（共 4 个问答）。"""
    chatlog.save_qa("图书馆借书流程是什么", "本科生", _Gen(), "S-A")
    chatlog.save_qa("校园卡怎么补办", "本科生", _Gen(answer="到一卡通中心办理。"), "S-A")
    chatlog.save_qa("食堂几点开门", "访客", _Gen(answer="抱歉，知识库中未找到相关内容。", refused=True), "S-B")
    chatlog.save_qa("研究生宿舍申请条件", "研究生", _Gen(answer="详见研究生院通知。"), "S-C")
    return {"qa": 4, "sessions": 3}


# ------------------------------------------------------------------ 问答视图
def test_query_qa_structure_and_order(seeded) -> None:
    data = chatlog_admin.query_qa(page_size=50)
    assert data["total"] == seeded["qa"]
    assert data["store"] == "json"
    assert {"items", "total", "page", "pageSize", "totalPages", "scanned", "scanLimit"} <= set(data)

    item = data["items"][0]
    assert {"msgId", "sessionId", "question", "answer", "role", "askedAt", "answeredAt"} <= set(item)

    stamps = [r["answeredAt"] or "" for r in data["items"]]
    assert stamps == sorted(stamps, reverse=True), "应按回答时间倒序"


def test_query_qa_pagination_no_overlap_no_gap(seeded) -> None:
    """msgId 只在会话内自增、不是全局唯一 —— 身份必须用 (sessionId, msgId)。"""
    page1 = chatlog_admin.query_qa(page=1, page_size=2)
    page2 = chatlog_admin.query_qa(page=2, page_size=2)
    ids1 = {(r["sessionId"], r["msgId"]) for r in page1["items"]}
    ids2 = {(r["sessionId"], r["msgId"]) for r in page2["items"]}
    assert not (ids1 & ids2)
    assert len(ids1 | ids2) == seeded["qa"]

    far = chatlog_admin.query_qa(page=99, page_size=2)
    assert far["items"] == []
    assert far["total"] == seeded["qa"]


def test_query_qa_keyword_hits_question_and_answer(seeded) -> None:
    by_question = chatlog_admin.query_qa(keyword="借书", page_size=50)
    assert by_question["total"] == 1
    assert "借书" in by_question["items"][0]["question"]

    by_answer = chatlog_admin.query_qa(keyword="一卡通中心", page_size=50)
    assert by_answer["total"] == 1
    assert "一卡通中心" in by_answer["items"][0]["answer"]

    assert chatlog_admin.query_qa(keyword="绝不存在的词ZZZ", page_size=50)["total"] == 0


def test_query_qa_role_filter(seeded) -> None:
    under = chatlog_admin.query_qa(role="本科生", page_size=50)
    assert under["total"] == 2
    assert all(r["role"] == "本科生" for r in under["items"])

    assert chatlog_admin.query_qa(role="教职工", page_size=50)["total"] == 0


def test_query_qa_date_range(seeded) -> None:
    today = date.today().isoformat()
    assert chatlog_admin.query_qa(date_from=today, date_to=today, page_size=50)["total"] == 4

    past = (date.today() - timedelta(days=30)).isoformat()
    assert chatlog_admin.query_qa(date_from=past, date_to=past, page_size=50)["total"] == 0


def test_query_qa_invalid_date_is_ignored(seeded) -> None:
    """无法解析的时间参数按「不筛选」处理，不抛错。"""
    assert chatlog_admin.query_qa(date_from="not-a-date", page_size=50)["total"] == 4


def test_query_qa_refused_flag_survives(seeded) -> None:
    refused = [r for r in chatlog_admin.query_qa(page_size=50)["items"] if r["refused"]]
    assert len(refused) == 1
    assert refused[0]["sessionId"] == "S-B"


# ------------------------------------------------------------------ 用户视图
def test_query_users_aggregation_is_consistent(seeded) -> None:
    qa = chatlog_admin.query_qa(page_size=50)
    users = chatlog_admin.query_users(page_size=50)

    assert users["total"] == seeded["sessions"]
    assert len({r["sessionId"] for r in users["items"]}) == users["total"]
    assert sum(r["questionCount"] for r in users["items"]) == qa["total"]
    assert all(r["questionCount"] >= 1 for r in users["items"])

    stamps = [r["lastAt"] or "" for r in users["items"]]
    assert stamps == sorted(stamps, reverse=True)


def test_query_users_role_filter(seeded) -> None:
    users = chatlog_admin.query_users(role="本科生", page_size=50)
    assert users["total"] == 1
    assert users["items"][0]["questionCount"] == 2
    assert users["items"][0]["role"] == "本科生"


# ------------------------------------------------------------------ 导出
def test_export_rows_same_scope_as_pagination(seeded) -> None:
    rows, truncated = chatlog_admin.export_rows("qa")
    assert len(rows) == chatlog_admin.query_qa(page_size=1)["total"]
    assert truncated is False

    frows, _ = chatlog_admin.export_rows("qa", role="本科生")
    assert len(frows) == chatlog_admin.query_qa(role="本科生", page_size=1)["total"] == 2

    urows, _ = chatlog_admin.export_rows("users")
    assert len(urows) == chatlog_admin.query_users(page_size=1)["total"]


def test_to_csv_header_bom_and_row_count(seeded) -> None:
    rows, _ = chatlog_admin.export_rows("qa")
    text = chatlog_admin.to_csv("qa", rows)
    assert text.startswith("\ufeff"), "Excel 需要 UTF-8 BOM"
    lines = text.lstrip("\ufeff").splitlines()
    assert lines[0] == ",".join(name for name, _ in chatlog_admin.QA_CSV_COLUMNS)
    assert len(lines) == len(rows) + 1

    empty = chatlog_admin.to_csv("qa", [])
    assert len(empty.lstrip("\ufeff").splitlines()) == 1

    utext = chatlog_admin.to_csv("users", chatlog_admin.export_rows("users")[0])
    assert utext.lstrip("\ufeff").splitlines()[0] == ",".join(
        name for name, _ in chatlog_admin.USER_CSV_COLUMNS
    )


def test_filter_options(seeded) -> None:
    opts = chatlog_admin.filter_options()
    assert {"agents", "channels", "roles", "store"} <= set(opts)
    assert set(opts["roles"]) == {"本科生", "研究生", "访客"}


# ------------------------------------------------------------------ 接口层
def test_api_chatlogs_ok(seeded) -> None:
    resp = client.get("/api/admin/chatlogs", params={"pageSize": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"]["total"] == seeded["qa"]
    assert len(body["data"]["items"]) == 2


def test_api_chatlogs_users_and_filters(seeded) -> None:
    assert client.get("/api/admin/chatlogs/users").json()["ok"] is True
    filters = client.get("/api/admin/chatlogs/filters").json()
    assert filters["ok"] is True
    assert "roles" in filters["data"]


def test_api_page_size_validation(seeded) -> None:
    assert client.get("/api/admin/chatlogs", params={"pageSize": 0}).status_code == 422
    assert client.get("/api/admin/chatlogs", params={"pageSize": 999}).status_code == 422
    assert client.get("/api/admin/chatlogs", params={"page": 0}).status_code == 422


def test_api_export_headers(seeded) -> None:
    resp = client.get("/api/admin/chatlogs/export", params={"view": "qa"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "filename*=UTF-8''" in resp.headers["content-disposition"]
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.text.startswith("\ufeff")

    assert client.get("/api/admin/chatlogs/export", params={"view": "bogus"}).status_code == 422


def test_api_operation_logs_json_mode_returns_empty(seeded) -> None:
    """JSON 模式不落审计表，应返回空列表而不是 500。"""
    body = client.get("/api/admin/operation-logs").json()
    assert body["ok"] is True
    assert body["data"]["items"] == []


def test_api_auth_required_when_enabled(monkeypatch, seeded) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "auth_enabled", True)
    assert client.get("/api/admin/chatlogs").status_code == 401
    assert client.get("/api/admin/chatlogs/export").status_code == 401


def test_api_non_admin_forbidden(monkeypatch, seeded) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "auth_enabled", True)
    token = auth.create_token("20230001", "本科生")
    resp = client.get("/api/admin/chatlogs", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403
    assert "本科生" in resp.json()["detail"]


def test_api_admin_allowed(monkeypatch, seeded) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "auth_enabled", True)
    token = auth.create_token("admin-001", "管理员")
    resp = client.get("/api/admin/chatlogs", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["data"]["total"] == seeded["qa"]
