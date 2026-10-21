"""通行查询智能体端到端验收（任务 4.4 / P-DQ-04、I-04）。

覆盖 HTTP 层关键契约：

1. 页面与静态资源可达，后台入口存在；
2. 鉴权：匿名 401、普通用户 403、管理员 200、联络员缺权限点 403；
3. mock 同步：整窗替换、重复同步幂等、缩短窗口后无残留；
4. 看板：总量 = 方向 / 校区 / 门禁点 / 逐日 / 24 小时口径，TOP 榜脱敏；
5. 筛选与层级下钻：校区 / 学院 / 门禁点、方向、异常；
6. 明细分页与字段；
7. 智能报告预览 / 下载、CSV BOM 与截断头、学工号脱敏；
8. 服务端无 500 堆栈。

**数据隔离**：验收前备份 ``data/access_events.json``，以空库启动，结束后恢复。

用法::

    python tools/verify_access.py
    python tools/verify_access.py --port 8046

退出码：全部通过为 0，存在 FAIL 为 1。
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

os.environ.setdefault("KB_STORE", "json")
os.environ.setdefault("LLM_MOCK", "1")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")

try:  # pragma: no cover - 仅影响交互输出
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

PASSED = 0
FAILED = 0
SYNC_DAYS = 10
EXPORT_LIMIT = 100


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


def _backup(path: Path, backup_dir: Path) -> Path | None:
    if not path.is_file():
        return None
    target = backup_dir / path.name
    shutil.copy2(path, target)
    return target


def _restore(path: Path, backup: Path | None) -> None:
    if backup is None:
        if path.exists():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup, path)


def main() -> int:
    parser = argparse.ArgumentParser(description="通行查询智能体端到端验收（任务 4.4）")
    parser.add_argument("--port", type=int, default=8046, help="验收用服务端口")
    args = parser.parse_args()

    from tools._server import read_log, spawn_server, stop_server, wait_port

    data_dir = SERVICE_DIR / "data"
    events_path = data_dir / "access_events.json"
    tmp_dir = Path(tempfile.mkdtemp(prefix="nxz-access-"))
    backup = _backup(events_path, tmp_dir)
    if events_path.exists():
        events_path.unlink()

    port = args.port
    base = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    env.update({
        "KB_STORE": "json",
        "AUTH_ENABLED": "true",
        "RATE_LIMIT_ENABLED": "false",
        "LLM_MOCK": "1",
        "ACCESS_SYNC_PROVIDER": "mock",
        "ACCESS_SYNC_DAYS": str(SYNC_DAYS),
        "ACCESS_EXPORT_LIMIT": str(EXPORT_LIMIT),
    })

    print(f"启动服务 127.0.0.1:{port}（KB_STORE=json, AUTH_ENABLED=true）…")
    proc, log_path = spawn_server(port, env, SERVICE_DIR)
    try:
        if not wait_port(port, timeout=60):
            print("服务启动失败，日志尾部：")
            print(read_log(log_path, 3000))
            return 1

        import httpx

        from app.security.auth import create_token

        admin_h = {
            "Authorization": f"Bearer {create_token('admin-001', '管理员', user_id=1, expires_minutes=10)}"
        }
        student_h = {
            "Authorization": f"Bearer {create_token('20230001', '本科生', user_id=2, expires_minutes=10)}"
        }
        liaison_h = {
            "Authorization": f"Bearer {create_token('D001', '部门联络员', user_id=3, expires_minutes=10)}"
        }

        with httpx.Client(timeout=20, trust_env=False, base_url=base) as cli:
            section("1. 页面与静态资源")
            page = cli.get("/access")
            check("通行分析页 200", page.status_code == 200, str(page.status_code))
            check("页面加载脚本与样式",
                  "access.js" in page.text and "access.css" in page.text)
            check("页面无内联脚本注入面", "<script>" not in page.text)
            for asset in ("/access.js", "/access.css"):
                check(f"静态资源 {asset} 可访问", cli.get(asset).status_code == 200)
            admin_page = cli.get("/admin")
            check("后台页含通行分析入口",
                  admin_page.status_code == 200
                  and 'href="/access"' in admin_page.text
                  and 'data-perm="access.view"' in admin_page.text)

            section("2. 管理端鉴权")
            check("匿名看板 401",
                  cli.get("/api/admin/access/overview").status_code == 401)
            check("匿名同步 401",
                  cli.post("/api/admin/access/sync").status_code == 401)
            check("本科生看板 403",
                  cli.get("/api/admin/access/overview", headers=student_h).status_code == 403)
            liaison = cli.get("/api/admin/access/overview", headers=liaison_h)
            check("联络员缺少 access.view 403", liaison.status_code == 403,
                  str(liaison.status_code))
            check("403 提示权限点", "access.view" in liaison.text)
            check("管理员看板 200",
                  cli.get("/api/admin/access/overview", headers=admin_h).status_code == 200)
            check("管理员导出 200",
                  cli.get("/api/admin/access/export", headers=admin_h).status_code == 200)
            check("管理员报告 200",
                  cli.get("/api/admin/access/report", headers=admin_h).status_code == 200)

            section("3. 门禁流水同步（I-04）")
            synced = cli.post(
                "/api/admin/access/sync", params={"days": SYNC_DAYS}, headers=admin_h
            )
            sync_data = synced.json().get("data") or {}
            check("首次同步 200", synced.status_code == 200, str(synced.status_code))
            check("同步来源 mock", sync_data.get("provider") == "mock")
            check("同步导入流水", int(sync_data.get("imported") or 0) > 0,
                  str(sync_data.get("imported")))
            check("同步无跳过", sync_data.get("skipped") == 0)
            start = date.today() - timedelta(days=SYNC_DAYS - 1)
            check("同步窗口正确",
                  sync_data.get("from") == start.isoformat()
                  and sync_data.get("to") == date.today().isoformat(),
                  f"{sync_data.get('from')} → {sync_data.get('to')}")
            first_total = int(sync_data.get("imported") or 0)

            again = cli.post(
                "/api/admin/access/sync", params={"days": SYNC_DAYS}, headers=admin_h
            ).json().get("data") or {}
            check("重复同步幂等", again.get("imported") == first_total,
                  str(again.get("imported")))
            short = cli.post(
                "/api/admin/access/sync", params={"days": 3}, headers=admin_h
            ).json().get("data") or {}
            check("缩短窗口整窗替换",
                  0 < int(short.get("imported") or 0) < first_total,
                  f"{short.get('imported')} < {first_total}")
            cli.post("/api/admin/access/sync", params={"days": SYNC_DAYS}, headers=admin_h)

            section("4. 多维看板（P-DQ-04）")
            params = {
                "dateFrom": start.isoformat(),
                "dateTo": date.today().isoformat(),
            }
            overview = cli.get(
                "/api/admin/access/overview", params=params, headers=admin_h
            )
            data = overview.json().get("data") or {}
            summary = data.get("summary") or {}
            total = summary.get("total")
            check("看板 200 且有数据", overview.status_code == 200 and total == first_total,
                  f"{overview.status_code} / {total}")
            check("方向拆分守恒",
                  summary.get("entries", 0) + summary.get("exits", 0) == total)
            check("校区维度守恒",
                  sum(row["total"] for row in data.get("byCampus") or []) == total)
            check("学院维度守恒",
                  sum(row["total"] for row in data.get("byCollege") or []) == total)
            check("门禁点维度守恒",
                  sum(row["total"] for row in data.get("byGate") or []) == total)
            check("24 小时分布守恒",
                  len(data.get("hourly") or []) == 24
                  and sum(row["total"] for row in data.get("hourly") or []) == total)
            check("逐日趋势补齐零值",
                  len(data.get("trend") or []) == SYNC_DAYS
                  and sum(row["total"] for row in data.get("trend") or []) == total)
            check("异常口径一致",
                  summary.get("abnormal") == (data.get("abnormal") or {}).get("total"))
            check("个人 TOP10 有脱敏",
                  0 < len(data.get("topPersons") or []) <= 10
                  and all("*" in row["userNoMasked"] for row in data.get("topPersons") or []))
            check("学院 TOP5 截断", len(data.get("topColleges") or []) <= 5)
            check("高峰时段与小时分布一致",
                  (summary.get("peakHour") or {}).get("total")
                  == max(row["total"] for row in data.get("hourly") or []))
            options = data.get("options") or {}
            check("筛选项齐全",
                  len(options.get("campuses") or []) >= 3
                  and len(options.get("gates") or []) == 8
                  and "计算机学院" in (options.get("colleges") or []))
            meta = data.get("meta") or {}
            check("同步元信息正确",
                  meta.get("configuredProvider") == "mock"
                  and meta.get("totalEvents") == total
                  and bool(meta.get("syncedAt")), str(meta.get("syncedAt")))

            section("5. 筛选与层级下钻")
            campus = (data.get("byCampus") or [{}])[0]
            by_campus = cli.get(
                "/api/admin/access/overview",
                params={**params, "campusId": campus.get("campusId")},
                headers=admin_h,
            ).json()["data"]
            check("校区筛选命中同量",
                  by_campus["summary"]["total"] == campus.get("total"),
                  f"{by_campus['summary']['total']} / {campus.get('total')}")
            gate = (data.get("byGate") or [{}])[0]
            by_gate = cli.get(
                "/api/admin/access/overview",
                params={**params, "gateId": gate.get("gateId")},
                headers=admin_h,
            ).json()["data"]
            check("门禁点筛选命中同量",
                  by_gate["summary"]["total"] == gate.get("total"))
            college = (data.get("byCollege") or [{}])[0]
            by_college = cli.get(
                "/api/admin/access/overview",
                params={**params, "college": college.get("collegeName")},
                headers=admin_h,
            ).json()["data"]
            check("学院筛选命中同量",
                  by_college["summary"]["total"] == college.get("total"))
            entries = cli.get(
                "/api/admin/access/overview",
                params={**params, "direction": "in"},
                headers=admin_h,
            ).json()["data"]
            check("方向筛选只含进入",
                  entries["summary"]["total"] == summary.get("entries")
                  and entries["summary"]["exits"] == 0)
            abnormal = cli.get(
                "/api/admin/access/overview",
                params={**params, "abnormal": "true"},
                headers=admin_h,
            ).json()["data"]
            check("异常筛选只含异常",
                  abnormal["summary"]["total"] == summary.get("abnormal")
                  and abnormal["summary"]["abnormal"] == abnormal["summary"]["total"])

            drill = cli.get(
                "/api/admin/access/drilldown",
                params={**params, "dimension": "campus", "campusId": campus.get("campusId")},
                headers=admin_h,
            )
            drill_data = drill.json().get("data") or {}
            check("校区下钻 200", drill.status_code == 200, str(drill.status_code))
            check("校区下钻标题与总量",
                  drill_data.get("title") == campus.get("campusName")
                  and (drill_data.get("summary") or {}).get("total") == campus.get("total"))
            gate_drill = cli.get(
                "/api/admin/access/drilldown",
                params={**params, "dimension": "gate", "gateId": gate.get("gateId")},
                headers=admin_h,
            ).json()["data"]
            check("门禁点下钻标题与总量",
                  gate_drill["title"] == gate.get("gateName")
                  and gate_drill["summary"]["total"] == gate.get("total"))
            missing = cli.get(
                "/api/admin/access/drilldown",
                params={"dimension": "campus"},
                headers=admin_h,
            )
            check("下钻缺 campusId 400", missing.status_code == 400,
                  str(missing.status_code))
            bad_range = cli.get(
                "/api/admin/access/overview",
                params={"dateFrom": "2026/01/01"},
                headers=admin_h,
            )
            check("非法日期 400", bad_range.status_code == 400,
                  str(bad_range.status_code))

            section("6. 明细分页")
            events = cli.get(
                "/api/admin/access/events",
                params={**params, "page": 1, "pageSize": 5},
                headers=admin_h,
            ).json().get("data") or {}
            check("明细总数与看板一致", events.get("total") == total,
                  str(events.get("total")))
            items = events.get("items") or []
            check("分页大小生效", len(items) == 5, str(len(items)))
            check("明细倒序",
                  [row["eventTime"] for row in items]
                  == sorted((row["eventTime"] for row in items), reverse=True))
            check("明细学工号脱敏",
                  all("*" in row.get("userNoMasked", "") for row in items))
            check("明细方向标签",
                  all(row.get("directionLabel") in ("进入", "离开") for row in items))
            second = cli.get(
                "/api/admin/access/events",
                params={**params, "page": 2, "pageSize": 5},
                headers=admin_h,
            ).json().get("data") or {}
            check("翻页无重复",
                  {row["eventId"] for row in items}.isdisjoint(
                      {row["eventId"] for row in second.get("items") or []}
                  ))

            section("7. 智能报告与导出")
            report = cli.get(
                "/api/admin/access/report", params=params, headers=admin_h
            )
            report_data = report.json().get("data") or {}
            content = report_data.get("content") or ""
            check("报告预览 200", report.status_code == 200, str(report.status_code))
            check("报告文件名", str(report_data.get("filename", "")).endswith(".md"))
            for marker in (
                "校园通行智能分析报告",
                "总体概况",
                "校区分布",
                "学院通行 TOP5",
                "通行频次 TOP10",
                "异常通行",
                "趋势与高峰",
                "结论与建议",
                "隐私说明",
            ):
                check(f"报告含「{marker}」", marker in content)
            check("报告不含原始学工号", "20230001" not in content)

            download = cli.get(
                "/api/admin/access/report",
                params={**params, "download": "true"},
                headers=admin_h,
            )
            check("报告下载 200", download.status_code == 200, str(download.status_code))
            check("报告下载头",
                  "attachment" in download.headers.get("content-disposition", "")
                  and download.headers.get("content-type", "").startswith("text/markdown"))
            check("下载内容与预览同源",
                  "校园通行智能分析报告" in download.text
                  and "20230001" not in download.text)

            exported = cli.get(
                "/api/admin/access/export", params=params, headers=admin_h
            )
            check("CSV 导出 200", exported.status_code == 200, str(exported.status_code))
            check("CSV 带 BOM", exported.content.startswith("\ufeff".encode("utf-8")))
            check("CSV 截断头",
                  exported.headers.get("x-export-truncated")
                  == f"true; limit={EXPORT_LIMIT}",
                  str(exported.headers.get("x-export-truncated")))
            text = exported.content.decode("utf-8-sig")
            rows = list(csv.reader(io.StringIO(text)))
            check("CSV 表头正确",
                  rows and rows[0][:3] == ["时间", "方向", "校区"],
                  str(rows[0][:3] if rows else []))
            check("CSV 行数等于上限", len(rows) == EXPORT_LIMIT + 1, str(len(rows)))
            check("CSV 学工号脱敏",
                  "20230001" not in text and "****" in text)

            section("8. 运行时错误")
            log_text = read_log(log_path, 30000)
            check("服务端无 500 堆栈",
                  "Traceback (most recent call last)" not in log_text)

        print(f"\n结果：{PASSED} PASS / {FAILED} FAIL")
        return 1 if FAILED else 0
    finally:
        stop_server(proc)
        _restore(events_path, backup)
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
