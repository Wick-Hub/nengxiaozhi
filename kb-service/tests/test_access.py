"""通行查询智能体测试（任务 4.4 / P-DQ-04、I-04）。

覆盖：

1. **mock 流水**：确定性生成、事件号唯一、深夜自动标记异常。
2. **同步**：整窗替换、重复同步幂等、缩短窗口后旧数据不残留。
3. **聚合**：总量/方向/校区/学院/门禁点/逐日/24 小时口径互相对齐。
4. **筛选与下钻**：校区/学院/门禁点/方向/异常/关键词；缺参数与非法维度的报错。
5. **明细**：分页、倒序、学工号脱敏。
6. **报告与导出**：Markdown 章节与隐私说明、下载头、CSV BOM/脱敏/截断头。
7. **HTTP 数据源**：Bearer、from/to 参数、字段归一化、去重、失败不覆盖本地库。
8. **鉴权与 RBAC**：未登录 401、普通用户 403、联络员缺权限点 403、管理员放行。
9. **页面契约**：/access 及静态资源、后台入口、权限点映射。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db import access_store
from app.main import app
from app.security import auth, permissions
from app.services import access

client = TestClient(app)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def _sync(days: int = 7) -> dict:
    resp = client.post("/api/admin/access/sync", params={"days": days})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _window(days: int = 7) -> tuple[date, date]:
    today = date.today()
    return today - timedelta(days=days - 1), today


def _overview(**params) -> dict:
    resp = client.get("/api/admin/access/overview", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


# ------------------------------------------------------------------ mock
def test_mock_events_are_deterministic_and_flagged() -> None:
    end = date(2025, 1, 15)
    first = access.mock_events(days=3, end=end)
    second = access.mock_events(days=3, end=end)

    assert first == second
    assert len(first) == 336
    assert len({event["eventNo"] for event in first}) == len(first)
    assert {event["eventTime"][:10] for event in first} == {
        "2025-01-13",
        "2025-01-14",
        "2025-01-15",
    }
    assert all(event["direction"] in ("in", "out") for event in first)
    assert {event["campusName"] for event in first} == {"主校区", "东校区", "南校区"}
    assert all(event["gateId"] in access.GATE_NAMES for event in first)

    night = [event for event in first if event["abnormalReason"] == access.NIGHT_REASON]
    assert len(night) == 11
    assert all(event["abnormal"] for event in night)
    assert all(
        event["eventTime"][11:13] >= "23" or event["eventTime"][11:13] < "06"
        for event in night
    )


# ------------------------------------------------------------------ 同步
def test_sync_is_idempotent_and_replaces_window() -> None:
    data = _sync(7)
    start, end = _window(7)

    assert data["provider"] == "mock"
    assert data["imported"] > 0 and data["skipped"] == 0
    assert data["from"] == start.isoformat() and data["to"] == end.isoformat()
    meta = access_store.sync_meta()
    assert meta["totalEvents"] == data["imported"]
    assert meta["provider"] == "mock"
    assert meta["syncedAt"]

    again = _sync(7)
    assert again["imported"] == data["imported"]
    assert access_store.sync_meta()["totalEvents"] == data["imported"]

    small = _sync(3)
    assert small["imported"] < data["imported"]
    assert access_store.sync_meta()["totalEvents"] == small["imported"]


# ------------------------------------------------------------------ 聚合
def test_overview_aggregates_match_local_events() -> None:
    data = _sync(7)
    start, end = _window(7)
    view = _overview(dateFrom=start.isoformat(), dateTo=end.isoformat())
    summary = view["summary"]

    assert summary["total"] == data["imported"]
    assert summary["entries"] + summary["exits"] == summary["total"]
    assert sum(row["total"] for row in view["byCampus"]) == summary["total"]
    assert sum(row["total"] for row in view["byCollege"]) == summary["total"]
    assert sum(row["total"] for row in view["byGate"]) == summary["total"]
    assert sum(row["total"] for row in view["hourly"]) == summary["total"]
    assert sum(row["total"] for row in view["trend"]) == summary["total"]
    assert len(view["trend"]) == 7
    assert view["trend"][-1]["date"] == end.isoformat()
    assert len(view["hourly"]) == 24

    assert summary["abnormal"] == view["abnormal"]["total"]
    assert summary["night"] == view["abnormal"]["night"]
    assert len(view["topPersons"]) <= 10
    assert len(view["topColleges"]) <= 5
    assert [row["rank"] for row in view["topPersons"]] == list(
        range(1, len(view["topPersons"]) + 1)
    )
    assert all("*" in row["userNoMasked"] for row in view["topPersons"])
    assert all("*" in row["userNoMasked"] for row in view["abnormal"]["recent"])
    assert summary["peakHour"] is not None
    assert summary["peakHour"]["total"] == max(row["total"] for row in view["hourly"])

    meta = view["meta"]
    assert meta["provider"] == "mock"
    assert meta["configuredProvider"] == "mock"
    assert meta["totalEvents"] == summary["total"]
    assert meta["syncDays"] == settings.access_sync_days

    options = view["options"]
    assert len(options["campuses"]) >= 3
    assert len(options["gates"]) == len(access.GATES)
    assert "计算机学院" in options["colleges"]
    assert "本科生" in options["roles"]


# ------------------------------------------------------------------ 筛选 / 下钻
def test_filters_and_drilldown_scope_correctly() -> None:
    _sync(7)
    view = _overview()

    campus = view["byCampus"][0]
    by_campus = _overview(campusId=campus["campusId"])
    assert by_campus["summary"]["total"] == campus["total"]
    assert len(by_campus["byCampus"]) == 1

    gate = view["byGate"][0]
    by_gate = _overview(gateId=gate["gateId"])
    assert by_gate["summary"]["total"] == gate["total"]
    assert {row["gateId"] for row in by_gate["byGate"]} == {gate["gateId"]}

    college = view["byCollege"][0]
    by_college = _overview(college=college["collegeName"])
    assert by_college["summary"]["total"] == college["total"]

    entries = _overview(direction="in")
    assert entries["summary"]["total"] == view["summary"]["entries"]
    assert entries["summary"]["exits"] == 0

    abnormal = _overview(abnormal="true")
    assert abnormal["summary"]["total"] == view["summary"]["abnormal"]
    assert abnormal["summary"]["abnormal"] == abnormal["summary"]["total"]
    assert abnormal["summary"]["night"] <= abnormal["summary"]["total"]

    person = view["topPersons"][0]
    hit = _overview(q=person["userName"])
    assert 0 < hit["summary"]["total"] <= view["summary"]["total"]

    resp = client.get(
        "/api/admin/access/drilldown",
        params={"dimension": "campus", "campusId": campus["campusId"]},
    )
    assert resp.status_code == 200
    campus_drill = resp.json()["data"]
    assert campus_drill["dimension"] == "campus"
    assert campus_drill["summary"]["total"] == campus["total"]
    assert campus_drill["title"] == campus["campusName"]

    resp = client.get(
        "/api/admin/access/drilldown",
        params={"dimension": "gate", "gateId": gate["gateId"]},
    )
    assert resp.status_code == 200
    gate_drill = resp.json()["data"]
    assert gate_drill["title"] == gate["gateName"]
    assert gate_drill["summary"]["total"] == gate["total"]

    resp = client.get(
        "/api/admin/access/drilldown",
        params={"dimension": "college", "college": college["collegeName"]},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["title"] == college["collegeName"]

    assert (
        client.get("/api/admin/access/drilldown", params={"dimension": "campus"}).status_code
        == 400
    )
    assert (
        client.get("/api/admin/access/drilldown", params={"dimension": "college"}).status_code
        == 400
    )
    assert (
        client.get("/api/admin/access/drilldown", params={"dimension": "gate"}).status_code
        == 400
    )
    assert (
        client.get("/api/admin/access/drilldown", params={"dimension": "building"}).status_code
        == 422
    )
    assert access.drilldown("building")["ok"] is False


def test_filter_validation_rejects_bad_input() -> None:
    _sync(2)
    today = date.today()
    yesterday = today - timedelta(days=1)
    too_wide = today - timedelta(days=settings.access_max_range_days)

    cases = [
        {"dateFrom": "2026/01/01"},
        {"dateFrom": today.isoformat(), "dateTo": yesterday.isoformat()},
        {"dateFrom": too_wide.isoformat(), "dateTo": today.isoformat()},
        {"campusId": -1},
        {"direction": "sideways"},
        {"abnormal": "perhaps"},
    ]
    for params in cases:
        resp = client.get("/api/admin/access/overview", params=params)
        assert resp.status_code in (400, 422), (params, resp.text)

    assert access.overview(direction="sideways")["ok"] is False
    assert access.overview(date_from="2026-13-01")["ok"] is False


# ------------------------------------------------------------------ 明细
def test_event_pagination_mask_and_order() -> None:
    data = _sync(7)
    first = client.get(
        "/api/admin/access/events", params={"page": 1, "pageSize": 5}
    ).json()["data"]

    assert first["total"] == data["imported"]
    assert first["page"] == 1 and first["pageSize"] == 5
    assert len(first["items"]) == 5
    times = [row["eventTime"] for row in first["items"]]
    assert times == sorted(times, reverse=True)
    assert all("*" in row["userNoMasked"] for row in first["items"])
    assert all(row["directionLabel"] in ("进入", "离开") for row in first["items"])

    second = client.get(
        "/api/admin/access/events", params={"page": 2, "pageSize": 5}
    ).json()["data"]
    assert {
        row["eventId"] for row in first["items"]
    }.isdisjoint({row["eventId"] for row in second["items"]})

    beyond = client.get(
        "/api/admin/access/events", params={"page": 99999, "pageSize": 20}
    ).json()["data"]
    assert beyond["items"] == []
    assert beyond["total"] == first["total"]

    assert (
        client.get("/api/admin/access/events", params={"page": 0}).status_code == 422
    )
    assert (
        client.get("/api/admin/access/events", params={"pageSize": 201}).status_code
        == 422
    )


# ------------------------------------------------------------------ 报告 / 导出
def test_report_preview_and_download_are_masked() -> None:
    _sync(7)
    start, end = _window(7)
    params = {"dateFrom": start.isoformat(), "dateTo": end.isoformat()}

    resp = client.get("/api/admin/access/report", params=params)
    assert resp.status_code == 200, resp.text
    report = resp.json()["data"]
    assert report["filename"].startswith("通行分析报告-")
    assert report["filename"].endswith(".md")
    assert report["window"] == {"from": start.isoformat(), "to": end.isoformat(), "days": 7}

    content = report["content"]
    for marker in (
        "# 校园通行智能分析报告",
        "## 一、总体概况",
        "## 二、校区分布",
        "## 三、学院通行 TOP5",
        "## 四、通行频次 TOP10",
        "## 五、异常通行",
        "## 六、趋势与高峰",
        "## 七、结论与建议",
        "隐私说明",
        "未向外部模型发送个人明细",
    ):
        assert marker in content, marker
    assert "20230001" not in content

    download = client.get(
        "/api/admin/access/report", params={**params, "download": "true"}
    )
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("text/markdown")
    assert "attachment" in download.headers["content-disposition"]
    assert "# 校园通行智能分析报告" in download.text
    assert "20230001" not in download.text


def test_export_csv_masks_and_reports_truncation(monkeypatch) -> None:
    _sync(7)
    monkeypatch.setattr(settings, "access_export_limit", 5)

    resp = client.get("/api/admin/access/export")
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-export-truncated"] == "true; limit=5"
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.headers["content-type"].startswith("text/csv")

    text = resp.content.decode("utf-8-sig")
    lines = text.strip().splitlines()
    assert lines[0].startswith("时间,方向,校区,学院,门禁点,姓名,学工号,身份,异常,异常原因")
    assert len(lines) == 6
    assert "****" in text
    assert "20230001" not in text

    monkeypatch.setattr(settings, "access_export_limit", 10000)
    full = client.get("/api/admin/access/export")
    assert full.status_code == 200
    assert "x-export-truncated" not in full.headers


# ------------------------------------------------------------------ HTTP 数据源
class _FakeResponse:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _stub_http(monkeypatch, payload, captured: dict, *, status_code: int = 200) -> None:
    def fake_get(url, headers=None, timeout=None, follow_redirects=False):
        captured.update(
            url=url,
            headers=headers or {},
            timeout=timeout,
            follow_redirects=follow_redirects,
        )
        return _FakeResponse(payload, status_code=status_code)

    monkeypatch.setattr(access.httpx, "get", fake_get)


def test_http_sync_normalizes_dedupes_and_sends_bearer(monkeypatch) -> None:
    yesterday = date.today() - timedelta(days=1)
    payload = {
        "items": [
            {
                "eventNo": "HW-1",
                "userNo": "20231234",
                "userName": "张三",
                "eventTime": f"{yesterday} 08:00:00",
                "direction": "in",
                "campusId": 2,
                "gateId": 5,
                "collegeName": "计算机学院",
                "role": "本科生",
            },
            {
                "eventNo": "HW-1",
                "userNo": "20231234",
                "userName": "张三",
                "eventTime": f"{yesterday} 08:01:00",
                "direction": "in",
            },
            {"userNo": "missing-time", "eventTime": "bad", "direction": "in"},
            {
                "eventNo": "HW-2",
                "userNo": "T10001",
                "userName": "李四",
                "eventTime": f"{yesterday} 23:30:00",
                "direction": 0,
                "campusName": "主校区",
                "gateName": "南门",
                "abnormal": False,
            },
        ]
    }
    captured: dict = {}
    _stub_http(monkeypatch, payload, captured)
    monkeypatch.setattr(settings, "access_sync_provider", "http")
    monkeypatch.setattr(
        settings, "access_sync_url", "https://access.example.edu/open/events?scope=campus"
    )
    monkeypatch.setattr(settings, "access_sync_token", "unit-test-token")

    result = access.sync_events(days=2)
    assert result["ok"], result
    data = result["data"]
    assert data["provider"] == "http"
    assert data["total"] == 4
    assert data["imported"] == 2
    assert data["skipped"] == 2

    assert captured["headers"]["Authorization"] == "Bearer unit-test-token"
    assert captured["headers"]["Accept"] == "application/json"
    assert captured["url"].startswith(
        "https://access.example.edu/open/events?scope=campus&"
    )
    assert "from=" in captured["url"] and "to=" in captured["url"]
    assert captured["follow_redirects"] is True

    view = access.overview(
        date_from=yesterday.isoformat(), date_to=yesterday.isoformat()
    )["data"]
    assert view["summary"]["total"] == 2
    assert view["summary"]["entries"] == 1
    assert view["summary"]["exits"] == 1
    assert view["summary"]["abnormal"] == 1

    stored = access_store.list_events({})
    night = next(event for event in stored if event["eventNo"] == "HW-2")
    assert night["abnormal"] is True
    assert night["abnormalReason"] == access.NIGHT_REASON
    assert night["campusId"] == 1
    assert night["campusName"] == "主校区"
    assert night["gateName"] == "南门"
    assert night["role"] == "访客"


def test_http_failures_keep_local_data(monkeypatch) -> None:
    seeded = _sync(3)
    before = access_store.sync_meta()["totalEvents"]
    assert before == seeded["imported"]

    monkeypatch.setattr(settings, "access_sync_provider", "http")
    monkeypatch.setattr(settings, "access_sync_url", "https://access.example.edu/events")

    cases = [
        ({"items": []}, 200, "未返回有效流水"),
        ({"msg": "oops"}, 200, "格式不正确"),
        ({"items": [{"userNo": "x"}]}, 200, "未返回有效流水"),
        (ValueError("not json"), 200, "不是 JSON"),
        ({"items": []}, 500, "HTTP 500"),
    ]
    for payload, status_code, needle in cases:
        captured: dict = {}
        _stub_http(monkeypatch, payload, captured, status_code=status_code)
        result = access.sync_events(days=3)
        assert result["ok"] is False, (payload, result)
        assert result["status"] == 502
        assert needle in result["error"], (needle, result["error"])
        assert access_store.sync_meta()["totalEvents"] == before

    monkeypatch.setattr(settings, "access_sync_url", "")
    missing = access.sync_events(days=3)
    assert missing["ok"] is False
    assert "ACCESS_SYNC_URL" in missing["error"]

    monkeypatch.setattr(settings, "access_sync_url", "https://access.example.edu/events")

    def timeout_get(*args, **kwargs):
        raise httpx.TimeoutException("boom")

    monkeypatch.setattr(access.httpx, "get", timeout_get)
    timed_out = access.sync_events(days=3)
    assert timed_out["ok"] is False
    assert "超时" in timed_out["error"]
    assert access_store.sync_meta()["totalEvents"] == before


# ------------------------------------------------------------------ 鉴权 / RBAC
def test_access_requires_admin_and_maps_rbac(monkeypatch) -> None:
    _sync(2)
    monkeypatch.setattr(settings, "auth_enabled", True)

    assert client.get("/api/admin/access/overview").status_code == 401
    assert client.get("/api/admin/access/export").status_code == 401
    assert client.post("/api/admin/access/sync").status_code == 401

    user_headers = {
        "Authorization": f"Bearer {auth.create_token('20230001', '本科生', user_id=1)}"
    }
    assert client.get("/api/admin/access/overview", headers=user_headers).status_code == 403

    liaison_headers = {
        "Authorization": f"Bearer {auth.create_token('D001', '部门联络员', user_id=2)}"
    }
    denied = client.get("/api/admin/access/overview", headers=liaison_headers)
    assert denied.status_code == 403
    assert "access.view" in denied.json()["detail"]

    admin_headers = {
        "Authorization": f"Bearer {auth.create_token('admin', '管理员', user_id=1)}"
    }
    assert client.get("/api/admin/access/overview", headers=admin_headers).status_code == 200
    assert (
        client.get("/api/admin/access/events", headers=admin_headers).status_code == 200
    )
    assert (
        client.post(
            "/api/admin/access/sync", params={"days": 2}, headers=admin_headers
        ).status_code
        == 200
    )
    assert client.get("/api/admin/access/export", headers=admin_headers).status_code == 200
    assert client.get("/api/admin/access/report", headers=admin_headers).status_code == 200

    assert permissions.required_for("GET", "/api/admin/access/overview") == "access.view"
    assert permissions.required_for("GET", "/api/admin/access/drilldown") == "access.view"
    assert permissions.required_for("GET", "/api/admin/access/events") == "access.view"
    assert permissions.required_for("POST", "/api/admin/access/sync") == "access.manage"
    assert permissions.required_for("GET", "/api/admin/access/export") == "access.export"
    assert permissions.required_for("GET", "/api/admin/access/report") == "access.export"


# ------------------------------------------------------------------ 页面契约
def test_access_page_and_entry_contract() -> None:
    page = client.get("/access")
    assert page.status_code == 200
    assert "access.js" in page.text and "access.css" in page.text
    assert client.get("/access.js").status_code == 200
    assert client.get("/access.css").status_code == 200

    admin_html = (STATIC_DIR / "admin.html").read_text(encoding="utf-8")
    assert 'href="/access"' in admin_html
    assert 'data-perm="access.view"' in admin_html

    auth_js = (STATIC_DIR / "auth.js").read_text(encoding="utf-8")
    assert "'/access'" in auth_js and "access.view" in auth_js

    page_js = (STATIC_DIR / "access.js").read_text(encoding="utf-8")
    for token in (
        "/api/admin/access/overview",
        "/api/admin/access/drilldown",
        "/api/admin/access/events",
        "/api/admin/access/sync",
        "/api/admin/access/export",
        "/api/admin/access/report",
        "textContent",
    ):
        assert token in page_js, token
    assert ".innerHTML" not in page_js
