"""数据中台对接测试（任务 4.2 / I-02、M-SYS-02）。

覆盖：

1. **组织/用户同步**：mock 确定性数据、按编码幂等 upsert、全量缺项只统计不删除、
   非法行跳过且不影响其余记录；
2. **上报批次**：同一批次相同内容重复执行只跳过、内容变化升 revision、
   强制重投不升版本、mock 收件箱落盘；
3. **隐私**：默认不带问答正文；开启后先脱敏再截断；
4. **接口契约**：状态/预览/上报/同步/批次/镜像端点，非法日期与未知批次；
5. **定时水位**：每日上报、每日增量、每周全量只跑一次；
6. **HTTP 数据源**：Bearer、查询参数、上报请求头、失败落 failed 并可重试；
7. **权限与页面**：datacenter.view/manage 映射、/data-center 页面契约。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db import chatlog, data_center_store as store
from app.main import app
from app.security import permissions
from app.services import data_center

client = TestClient(app)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
TODAY = date.today().isoformat()


@dataclass
class _Gen:
    answer: str = "请携带校园卡前往一卡通中心办理。"
    citations: list = field(default_factory=lambda: [{"n": 1, "chunkId": "d1::0"}])
    confidence: float = 0.72
    refused: bool = False


def _seed(count: int = 1, session: str = "S-DC-1") -> None:
    for i in range(count):
        chatlog.save_qa(
            f"数据中台上报测试问题{i}",
            "本科生",
            _Gen(answer=f"数据中台上报测试回答{i}"),
            session,
        )


def _run_report(kind: str = "all", **kwargs) -> dict:
    result = data_center.run_report(TODAY, kind, **kwargs)
    assert result["ok"], result
    return result["data"]


def _sink(name: str) -> dict:
    return json.loads((store.SINK_DIR / f"{name}.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------------ 同步
def test_mock_sync_is_deterministic_and_idempotent() -> None:
    first = data_center.run_sync("incremental")
    assert first["ok"], first
    data = first["data"]
    assert data["orgs"] == {"fetched": 6, "created": 6, "updated": 0}
    assert data["users"] == {"fetched": 4, "created": 4, "updated": 0}
    assert data["cursor"] == "2026-09-01T08:05:00"
    assert data["mirror"] == {"orgs": 6, "users": 4, "lastSyncedAt": data["mirror"]["lastSyncedAt"]}
    assert data["mirror"]["lastSyncedAt"]
    assert data["invalid"] == 0
    assert data["since"] == ""
    assert store.get_state("orgCursor") == "2026-09-01T08:05:00"

    second = data_center.run_sync("incremental")
    assert second["ok"], second
    assert second["data"]["orgs"] == {"fetched": 0, "created": 0, "updated": 0}
    assert second["data"]["users"] == {"fetched": 0, "created": 0, "updated": 0}
    assert len(store.list_orgs()) == 6
    assert len(store.list_users()) == 4

    full = data_center.run_sync("full")
    assert full["ok"], full
    assert full["data"]["orgs"] == {"fetched": 6, "created": 0, "updated": 0}
    assert full["data"]["users"] == {"fetched": 4, "created": 0, "updated": 0}
    assert full["data"]["missing"]["orgs"] == 0
    assert full["data"]["missing"]["users"] == 0
    assert "只统计不删除" in full["data"]["missing"]["policy"]


def test_full_sync_updates_changed_rows(monkeypatch) -> None:
    assert data_center.run_sync("full")["ok"]
    changed = [dict(org) for org in data_center.MOCK_ORGS]
    changed[1]["orgName"] = "教务处（更名）"
    monkeypatch.setattr(data_center, "MOCK_ORGS", tuple(changed))

    result = data_center.run_sync("full")
    assert result["ok"], result
    assert result["data"]["orgs"] == {"fetched": 6, "created": 0, "updated": 1}
    names = {o["orgCode"]: o["orgName"] for o in store.list_orgs()}
    assert names["JW"] == "教务处（更名）"


def test_normalizers_skip_invalid_rows() -> None:
    orgs, bad_orgs = data_center._normalize_orgs(
        [
            {"orgCode": "A", "orgName": "甲", "sortNo": "3", "status": 0},
            {"orgCode": "", "orgName": "缺编码"},
            {"orgCode": "B", "sortNo": "not-a-number"},
            "not-a-dict",
        ]
    )
    assert len(orgs) == 1 and bad_orgs == 3
    assert orgs[0] == {
        "orgCode": "A",
        "orgName": "甲",
        "parentCode": "",
        "orgPath": "",
        "sortNo": 3,
        "status": 0,
        "updatedAt": "",
    }

    users, bad_users = data_center._normalize_users(
        [
            {"userNo": "20230001", "realName": "张三", "idType": 9, "status": 1},
            {"userNo": " ", "realName": "无编码"},
            {"userNo": "X", "idType": "abc"},
        ]
    )
    assert len(users) == 1 and bad_users == 2
    assert users[0]["idType"] == 1
    assert users[0]["status"] == 1


# ------------------------------------------------------------------ 上报
def test_report_is_idempotent_and_bumps_revision_on_change() -> None:
    _seed(1)
    first = _run_report()
    assert [b["kind"] for b in first["batches"]] == ["chatlog", "stats"]
    assert all(b["status"] == "delivered" and not b["skipped"] for b in first["batches"])
    assert all(b["rows"] == 1 for b in first["batches"])
    chatlog_batch = f"DC-CHATLOG-{TODAY.replace('-', '')}"
    stats_batch = f"DC-STATS-{TODAY.replace('-', '')}"
    assert first["batches"][0]["batchId"] == chatlog_batch
    assert first["batches"][1]["batchId"] == stats_batch
    assert _sink(chatlog_batch)["counts"] == {"records": 1}
    assert _sink(stats_batch)["statistics"]["questions"] == 1
    assert not first["truncated"]

    skipped = data_center.run_report(TODAY, "all")
    assert skipped["ok"], skipped
    assert all(b["skipped"] for b in skipped["data"]["batches"])
    assert all(b["status"] == "delivered" for b in skipped["data"]["batches"])
    assert all(b["revision"] == 1 for b in skipped["data"]["batches"])

    _seed(1, session="S-DC-2")
    changed = data_center.run_report(TODAY, "all")
    assert changed["ok"], changed
    assert all(b["revision"] == 2 and not b["skipped"] for b in changed["data"]["batches"])
    assert _sink(chatlog_batch)["revision"] == 2
    assert _sink(chatlog_batch)["counts"] == {"records": 2}

    forced = data_center.run_report(TODAY, "all", force=True)
    assert forced["ok"], forced
    assert all(b["revision"] == 2 and not b["skipped"] for b in forced["data"]["batches"])

    assert len(store.list_jobs()) == 4
    assert all(job["status"] == "success" for job in store.list_jobs())
    assert store.get_state("lastReportAt")


def test_preview_does_not_deliver_or_record() -> None:
    _seed(2)
    data = data_center.preview_report(TODAY, "all")
    assert data["ok"], data
    assert data["data"]["schema"] == data_center.SCHEMA
    assert data["data"]["includeContent"] is False
    assert [item["kind"] for item in data["data"]["items"]] == ["chatlog", "stats"]
    assert data["data"]["items"][0]["rows"] == 2
    assert len(data["data"]["items"][0]["sample"]) == 2
    assert data["data"]["items"][1]["statistics"]["questions"] == 2
    assert not store.list_reports()
    assert not store.list_jobs()
    assert not store.SINK_DIR.exists() or not list(store.SINK_DIR.iterdir())


def test_report_privates_content_by_default_and_masks_when_enabled(monkeypatch) -> None:
    chatlog.save_qa(
        "我的学号是20230001，手机号13812345678",
        "本科生",
        _Gen(answer="证件号110101199001011234，邮箱 zhangsan@example.edu.cn。" + "长" * 600),
        "S-DC-PRIV",
    )
    _run_report("chatlog")
    batch = f"DC-CHATLOG-{TODAY.replace('-', '')}"
    record = _sink(batch)["records"][0]
    assert "question" not in record and "answer" not in record
    assert _sink(batch)["privacy"]["contentIncluded"] is False

    monkeypatch.setattr(settings, "data_center_include_content", True)
    result = data_center.run_report(TODAY, "chatlog")
    assert result["ok"], result
    payload = _sink(batch)
    assert payload["privacy"]["contentIncluded"] is True
    record = payload["records"][0]
    assert "20230001" not in record["question"]
    assert "13812345678" not in record["question"]
    assert "****" in record["question"]
    assert "110101199001011234" not in record["answer"]
    assert "zhangsan@example.edu.cn" not in record["answer"]
    assert "z***@example.edu.cn" in record["answer"]
    assert len(record["answer"]) == data_center.CONTENT_LIMIT


def test_max_records_truncates_report(monkeypatch) -> None:
    _seed(3)
    monkeypatch.setattr(settings, "data_center_max_records", 2)
    data = _run_report("chatlog")
    assert data["truncated"] is True
    assert data["batches"][0]["rows"] == 2


def test_delivery_failure_is_recorded_and_retry_succeeds(monkeypatch) -> None:
    _seed(1)
    original = data_center._deliver

    def boom(_payload):
        raise RuntimeError("中台不可达")

    monkeypatch.setattr(data_center, "_deliver", boom)
    failed = data_center.run_report(TODAY, "chatlog")
    assert failed["ok"] is False
    assert failed["status"] == 502
    batch = f"DC-CHATLOG-{TODAY.replace('-', '')}"
    saved = store.get_report(batch)
    assert saved["status"] == "failed"
    assert "中台不可达" in saved["error"]
    job = store.list_jobs(1)[0]
    assert job["status"] == "failed"
    assert job["failed"] == 1

    monkeypatch.setattr(data_center, "_deliver", original)
    retried = data_center.run_report(TODAY, "chatlog")
    assert retried["ok"], retried
    assert retried["data"]["batches"][0]["revision"] == 1
    assert store.get_report(batch)["status"] == "delivered"
    assert (store.SINK_DIR / f"{batch}.json").is_file()


# ------------------------------------------------------------------ 定时
def test_run_due_respects_watermarks_and_weekday(monkeypatch) -> None:
    monkeypatch.setattr(settings, "data_center_full_weekday", 2)  # 周三全量
    normal = datetime(2026, 10, 14, 1, 0)  # 周三 01:00，尚未到上报/增量时间
    assert data_center.run_due(normal)["executed"] == []

    morning = datetime(2026, 10, 14, 5, 0)
    first = data_center.run_due(morning)
    assert first["ok"], first
    assert [item["job"] for item in first["executed"]] == [
        "report",
        "org_incremental",
        "org_full",
    ]
    assert all(item["ok"] for item in first["executed"])
    assert store.get_state("lastReportDate") == "2026-10-14"
    assert store.get_state("lastIncrementalDate") == "2026-10-14"
    assert store.get_state("lastFullDate") == "2026-10-14"

    assert data_center.run_due(morning)["executed"] == []

    monkeypatch.setattr(settings, "data_center_full_weekday", 4)  # 周五才全量
    thursday = datetime(2026, 10, 15, 5, 0)
    second = data_center.run_due(thursday)
    assert [item["job"] for item in second["executed"]] == ["report", "org_incremental"]
    assert store.get_state("lastFullDate") == "2026-10-14"
    assert len(store.list_jobs()) == 5


# ------------------------------------------------------------------ HTTP 数据源
class _FakeResponse:
    def __init__(self, payload, status: int = 200) -> None:
        self._payload = payload
        self.status = status

    def read(self) -> bytes:
        if isinstance(self._payload, Exception):
            raise self._payload
        return json.dumps(self._payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


def test_http_source_sends_auth_and_query(monkeypatch) -> None:
    captured: list[dict] = []

    def fake_urlopen(request, timeout=None):
        captured.append(
            {
                "method": request.get_method(),
                "url": request.full_url,
                "headers": dict(request.headers),
                "body": request.data,
                "timeout": timeout,
            }
        )
        if request.get_method() == "POST":
            return _FakeResponse({"ok": True})
        return _FakeResponse(
            {
                "data": {
                    "orgs": [
                        {
                            "orgCode": "XUST",
                            "orgName": "西安科技大学",
                            "orgPath": "/XUST/",
                            "sortNo": 1,
                            "status": 1,
                            "updatedAt": "2026-10-01T00:00:00",
                        }
                    ],
                    "users": [
                        {
                            "userNo": "20239999",
                            "realName": "测试生",
                            "idType": 1,
                            "orgCode": "XUST",
                            "status": 1,
                            "updatedAt": "2026-10-01T00:00:00",
                        }
                    ],
                }
            }
        )

    monkeypatch.setattr(settings, "data_center_provider", "http")
    monkeypatch.setattr(settings, "data_center_base_url", "https://dc.example.edu/api/")
    monkeypatch.setattr(settings, "data_center_token", "unit-test-token")
    monkeypatch.setattr(data_center.urllib.request, "urlopen", fake_urlopen)
    store.set_state("orgCursor", "2026-09-01T08:05:00")

    synced = data_center.run_sync("incremental")
    assert synced["ok"], synced
    assert synced["data"]["orgs"]["created"] == 1
    assert synced["data"]["users"]["created"] == 1
    assert synced["data"]["cursor"] == "2026-10-01T00:00:00"
    fetch = captured[0]
    assert fetch["method"] == "GET"
    assert fetch["url"].startswith("https://dc.example.edu/api/org/export?mode=incremental")
    assert "since=2026-09-01T08%3A05%3A00" in fetch["url"]
    assert fetch["headers"]["Authorization"] == "Bearer unit-test-token"

    _seed(1)
    reported = data_center.run_report(TODAY, "chatlog")
    assert reported["ok"], reported
    post = captured[1]
    assert post["method"] == "POST"
    assert post["url"] == "https://dc.example.edu/api/report"
    assert post["headers"]["Authorization"] == "Bearer unit-test-token"
    assert post["headers"]["X-data-standard"] == data_center.SCHEMA
    body = json.loads(post["body"].decode("utf-8"))
    assert body["schema"] == data_center.SCHEMA
    assert reported["data"]["batches"][0]["location"] == "https://dc.example.edu/api/report"


# ------------------------------------------------------------------ 接口契约
def test_admin_api_contract() -> None:
    status = client.get("/api/admin/data-center/status")
    assert status.status_code == 200, status.text
    data = status.json()["data"]
    assert data["provider"] == "mock"
    assert data["schema"] == data_center.SCHEMA
    assert data["mirror"]["orgs"] == 0
    assert data["sync"]["modes"] == ["incremental", "full"]

    synced = client.post("/api/admin/data-center/sync", json={"mode": "full"})
    assert synced.status_code == 200, synced.text
    assert synced.json()["data"]["orgs"]["fetched"] == 6

    preview = client.post(
        "/api/admin/data-center/preview", json={"date": TODAY, "kind": "stats"}
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["data"]["items"][0]["kind"] == "stats"

    _seed(2)
    reported = client.post(
        "/api/admin/data-center/report", json={"date": TODAY, "kind": "all"}
    )
    assert reported.status_code == 200, reported.text
    assert len(reported.json()["data"]["batches"]) == 2

    reports = client.get("/api/admin/data-center/reports").json()["data"]["items"]
    assert len(reports) == 2
    batch = reports[0]["batchId"]
    detail = client.get(f"/api/admin/data-center/reports/{batch}")
    assert detail.status_code == 200
    assert detail.json()["data"]["batchId"] == batch
    assert client.get("/api/admin/data-center/reports/DC-NOPE").status_code == 404

    jobs = client.get("/api/admin/data-center/jobs").json()["data"]["items"]
    assert len(jobs) == 2
    assert {job["jobType"] for job in jobs} == {"org_sync", "report"}

    orgs = client.get("/api/admin/data-center/orgs").json()["data"]["items"]
    users = client.get("/api/admin/data-center/users").json()["data"]["items"]
    assert len(orgs) == 6 and orgs[0]["orgCode"] == "XUST"
    assert len(users) == 4 and users[0]["userNo"] == "20210002"

    bad_date = client.post(
        "/api/admin/data-center/report", json={"date": "2026-13-01", "kind": "all"}
    )
    assert bad_date.status_code == 400
    future = (date.today() + timedelta(days=1)).isoformat()
    future_resp = client.post(
        "/api/admin/data-center/report", json={"date": future, "kind": "all"}
    )
    assert future_resp.status_code == 400
    assert "未来" in future_resp.json()["detail"]
    assert (
        client.post("/api/admin/data-center/sync", json={"mode": "bogus"}).status_code == 422
    )


def test_run_due_endpoint() -> None:
    assert client.post("/api/admin/data-center/run-due").status_code == 200
    payload = client.post("/api/admin/data-center/run-due").json()["data"]
    assert payload["executed"] == []


# ------------------------------------------------------------------ 权限 / 页面
def test_permissions_cover_data_center() -> None:
    assert {"datacenter.view", "datacenter.manage"} <= permissions.ALL_CODES
    assert permissions.required_for("GET", "/api/admin/data-center/status") == "datacenter.view"
    assert permissions.required_for("GET", "/api/admin/data-center/reports") == "datacenter.view"
    assert permissions.required_for("POST", "/api/admin/data-center/report") == "datacenter.manage"
    assert permissions.required_for("POST", "/api/admin/data-center/sync") == "datacenter.manage"
    group = next(g for g in permissions.catalog() if g["module"] == "datacenter")
    assert group["name"] == "数据中台"
    assert [p["code"] for p in group["perms"]] == ["datacenter.view", "datacenter.manage"]


def test_data_center_page_and_admin_entry() -> None:
    page = client.get("/data-center")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    html = page.text
    for marker in ("dc-run-report", "dc-sync-inc", "dc-run-due", "dc-report-rows", "dc-detail"):
        assert marker in html

    for name in ("data-center.html", "data-center.js", "data-center.css"):
        assert (STATIC_DIR / name).is_file(), name
    js = (STATIC_DIR / "data-center.js").read_text(encoding="utf-8")
    assert "/api/admin/data-center" in js
    admin = (STATIC_DIR / "admin.html").read_text(encoding="utf-8")
    assert 'href="/data-center"' in admin
    assert 'data-perm="datacenter.view"' in admin
