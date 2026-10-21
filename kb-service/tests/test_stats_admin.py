"""统计与运营分析测试（任务 3.10 / M-T-01～09）。

覆盖：概览指标与会话日志（2.1）同口径、未答/成功率、按天趋势、渠道与助手
聚合、热门知识与分类排名、设备采集与首次写入、情绪/应用空态、CSV 导出、
接口层契约、权限映射与鉴权。

只覆盖 **JSON 后端**（``KB_STORE=json``）；MySQL 分支由
``tools/verify_stats.py --store mysql`` 在有库环境执行。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.db import chatlog
from app.main import app
from app.security import auth, permissions
from app.services import chatlog_admin, stats_admin

client = TestClient(app)


@dataclass
class _Gen:
    answer: str = "请携带校园卡前往办理。"
    citations: list = field(default_factory=list)
    confidence: float = 0.8
    refused: bool = False


DOC = {
    "docId": 99001,
    "titlePath": "校园卡管理办法",
    "dept": "信息中心",
    "ktype": "制度",
}
DOC2 = {
    "docId": 99002,
    "titlePath": "图书馆借阅规则",
    "dept": "图书馆",
    "ktype": "流程",
}


@pytest.fixture
def seeded() -> dict:
    """两个会话 3 条问答：本科生 2 答（3 条引用）+ 访客 1 拒答。"""
    chatlog.save_qa(
        "校园卡怎么补办？", "本科生", _Gen(citations=[DOC]), "S-A"
    )
    chatlog.save_qa(
        "图书馆能借几本书？",
        "本科生",
        _Gen(answer="本科生可借 10 本。", citations=[DOC, DOC2]),
        "S-A",
    )
    chatlog.save_qa(
        "食堂几点开门？",
        "访客",
        _Gen(answer="抱歉，知识库中未找到相关内容。", refused=True),
        "S-B",
    )
    return {"qa": 3, "sessions": 2, "citations": 3}


# ------------------------------------------------------------------ 概览口径
def test_summary_matches_chatlogs(seeded) -> None:
    data = stats_admin.overview()
    qa = chatlog_admin.query_qa(page_size=1)

    assert data["summary"]["questions"] == qa["total"] == seeded["qa"]
    assert data["summary"]["replies"] == qa["total"]
    assert data["window"]["scanned"] == qa["scanned"]
    assert data["window"]["scanLimit"] == chatlog_admin.SCAN_LIMIT
    assert data["store"] == "json"


def test_summary_refused_success_rate_and_visits(seeded) -> None:
    summary = stats_admin.overview()["summary"]
    assert summary["unanswered"] == 1
    assert summary["answered"] == 2
    assert summary["successRate"] == pytest.approx(2 / 3, abs=1e-4)
    assert summary["sessions"] == summary["visits"] == seeded["sessions"]
    assert summary["persons"] == seeded["sessions"], "JSON 无人身份，人次按会话计"


def test_overview_filters_align_with_chatlogs(seeded) -> None:
    role = stats_admin.overview(role="本科生")
    assert role["summary"]["questions"] == chatlog_admin.query_qa(
        role="本科生", page_size=1
    )["total"] == 2
    assert role["summary"]["unanswered"] == 0
    assert role["summary"]["successRate"] == 1.0

    today = date.today().isoformat()
    same_day = stats_admin.overview(date_from=today, date_to=today)
    assert same_day["summary"]["questions"] == seeded["qa"]

    past = (date.today() - timedelta(days=30)).isoformat()
    empty = stats_admin.overview(date_from=past, date_to=past)
    assert empty["summary"] == {
        "questions": 0,
        "replies": 0,
        "unanswered": 0,
        "answered": 0,
        "sessions": 0,
        "visits": 0,
        "persons": 0,
        "successRate": None,
    }
    assert empty["trend"] == [] and empty["channels"] == [] and empty["agents"] == []


def test_trend_by_day(seeded) -> None:
    rows, truncated = stats_admin._trend(
        [
            {"answeredAt": "2026-10-01T08:00:00", "sessionId": "A"},
            {"answeredAt": "2026-10-01T09:00:00", "sessionId": "A", "refused": True},
            {"answeredAt": "2026-10-02T09:00:00", "sessionId": "B"},
        ]
    )
    assert truncated is False
    assert [r["date"] for r in rows] == ["2026-10-01", "2026-10-02"]
    assert rows[0] == {
        "date": "2026-10-01",
        "questions": 2,
        "replies": 2,
        "unanswered": 1,
        "sessions": 1,
    }
    assert rows[1]["sessions"] == 1


def test_channel_and_agent_rows(seeded) -> None:
    data = stats_admin.overview()
    channel = data["channels"][0]
    agent = data["agents"][0]
    assert channel["channelId"] is None
    assert channel["questions"] == seeded["qa"]
    assert channel["share"] == 1.0
    assert "门户Web" in channel["name"]
    assert agent["agentId"] is None
    assert agent["unanswered"] == 1
    assert "能小智" in agent["name"]


# ------------------------------------------------------------------ 排行
def test_knowledge_and_category_rankings(seeded, monkeypatch) -> None:
    monkeypatch.setattr(stats_admin, "_json_doc_map", lambda: {})
    data = stats_admin.rankings(limit=10)

    assert data["citations"] == seeded["citations"]
    assert data["knowledgeTotal"] == 2
    assert len(data["knowledge"]) == 2

    top = data["knowledge"][0]
    assert top["docId"] == DOC["docId"]
    assert top["docTitle"] == DOC["titlePath"]
    assert top["citations"] == 2
    assert top["sessions"] == 1
    assert top["share"] == pytest.approx(2 / 3, abs=1e-4)

    assert data["categoryTotal"] == 2
    labels = {c["label"] for c in data["categories"]}
    assert labels == {"信息中心 · 制度", "图书馆 · 流程"}

    smaller = stats_admin.rankings(limit=1)
    assert len(smaller["knowledge"]) == 1
    assert smaller["knowledgeTotal"] == 2, "limit 只截断列表，不改变总数"


def test_device_buckets_and_first_write_wins(seeded) -> None:
    chatlog.set_session_device("S-A", "mobile")
    assert chatlog.set_session_device("S-A", "pc") is False, "只补空值，不覆盖首次设备"
    assert chatlog.set_session_device("S-A", "bogus") is False
    assert chatlog.set_session_device("S-MISSING", "pc") is False

    devices = stats_admin.rankings()["devices"]
    by_key = {d["key"]: d["count"] for d in devices}
    assert by_key == {"mobile": 1, "unknown": 1}
    assert stats_admin.rankings()["notes"].get("devices") is None, "已采集到设备则不再提示空态"


def test_empty_state_for_devices_sentiments_apps(seeded) -> None:
    data = stats_admin.rankings()
    assert data["apps"] == []
    assert data["sentiments"] == []
    assert "应用访问埋点尚未接入" in data["notes"]["apps"]
    assert "4.6" in data["notes"]["sentiments"]
    assert "暂无设备数据" in data["notes"]["devices"]


def test_no_knowledge_note_without_citations() -> None:
    chatlog.save_qa("没有引用的回答", "访客", _Gen(), "S-NO-CITE")
    data = stats_admin.rankings()
    assert data["citations"] == 0
    assert data["knowledge"] == [] and data["categories"] == []
    assert "没有引用记录" in data["notes"]["knowledge"]


# ------------------------------------------------------------------ 设备采集
@pytest.mark.parametrize(
    ("ua", "device"),
    [
        ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Mobile/15E148", "mobile"),
        ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Mobile Safari/537.36", "mobile"),
        ("Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15", "tablet"),
        ("Mozilla/5.0 (Linux; Android 13; SM-X700) AppleWebKit/537.36 Safari/537.36", "tablet"),
        ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 Safari/537.36", "pc"),
    ],
)
def test_chat_captures_device_from_user_agent(ua, device) -> None:
    sid = f"S-UA-{device}"
    resp = client.post(
        "/api/chat",
        json={"question": "校园卡怎么补办？", "role": "本科生", "sessionId": sid, "stream": False},
        headers={"User-Agent": ua},
    )
    assert resp.status_code == 200, resp.text
    assert chatlog.load_session(sid)["device"] == device

    buckets = {d["key"]: d["count"] for d in stats_admin.rankings()["devices"]}
    assert buckets.get(device, 0) >= 1


def test_device_of_unknown_when_user_agent_missing() -> None:
    from types import SimpleNamespace

    from app.api.chat import _device_of

    assert _device_of(SimpleNamespace(headers={})) == "other"
    assert _device_of(SimpleNamespace(headers={"user-agent": "curl/8.0"})) == "pc"


# ------------------------------------------------------------------ 导出
def test_export_summary_and_trend(seeded) -> None:
    text, rows = stats_admin.export_csv("summary")
    assert rows == len(stats_admin.SUMMARY_LABELS)
    assert text.startswith("\ufeff")
    lines = text.lstrip("\ufeff").splitlines()
    assert lines[0] == ",".join(name for name, _ in stats_admin.EXPORT_COLUMNS["summary"])
    assert len(lines) == rows + 1

    trend_text, trend_rows = stats_admin.export_csv("trend")
    assert trend_rows == len(stats_admin.overview()["trend"]) == 1
    assert trend_text.startswith("\ufeff")


def test_export_rankings_respect_scope(seeded, monkeypatch) -> None:
    monkeypatch.setattr(stats_admin, "_json_doc_map", lambda: {})
    text, rows = stats_admin.export_csv("knowledge")
    assert rows == 2
    lines = text.lstrip("\ufeff").splitlines()
    assert lines[0] == ",".join(name for name, _ in stats_admin.EXPORT_COLUMNS["knowledge"])

    _, role_rows = stats_admin.export_csv("knowledge", role="访客")
    assert role_rows == 0
    role_text, _ = stats_admin.export_csv("knowledge", role="访客")
    assert len(role_text.lstrip("\ufeff").splitlines()) == 1, "空结果仍保留表头"


def test_export_rejects_unknown_kind(seeded) -> None:
    with pytest.raises(ValueError):
        stats_admin.export_csv("bogus")


# ------------------------------------------------------------------ 接口层
def test_api_overview_and_rankings(seeded) -> None:
    overview = client.get("/api/admin/stats/overview").json()
    assert overview["ok"] is True
    assert overview["data"]["summary"]["questions"] == seeded["qa"]
    assert {"summary", "trend", "channels", "agents", "window", "notes", "store"} <= set(
        overview["data"]
    )

    rankings = client.get("/api/admin/stats/rankings", params={"limit": 5}).json()
    assert rankings["ok"] is True
    assert {"knowledge", "categories", "devices", "sentiments", "apps", "notes"} <= set(
        rankings["data"]
    )

    filters = client.get("/api/admin/stats/filters").json()
    assert filters["ok"] is True and "roles" in filters["data"]


def test_api_validation(seeded) -> None:
    assert client.get("/api/admin/stats/rankings", params={"limit": 0}).status_code == 422
    assert client.get("/api/admin/stats/rankings", params={"limit": 51}).status_code == 422


def test_api_export_headers(seeded) -> None:
    resp = client.get("/api/admin/stats/export", params={"kind": "summary"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "filename*=UTF-8''" in resp.headers["content-disposition"]
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.text.startswith("\ufeff")

    assert client.get("/api/admin/stats/export", params={"kind": "bogus"}).status_code == 422


def test_api_auth_and_permissions(monkeypatch, seeded) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "auth_enabled", True)
    assert client.get("/api/admin/stats/overview").status_code == 401
    assert client.get("/api/admin/stats/export").status_code == 401

    guest = auth.create_token("20230001", "本科生")
    resp = client.get(
        "/api/admin/stats/overview", headers={"Authorization": f"Bearer {guest}"}
    )
    assert resp.status_code == 403

    admin = auth.create_token("admin-001", "管理员")
    resp = client.get(
        "/api/admin/stats/rankings", headers={"Authorization": f"Bearer {admin}"}
    )
    assert resp.status_code == 200


def test_permission_mapping_and_catalog() -> None:
    assert permissions.required_for("GET", "/api/admin/stats/overview") == "stats.view"
    assert permissions.required_for("GET", "/api/admin/stats/rankings") == "stats.view"
    assert permissions.required_for("GET", "/api/admin/stats/export") == "stats.export"
    assert "stats" in [g["module"] for g in permissions.GROUPS]
    assert {"stats.view", "stats.export"} <= permissions.ALL_CODES
