"""问数服务测试（任务 4.3 / P-DQ-01·02·03）。

覆盖：

1. **意图与参数**：课表/成绩/一卡通识别，周次/星期/学期/天数/条数抽取。
2. **身份与权限**：演示模式固定演示学号；登录后以 token 身份为准；
   教务仅本科生/研究生，一卡通另含教职工/离退休；未映射学号拒绝。
3. **卡片**：mock 课表/成绩/一卡通的结构、排序、过滤、汇总与空态。
4. **接口**：能力状态、成功查询、参数校验、未识别、服务关闭。
5. **对话集成**：非流式与 SSE `done` 携带 `dataCards`，拒绝卡片不阻断问答。
6. **HTTP 数据源**：契约地址/请求头、常见返回形态、上游错误映射。
"""

from __future__ import annotations

import json
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.security import auth, permissions
from app.services import data_query

client = TestClient(app)


def _sse_events(text: str) -> list[dict]:
    events: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        try:
            events.append(json.loads(line[5:].strip()))
        except ValueError:
            continue
    return events


def _query(**payload) -> dict:
    resp = client.post("/api/data/query", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


# ------------------------------------------------------------------ 意图与参数
def test_detect_intent_across_phrasings() -> None:
    assert data_query.detect_intent("本周课表") == "schedule"
    assert data_query.detect_intent("明天有什么课") == "schedule"
    assert data_query.detect_intent("我的成绩单") == "grades"
    assert data_query.detect_intent("上学期 GPA 多少") == "grades"
    assert data_query.detect_intent("一卡通余额") == "card"
    assert data_query.detect_intent("最近 7 天消费流水") == "card"
    assert data_query.detect_intent("校园网怎么开通") is None
    assert data_query.detect_intent("") is None


def test_extract_params_schedule_grades_card() -> None:
    params = data_query._extract_params("第 3 周周一有什么课", "schedule")
    assert params == {"week": 3, "day": "周一"}

    params = data_query._extract_params("星期五的课表", "schedule")
    assert params == {"day": "周五"}

    params = data_query._extract_params(
        "2025-2026 学年第一学期的成绩", "grades"
    )
    assert params == {"semester": "2025-2026学年第一学期"}

    params = data_query._extract_params("最近 7 天消费流水，来 5 条", "card")
    assert params == {"days": 7, "limit": 5}

    params = data_query._extract_params("本月一卡通流水", "card")
    assert 1 <= params["days"] <= 31


def test_semester_and_week_math() -> None:
    label, week = data_query._semester(datetime(2026, 10, 21))
    assert label == "2026-2027 学年第一学期"
    assert week == 7
    label, week = data_query._semester(datetime(2027, 3, 1))
    assert label == "2026-2027 学年第二学期"
    assert week == 2
    label, _week = data_query._semester(datetime(2027, 1, 10))
    assert label == "2026-2027 学年第一学期"

    assert data_query._week_in("1-16", 8) is True
    assert data_query._week_in("2-16", 1) is False
    assert data_query._week_in("1,3,5", 3) is True
    assert data_query._week_in("", 5) is True


# ------------------------------------------------------------------ 身份与权限
def test_demo_mode_identity_by_role() -> None:
    identity, denied = data_query._identity(None, "本科生", "schedule")
    assert denied is None
    assert identity["userNo"] == "20230001"
    assert identity["name"] == "李知远"
    assert identity["authenticated"] is False

    identity, denied = data_query._identity(None, "研究生", "schedule")
    assert denied is None
    assert identity["role"] == "研究生"

    identity, denied = data_query._identity(None, "访客", "schedule")
    assert identity is None
    assert denied["status"] == 403
    assert "教务数据" in denied["error"]

    identity, denied = data_query._identity(None, "教职工", "card")
    assert denied is None
    assert identity["role"] == "教职工"
    assert data_query._identity(None, "离退休", "card")[0]["role"] == "离退休人员"


def test_login_identity_comes_from_token(monkeypatch) -> None:
    monkeypatch.setattr(settings, "auth_enabled", True)
    assert data_query._identity(None, "本科生", "schedule")[1]["status"] == 401

    token = auth.create_token("20240001", "本科生", user_id=2)
    principal = auth_principal(token)
    identity, denied = data_query._identity(principal, "研究生", "schedule")
    assert denied is None
    assert identity["userNo"] == "20240001"
    assert identity["role"] == "本科生"

    teacher = auth_principal(auth.create_token("T10001", "教职工", user_id=3))
    identity, denied = data_query._identity(teacher, None, "grades")
    assert denied["status"] == 403
    identity, denied = data_query._identity(teacher, None, "card")
    assert denied is None
    assert identity["name"] == "陈老师"

    unmapped = auth_principal(auth.create_token("stu-042", "本科生", user_id=4))
    identity, denied = data_query._identity(unmapped, None, "schedule")
    assert denied["status"] == 403
    assert "未在教务" in denied["error"]


def auth_principal(token: str):
    from app.security.deps import get_current_user

    return get_current_user(f"Bearer {token}")


# ------------------------------------------------------------------ 卡片
def test_schedule_card_filters_and_sorts() -> None:
    principal = None
    result = data_query.query(
        "第 3 周周一有什么课", principal, "本科生", intent="schedule"
    )
    assert result["ok"] is True
    card = result["data"]
    assert card["type"] == "schedule"
    assert card["title"] == "周一课表"
    assert "第 3 周" in card["subtitle"]
    assert [row["course"] for row in card["rows"]] == [
        "高等数学 A（二）",
        "大学英语（四）",
    ]
    assert card["summary"][0] == {"label": "课程", "value": "2 门"}
    assert card["identity"]["userNo"] == "20230001"
    assert card["source"] == "mock"

    result = data_query.query("本周课表", principal, "研究生", intent="schedule")
    courses = [row["course"] for row in result["data"]["rows"]]
    assert "高级算法设计" in courses
    assert "高等数学 A（二）" not in courses


def test_schedule_card_empty_when_no_course() -> None:
    result = data_query.query(
        "第 1 周周日的课", None, "本科生", intent="schedule"
    )
    assert result["ok"] is True
    card = result["data"]
    assert card["rows"] == []
    assert card["empty"] is True
    assert card["summary"][0]["value"] == "0 门"


def test_grades_card_statistics() -> None:
    card = data_query.query("我的成绩", None, "本科生", intent="grades")["data"]
    assert card["type"] == "grades"
    assert len(card["rows"]) == 6
    assert card["summary"][0]["value"] == "6 门"
    assert card["summary"][2]["value"] == "89.6"
    assert card["summary"][3]["value"] == "3.96"
    assert card["columns"][0]["key"] == "course"


def test_card_summary_and_limits() -> None:
    card = data_query.query(
        "一卡通余额，最近 7 天来 3 条", None, "本科生", intent="card"
    )["data"]
    assert card["type"] == "card"
    assert card["summary"][0]["emphasis"] is True
    assert card["summary"][0]["value"] == "¥86.50"
    assert len(card["rows"]) == 3
    assert card["subtitle"].startswith("最近 7 天")

    card = data_query.query(
        "一卡通余额", None, "本科生", intent="card", params={"limit": 1}
    )["data"]
    assert len(card["rows"]) == 1


def test_query_rejects_unknown_intent_and_bad_params() -> None:
    result = data_query.query("校园网怎么开通", None, "本科生")
    assert result["ok"] is False
    assert result["status"] == 400

    result = data_query.query(
        "一卡通", None, "本科生", intent="card", params={"limit": "many"}
    )
    assert result["ok"] is False
    assert result["status"] == 400
    assert "整数" in result["error"]


def test_query_disabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "dq_enabled", False)
    result = data_query.query("本周课表", None, "本科生")
    assert result["status"] == 503
    assert data_query.chat_cards("本周课表", None, "本科生") == []


# ------------------------------------------------------------------ 接口
def test_capabilities() -> None:
    data = client.get("/api/data/capabilities").json()["data"]
    assert data["enabled"] is True
    assert data["provider"] == "mock"
    assert data["maxRows"] == 50
    assert [intent["code"] for intent in data["intents"]] == [
        "schedule",
        "grades",
        "card",
    ]
    assert data["intents"][0]["roles"] == ["本科生", "研究生"]
    assert "教职工" in data["intents"][2]["roles"]


def test_api_query_success() -> None:
    data = _query(question="本周课表", role="本科生")
    assert data["type"] == "schedule"
    assert data["rows"]

    data = _query(question="我的成绩", role="研究生")
    assert data["type"] == "grades"
    assert data["identity"]["role"] == "研究生"


def test_api_query_denied_and_invalid() -> None:
    resp = client.post("/api/data/query", json={"question": "本周课表", "role": "访客"})
    assert resp.status_code == 403
    assert "无权" in resp.json()["detail"]

    resp = client.post("/api/data/query", json={"question": "高等数学怎么复习"})
    assert resp.status_code == 400

    resp = client.post(
        "/api/data/query",
        json={"question": "一卡通", "intent": "card", "params": {"days": "x"}},
    )
    assert resp.status_code == 400
    assert "整数" in resp.json()["detail"]

    resp = client.post(
        "/api/data/query", json={"question": "一卡通", "intent": "unknown"}
    )
    assert resp.status_code == 400


def test_api_query_auth_enabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "auth_enabled", True)
    resp = client.post("/api/data/query", json={"question": "本周课表", "role": "本科生"})
    assert resp.status_code == 401

    headers = {
        "Authorization": f"Bearer {auth.create_token('20230001', '本科生', user_id=1)}"
    }
    resp = client.post(
        "/api/data/query", json={"question": "本周课表", "role": "研究生"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["identity"]["role"] == "本科生"

    teacher = {
        "Authorization": f"Bearer {auth.create_token('T10001', '教职工', user_id=2)}"
    }
    resp = client.post(
        "/api/data/query", json={"question": "一卡通余额"}, headers=teacher
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["identity"]["userNo"] == "T10001"

    resp = client.post(
        "/api/data/query", json={"question": "我的成绩"}, headers=teacher
    )
    assert resp.status_code == 403


def test_data_routes_have_no_admin_permission(monkeypatch) -> None:
    assert permissions.required_for("GET", "/api/data/capabilities") is None
    assert permissions.required_for("POST", "/api/data/query") is None
    monkeypatch.setattr(settings, "dq_enabled", False)
    assert client.post("/api/data/query", json={"question": "本周课表"}).status_code == 503


# ------------------------------------------------------------------ 对话集成
def test_chat_includes_data_cards() -> None:
    resp = client.post(
        "/api/chat", json={"question": "本周课表", "role": "本科生"}
    )
    assert resp.status_code == 200, resp.text
    cards = resp.json()["data"]["dataCards"]
    assert cards and cards[0]["type"] == "schedule"
    assert cards[0]["rows"]

    resp = client.post(
        "/api/chat/stream", json={"question": "一卡通余额", "role": "本科生"}
    )
    assert resp.status_code == 200
    done = [e for e in _sse_events(resp.text) if e.get("type") == "done"]
    assert done, resp.text
    assert done[-1]["dataCards"][0]["type"] == "card"

    resp = client.post(
        "/api/chat", json={"question": "校园网怎么开通", "role": "本科生"}
    )
    assert resp.json()["data"]["dataCards"] == []


def test_chat_data_cards_denied_without_break() -> None:
    resp = client.post("/api/chat", json={"question": "我的课表", "role": "访客"})
    assert resp.status_code == 200
    cards = resp.json()["data"]["dataCards"]
    assert cards and cards[0]["type"] == "denied"
    assert cards[0]["status"] == 403
    assert resp.json()["data"]["answer"]

    resp = client.post(
        "/api/chat/stream", json={"question": "我的成绩", "role": "教职工"}
    )
    done = [e for e in _sse_events(resp.text) if e.get("type") == "done"]
    assert done[-1]["dataCards"][0]["type"] == "denied"


# ------------------------------------------------------------------ HTTP 数据源
class _FakeResponse:
    def __init__(self, body, status_code: int = 200) -> None:
        self._body = body
        self.status_code = status_code

    def json(self):
        return self._body


def test_http_provider_contracts(monkeypatch) -> None:
    seen: list[dict] = []

    def fake_get(url, **kwargs):
        seen.append({"url": url, **kwargs})
        if "/schedule" in url:
            return _FakeResponse(
                {
                    "items": [
                        {
                            "day": "周二",
                            "time": "10:00-11:40",
                            "course": "线上课程",
                            "place": "线上",
                            "teacher": "王老师",
                            "weeks": "1-16",
                            "credit": 2,
                        }
                    ]
                }
            )
        if "/grades" in url:
            return _FakeResponse(
                {"data": {"items": [
                    {"course": "线上课程", "type": "必修", "credit": 2, "score": 90, "gpa": 4.0}
                ]}}
            )
        return _FakeResponse(
            {
                "balance": 123.45,
                "transactions": [
                    {"time": "10-20 12:00", "place": "食堂", "type": "消费", "amount": -12.5}
                ],
            }
        )

    monkeypatch.setattr(settings, "dq_provider", "http")
    monkeypatch.setattr(settings, "dq_jw_base_url", "https://jw.example.edu/api")
    monkeypatch.setattr(settings, "dq_jw_token", "jw-token")
    monkeypatch.setattr(settings, "dq_card_base_url", "https://card.example.edu/api")
    monkeypatch.setattr(settings, "dq_card_token", "card-token")
    monkeypatch.setattr(data_query.httpx, "get", fake_get)

    card = data_query.query("本周课表", None, "本科生", intent="schedule")["data"]
    assert card["source"] == "http"
    assert card["rows"][0]["course"] == "线上课程"
    assert seen[0]["url"] == "https://jw.example.edu/api/students/20230001/schedule"
    assert seen[0]["headers"]["Authorization"] == "Bearer jw-token"

    card = data_query.query("我的成绩", None, "本科生", intent="grades")["data"]
    assert card["rows"][0]["score"] == 90
    assert seen[1]["url"].endswith("/students/20230001/grades")

    card = data_query.query("一卡通余额", None, "本科生", intent="card")["data"]
    assert card["summary"][0]["value"] == "¥123.45"
    assert card["rows"][0]["amount"] == "-¥12.50"
    assert seen[2]["url"] == "https://card.example.edu/api/accounts/20230001/card"
    assert seen[2]["headers"]["Authorization"] == "Bearer card-token"
    assert seen[2]["params"] == {"days": 30, "limit": 10}


def test_http_provider_errors(monkeypatch) -> None:
    monkeypatch.setattr(settings, "dq_provider", "http")
    monkeypatch.setattr(settings, "dq_jw_base_url", "")
    resp = client.post(
        "/api/data/query", json={"question": "本周课表", "role": "本科生"}
    )
    assert resp.status_code == 503
    assert "DQ_JW_BASE_URL" in resp.json()["detail"]

    monkeypatch.setattr(settings, "dq_jw_base_url", "https://jw.example.edu")
    monkeypatch.setattr(
        data_query.httpx, "get", lambda *a, **k: _FakeResponse({}, status_code=500)
    )
    resp = client.post(
        "/api/data/query", json={"question": "我的成绩", "role": "本科生"}
    )
    assert resp.status_code == 502
    assert "500" in resp.json()["detail"]

    def boom(*_a, **_k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(data_query.httpx, "get", boom)
    result = data_query.query("本周课表", None, "本科生", intent="schedule")
    assert result["status"] == 502
    assert "不可达" in result["error"]


def test_extract_items_common_shapes() -> None:
    rows = [{"course": "A"}]
    assert data_query._extract_items(rows) == rows
    assert data_query._extract_items({"items": rows}) == rows
    assert data_query._extract_items({"data": rows}) == rows
    assert data_query._extract_items({"data": {"items": rows}}) == rows
    assert data_query._extract_items({"unexpected": 1}) == []


# ------------------------------------------------------------------ 能力映射
def test_chat_cards_silent_on_no_intent_or_bad_params(monkeypatch) -> None:
    assert data_query.chat_cards("校园网怎么开通", None, "本科生") == []
    result = data_query.query(
        "课表", None, "本科生", intent="schedule", params={"week": "第一周"}
    )
    assert result["status"] == 400
