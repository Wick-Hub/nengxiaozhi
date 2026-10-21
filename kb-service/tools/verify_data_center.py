"""数据中台验收（任务 4.2 / I-02、M-SYS-02）。

默认 **JSON 模式 + 临时数据文件**，无需 MySQL；``--store mysql`` 切到真实库。

覆盖：

1. 表结构：``data_center_job`` / ``data_center_report`` / ``data_center_state``（仅 MySQL）；
2. 组织同步：首次建档、重复执行幂等、全量缺项只统计不删除；
3. 上报：预览不投递、按日生成批次、重复执行 skipped、内容变化升 revision；
4. 隐私：默认不含问答正文，mock 收件箱与数据库 payload 一致；
5. 定时：每日上报 / 增量、每周全量按水位只跑一次；
6. 接口：状态、任务、批次详情、组织/用户镜像、非法参数；
7. 权限与页面：``datacenter.view/manage`` 映射、``/data-center`` 页面；
8. 操作日志（仅 MySQL）：上报 / 同步各留一条 ``DATA_CENTER_*``。

MySQL 模式只写入 ``VFDC`` 前缀的组织/用户，结束时删除；任务、批次与
调度水位按验收前快照恢复，不破坏已有数据。

用法::

    python tools/verify_data_center.py
    python tools/verify_data_center.py --store mysql
    python tools/verify_data_center.py --keep-data

退出码：全部通过为 0，存在 FAIL 为 1。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

os.environ.setdefault("KB_STORE", "json")
os.environ.setdefault("LLM_MOCK", "1")
os.environ.setdefault("AUTH_ENABLED", "false")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")

try:  # pragma: no cover - 仅影响交互输出
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

PASSED = 0
FAILED = 0
ORG_PREFIX = "VFDC"
STATE_KEYS = (
    "orgCursor",
    "lastReportAt",
    "lastIncrementalAt",
    "lastFullAt",
    "lastReportDate",
    "lastIncrementalDate",
    "lastFullDate",
)

VF_ORGS = (
    {
        "orgCode": "VFDC",
        "orgName": "数据中台验收根",
        "parentCode": "",
        "orgPath": "/VFDC/",
        "sortNo": 900,
        "status": 1,
        "updatedAt": "2026-01-01T08:00:00",
    },
    {
        "orgCode": "VFDC01",
        "orgName": "数据中台验收子部门",
        "parentCode": "VFDC",
        "orgPath": "/VFDC/VFDC01/",
        "sortNo": 910,
        "status": 1,
        "updatedAt": "2026-01-01T08:00:00",
    },
)
VF_USERS = (
    {
        "userNo": "VFDC0001",
        "realName": "中台验收生",
        "idType": 1,
        "orgCode": "VFDC01",
        "email": "vfdc@example.edu.cn",
        "status": 1,
        "updatedAt": "2026-01-01T08:05:00",
    },
)


@dataclass
class _Gen:
    answer: str = "请携带校园卡前往一卡通中心办理。"
    citations: list = field(default_factory=lambda: [{"n": 1, "chunkId": "d1::0"}])
    confidence: float = 0.72
    refused: bool = False


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


def _mysql() -> bool:
    from app.config import settings

    return settings.store_label == "mysql"


# ==================================================================== 0. 表结构
def check_schema() -> bool:
    if not _mysql():
        info("JSON 模式：跳过 MySQL 表结构检查。")
        return True
    from sqlalchemy import text

    from app.db.session import db_session

    section("0. MySQL 表结构")
    want = {
        "data_center_job": {
            "job_id", "job_type", "mode", "status", "started_at", "finished_at",
            "total", "created_count", "updated_count", "skipped_count",
            "failed_count", "checksum", "detail", "error",
        },
        "data_center_report": {
            "report_id", "batch_id", "kind", "report_date", "revision",
            "checksum", "status", "rows_count", "payload", "error",
            "created_at", "updated_at",
        },
        "data_center_state": {"state_key", "state_value", "updated_at"},
    }
    ok = True
    with db_session() as conn:
        for table, columns in want.items():
            rows = conn.execute(
                text(
                    "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t"
                ),
                {"t": table},
            ).scalars()
            have = set(rows)
            missing = columns - have
            check(f"{table} 表结构", not missing, f"缺列 {sorted(missing)}" if missing else f"{len(have)} 列")
            ok = ok and not missing
    if not ok:
        info("请先执行：python tools/migrate_data_center_4_2.py")
    return ok


# ==================================================================== MySQL 清理
def snapshot_mysql() -> dict:
    if not _mysql():
        return {}
    from sqlalchemy import func, select

    from app.db import data_center_store as store
    from app.db import models as M
    from app.db.session import db_session

    with db_session() as conn:
        max_job = int(conn.execute(select(func.max(M.DataCenterJob.job_id))).scalar() or 0)
    return {"max_job": max_job, "states": store.states()}


def cleanup_mysql(ctx: dict, batch_ids: list[str]) -> None:
    if not _mysql() or not ctx:
        return
    from sqlalchemy import delete as sa_delete, insert as sa_insert

    from app.db import models as M
    from app.db.session import db_transaction

    with db_transaction() as conn:
        conn.execute(
            sa_delete(M.DataCenterJob).where(M.DataCenterJob.job_id > int(ctx["max_job"]))
        )
        if batch_ids:
            conn.execute(
                sa_delete(M.DataCenterReport).where(
                    M.DataCenterReport.batch_id.in_(batch_ids)
                )
            )
        conn.execute(
            sa_delete(M.DataCenterState).where(M.DataCenterState.state_key.in_(STATE_KEYS))
        )
        for key, value in ctx["states"].items():
            if key in STATE_KEYS:
                conn.execute(
                    sa_insert(M.DataCenterState).values(
                        state_key=key, state_value=str(value), updated_at=datetime.now()
                    )
                )
        conn.execute(sa_delete(M.SysUser).where(M.SysUser.user_no.like(f"{ORG_PREFIX}%")))
        conn.execute(sa_delete(M.SysOrg).where(M.SysOrg.org_code.like(f"{ORG_PREFIX}%")))


# ==================================================================== 1. 同步
def check_sync(expect_default: bool) -> None:
    from app.services import data_center

    section("1. 组织 / 用户同步")
    result = data_center.run_sync("full")
    check("全量同步成功", result["ok"], str(result.get("error") or ""))
    if not result["ok"]:
        return
    data = result["data"]
    if expect_default:
        check("组织首次建档 6 条", data["orgs"]["created"] == 6, str(data["orgs"]))
        check("用户首次建档 4 条", data["users"]["created"] == 4, str(data["users"]))
    else:
        check("验收组织建档", data["orgs"]["created"] == len(VF_ORGS), str(data["orgs"]))
        check("验收用户建档", data["users"]["created"] == len(VF_USERS), str(data["users"]))
    check("同步水位已推进", bool(data["cursor"]), data["cursor"])

    again = data_center.run_sync("full")
    check("重复全量同步不重复建档", again["ok"]
          and again["data"]["orgs"]["created"] == 0
          and again["data"]["users"]["created"] == 0, str(again.get("data") or again.get("error")))
    check("重复全量同步不误报更新", again["ok"]
          and again["data"]["orgs"]["updated"] == 0
          and again["data"]["users"]["updated"] == 0)

    inc = data_center.run_sync("incremental")
    check("增量同步成功", inc["ok"], str(inc.get("error") or ""))
    if inc["ok"] and expect_default:
        check("水位之后无新增", inc["data"]["orgs"]["fetched"] == 0
              and inc["data"]["users"]["fetched"] == 0, str(inc["data"]))

    mirror = data_center.status()["mirror"]
    check("本地镜像可查询", mirror["orgs"] >= len(VF_ORGS) and mirror["users"] >= len(VF_USERS),
          f"orgs={mirror['orgs']} users={mirror['users']}")


# ==================================================================== 2. 上报
def check_report(day: str, seeded: bool) -> list[str]:
    from app.db import data_center_store as store
    from app.services import data_center

    section("2. 聊天日志 / 统计上报")
    preview = data_center.preview_report(day, "all")
    check("预览成功", preview["ok"], str(preview.get("error") or ""))
    if preview["ok"]:
        items = preview["data"]["items"]
        check("预览包含日志与统计", [i["kind"] for i in items] == ["chatlog", "stats"])
        check("预览不写批次", not store.list_reports())
        check("预览默认不含正文", preview["data"]["includeContent"] is False)
        if seeded:
            check("预览统计有数据", items[1]["statistics"]["questions"] > 0,
                  str(items[1]["statistics"]["questions"]))

    result = data_center.run_report(day, "all")
    check("上报成功", result["ok"], str(result.get("error") or ""))
    if not result["ok"]:
        return []
    batches = result["data"]["batches"]
    batch_ids = [b["batchId"] for b in batches]
    check("生成两个交付批次", len(batches) == 2, ",".join(batch_ids))
    check("批次均已投递", all(b["status"] == "delivered" for b in batches))
    check("批次带 revision=1", all(b["revision"] == 1 for b in batches))
    if seeded:
        check("日志批次包含记录", batches[0]["rows"] > 0, str(batches[0]["rows"]))
        check("统计口径与记录一致",
              batches[1]["rows"] == batches[0]["rows"],
              f"{batches[1]['rows']} vs {batches[0]['rows']}")

    saved = store.get_report(batch_ids[0])
    check("批次已落库", bool(saved))
    if saved:
        payload = saved.get("payload") or {}
        check("上报标准版本", payload.get("schema") == data_center.SCHEMA, data_center.SCHEMA)
        check("默认不含问答正文",
              all("question" not in r and "answer" not in r for r in payload.get("records", [])))
        if seeded:
            check("记录含会话与角色",
                  bool(payload.get("records"))
                  and {"sessionId", "role", "askedAt"} <= set(payload["records"][0]))

    again = data_center.run_report(day, "all")
    check("相同内容重复上报跳过", again["ok"] and all(b["skipped"] for b in again["data"]["batches"]))
    check("跳过不升 revision", again["ok"]
          and all(b["revision"] == 1 for b in again["data"]["batches"]))

    if not _mysql():
        from app.db import data_center_store as store2

        files = {p.name for p in store2.SINK_DIR.glob("*.json")} if store2.SINK_DIR.exists() else set()
        check("mock 收件箱落盘", {f"{bid}.json" for bid in batch_ids} <= files, ",".join(sorted(files)))
        payload = json.loads(
            (store2.SINK_DIR / f"{batch_ids[0]}.json").read_text(encoding="utf-8")
        )
        check("收件箱内容与批次一致", payload["counts"]["records"] == saved["rows"])
    return batch_ids


# ==================================================================== 3. 定时
def check_scheduler() -> list[str]:
    from app.config import settings
    from app.db import data_center_store as store
    from app.services import data_center

    section("3. 定时任务与水位")
    original_weekday = settings.data_center_full_weekday
    settings.data_center_full_weekday = 2  # 2026-10-14 为周三
    batch_ids: list[str] = []
    try:
        moment = datetime(2026, 10, 14, 5, 0)
        first = data_center.run_due(moment)
        jobs = [item["job"] for item in first["executed"]]
        check("到期任务全部执行", jobs == ["report", "org_incremental", "org_full"], str(jobs))
        check("任务均成功", all(item["ok"] for item in first["executed"]))
        check("日报水位", store.get_state("lastReportDate") == "2026-10-14")
        check("增量水位", store.get_state("lastIncrementalDate") == "2026-10-14")
        check("全量水位", store.get_state("lastFullDate") == "2026-10-14")
        batch_ids += [
            f"DC-CHATLOG-20261013",
            f"DC-STATS-20261013",
        ]
        second = data_center.run_due(moment)
        check("同日重复调度不重复执行", second["executed"] == [])
    finally:
        settings.data_center_full_weekday = original_weekday
    return batch_ids


# ==================================================================== 4. 接口
def check_api(client, day: str, batch_ids: list[str]) -> None:
    section("4. 管理接口")
    status = client.get("/api/admin/data-center/status")
    check("状态接口 200", status.status_code == 200, status.text[:120])
    body = status.json()["data"]
    check("返回上报标准", body["schema"] == "nxz.data-center.report.v1")
    check("返回同步元信息", body["sync"]["modes"] == ["incremental", "full"])

    jobs = client.get("/api/admin/data-center/jobs").json()["data"]["items"]
    check("任务清单可查", len(jobs) >= 2, f"{len(jobs)} 条")
    reports = client.get("/api/admin/data-center/reports").json()["data"]["items"]
    check("批次清单可查", len(reports) >= len(set(batch_ids)), f"{len(reports)} 条")
    if batch_ids:
        detail = client.get(f"/api/admin/data-center/reports/{batch_ids[0]}")
        check("批次详情 200", detail.status_code == 200, detail.text[:120])
        if detail.status_code == 200:
            check("详情带 payload", detail.json()["data"]["payload"]["batchId"] == batch_ids[0])
    check("未知批次 404",
          client.get("/api/admin/data-center/reports/DC-NOT-EXISTS").status_code == 404)

    orgs = client.get("/api/admin/data-center/orgs").json()["data"]["items"]
    users = client.get("/api/admin/data-center/users").json()["data"]["items"]
    codes = {o["orgCode"] for o in orgs}
    check("组织镜像含同步来源", "XUST" in codes or "VFDC" in codes, ",".join(sorted(codes)[:5]))
    check("用户镜像可查询", len(users) >= 1, f"{len(users)} 条")

    preview = client.post("/api/admin/data-center/preview", json={"date": day, "kind": "stats"})
    check("预览接口 200", preview.status_code == 200, preview.text[:120])
    bad = client.post("/api/admin/data-center/report", json={"date": "2026-13-01"})
    check("非法日期 400", bad.status_code == 400, str(bad.status_code))
    future = (date.today() + timedelta(days=1)).isoformat()
    future_resp = client.post("/api/admin/data-center/report", json={"date": future})
    check("未来日期 400", future_resp.status_code == 400, str(future_resp.status_code))

    logged_sync = client.post("/api/admin/data-center/sync", json={"mode": "full"})
    check("接口触发同步", logged_sync.status_code == 200, logged_sync.text[:120])
    logged_report = client.post(
        "/api/admin/data-center/report", json={"date": day, "kind": "stats"}
    )
    check("接口触发上报", logged_report.status_code == 200, logged_report.text[:120])


# ==================================================================== 5. 权限 / 页面
def check_permissions_page(client) -> None:
    from app.security import permissions

    section("5. 权限与页面")
    check("权限点已注册",
          {"datacenter.view", "datacenter.manage"} <= set(permissions.all_codes()))
    check("GET → datacenter.view",
          permissions.required_for("GET", "/api/admin/data-center/status") == "datacenter.view")
    check("POST → datacenter.manage",
          permissions.required_for("POST", "/api/admin/data-center/sync") == "datacenter.manage")
    page = client.get("/data-center")
    check("数据中台页面 200", page.status_code == 200, str(page.status_code))
    check("页面含核心控件", "dc-run-report" in page.text and "dc-run-due" in page.text)
    admin = client.get("/admin")
    check("后台入口可访问", admin.status_code == 200, str(admin.status_code))


# ==================================================================== 6. 审计
def check_oplog(client) -> None:
    if not _mysql():
        info("JSON 模式不落审计表，跳过 DATA_CENTER_* 操作日志检查。")
        return
    section("6. 操作日志")
    for action, label in (("DATA_CENTER_REPORT", "上报"), ("DATA_CENTER_SYNC", "同步")):
        body = client.get(
            "/api/admin/operation-logs", params={"action": action, "pageSize": 50}
        ).json()["data"]
        check(f"操作日志留痕：{label}", body["total"] >= 1, f"{action} total={body['total']}")


# ==================================================================== main
def main() -> int:
    parser = argparse.ArgumentParser(description="数据中台验收（任务 4.2）")
    parser.add_argument("--store", default="json", choices=["json", "mysql"],
                        help="存储模式；默认 json（无需 MySQL）")
    parser.add_argument("--keep-data", action="store_true",
                        help="保留临时数据（默认测完删除）")
    args = parser.parse_args()

    os.environ["KB_STORE"] = args.store

    tmpdir = Path(tempfile.mkdtemp(prefix="verify-datacenter-"))
    seeded = args.store == "json"
    if not _mysql():
        from app.db import chatlog, data_center_store

        chatlog.SESSIONS_PATH = tmpdir / "sessions.json"
        data_center_store.DATA_CENTER_PATH = tmpdir / "data_center.json"
        data_center_store.SINK_DIR = tmpdir / "data_center_outbox"
        print(f"临时数据目录：{tmpdir}")

    from fastapi.testclient import TestClient

    from app.config import settings
    from app.db import chatlog
    from app.main import app
    from app.services import data_center

    print("=" * 68)
    print("数据中台验收（任务 4.2 · I-02、M-SYS-02）")
    print(f"存储模式：{settings.store_label}    鉴权：AUTH_ENABLED={settings.auth_enabled}")
    print("=" * 68)

    today = date.today().isoformat()
    if seeded:
        chatlog.save_qa("图书馆借阅规则是什么", "本科生", _Gen(), "S-VF-DC-1")
        chatlog.save_qa(
            "校园卡补办需要多久",
            "本科生",
            _Gen(answer="到一卡通中心现场办理。"),
            "S-VF-DC-1",
        )
        chatlog.save_qa(
            "食堂营业时间",
            "访客",
            _Gen(answer="抱歉，知识库中未找到相关内容。", refused=True),
            "S-VF-DC-2",
        )
    else:
        # 验收组织/用户使用独立前缀，结束后删除
        data_center.MOCK_ORGS = VF_ORGS
        data_center.MOCK_USERS = VF_USERS

    client = TestClient(app)
    batch_ids = [f"DC-CHATLOG-{today.replace('-', '')}", f"DC-STATS-{today.replace('-', '')}"]
    ctx: dict = {}
    ok = False
    try:
        if not check_schema():
            return 1
        ctx = snapshot_mysql()
        check_sync(expect_default=seeded)
        batch_ids += check_report(today, seeded)
        batch_ids += check_scheduler()
        check_api(client, today, batch_ids)
        check_permissions_page(client)
        check_oplog(client)
        ok = True
    finally:
        cleanup_mysql(ctx, sorted(set(batch_ids)))
        if not args.keep_data:
            shutil.rmtree(tmpdir, ignore_errors=True)
        elif not _mysql():
            print(f"临时数据保留在：{tmpdir}")

    print(f"\n结果：{PASSED} PASS / {FAILED} FAIL")
    return 0 if FAILED == 0 and ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
