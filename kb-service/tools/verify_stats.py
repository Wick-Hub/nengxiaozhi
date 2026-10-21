"""统计与运营分析验收（任务 3.10 / M-T-01～09）。

六组自检，默认 JSON 模式可直接复现；``--store mysql`` 在有库环境验证同一套断言。

1. **口径一致性**：概览「提问/回复」与会话日志（2.1）在无筛选 / 角色 / 日期下完全一致；
   渠道、助手行聚合覆盖全部问答；成功率 = (回复 − 未答) / 回复。
2. **维度与排行**：热门知识/分类引用总量自洽、排序与占比；设备分桶；
   情绪 / 应用按任务范围返回空态与说明，而不是伪造 0 数据。
3. **导出**：summary/trend/channels/agents/knowledge/categories/devices 行数与接口同口径，
   UTF-8 BOM、表头、空结果仅表头；非法类型抛错。
4. **设备采集**：真实 ``/api/chat`` 带 iPhone / iPad / PC UA 落到 ``chat_session.device``，
   并在排行中体现；首次设备不被后续覆盖（隔离临时存储，不污染运行数据）。
5. **接口层契约**：真实服务跑 overview/rankings/filters/export 与参数校验。
6. **鉴权**：无 token → 401、访客 → 403、管理员 → 200。

用法::

    python tools/verify_stats.py
    python tools/verify_stats.py --store mysql
    python tools/verify_stats.py --port 8031

退出码：全部通过为 0，存在 FAIL 为 1。
"""

from __future__ import annotations

import argparse
import os
import sys
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


def _pct(value) -> str:
    return "—" if value is None else f"{value:.2%}"


# ==================================================================== 1. 口径
def check_consistency() -> None:
    from app.services import chatlog_admin, stats_admin

    section("1. 口径一致性（与会话日志 2.1 对齐）")
    overview = stats_admin.overview()
    qa = chatlog_admin.query_qa(page_size=1)
    summary = overview["summary"]

    check("提问数 == 会话日志命中总数",
          summary["questions"] == qa["total"], f"{summary['questions']} == {qa['total']}")
    check("回复数 == 会话日志命中总数",
          summary["replies"] == qa["total"], f"{summary['replies']} == {qa['total']}")
    check("扫描窗口与会话日志一致",
          overview["window"]["scanned"] == qa["scanned"],
          f"{overview['window']['scanned']} == {qa['scanned']}")
    check("已答 + 未答 == 回复",
          summary["answered"] + summary["unanswered"] == summary["replies"],
          f"{summary['answered']} + {summary['unanswered']} == {summary['replies']}")
    if summary["replies"]:
        expect = round(summary["answered"] / summary["replies"], 4)
        check("成功率公式正确", summary["successRate"] == expect,
              f"{_pct(summary['successRate'])}")
    check("访问 == 会话数", summary["sessions"] == summary["visits"],
          f"{summary['sessions']}")

    check("渠道行提问之和 == 总提问",
          sum(r["questions"] for r in overview["channels"]) == summary["questions"],
          f"{sum(r['questions'] for r in overview['channels'])}")
    check("助手行提问之和 == 总提问",
          sum(r["questions"] for r in overview["agents"]) == summary["questions"],
          f"{sum(r['questions'] for r in overview['agents'])}")
    check("渠道/助手行含占比与成功率",
          all({"share", "successRate", "sessions"} <= set(r)
              for r in overview["channels"] + overview["agents"]))

    opts = chatlog_admin.filter_options()
    roles = opts.get("roles") or []
    if roles:
        role = roles[0]
        ov = stats_admin.overview(role=role)
        q = chatlog_admin.query_qa(role=role, page_size=1)
        check(f"角色筛选口径一致 role={role}", ov["summary"]["questions"] == q["total"],
              f"{ov['summary']['questions']} == {q['total']}")

    first = (qa.get("items") or [{}])[0]
    day = str(first.get("answeredAt") or "")[:10]
    if day:
        ov = stats_admin.overview(date_from=day, date_to=day)
        q = chatlog_admin.query_qa(date_from=day, date_to=day, page_size=1)
        check(f"日期筛选口径一致 date={day}", ov["summary"]["questions"] == q["total"],
              f"{ov['summary']['questions']} == {q['total']}")


# ==================================================================== 2. 维度
def check_rankings() -> None:
    from app.services import stats_admin

    section("2. 维度与排行")
    data = stats_admin.rankings(limit=50)
    need = {"knowledge", "knowledgeTotal", "categories", "categoryTotal",
            "citations", "devices", "sentiments", "apps", "notes", "window"}
    check("排行结构完整", need <= set(data), f"missing={sorted(need - set(data))}")

    docs = data["knowledge"]
    check("热门知识按引用次数降序",
          [d["citations"] for d in docs] == sorted([d["citations"] for d in docs], reverse=True))
    check("知识 total >= 返回条数", data["knowledgeTotal"] >= len(docs),
          f"{data['knowledgeTotal']} >= {len(docs)}")
    if data["knowledgeTotal"] <= 50:
        check("知识引用之和 == 总引用",
              sum(d["citations"] for d in docs) == data["citations"],
              f"{sum(d['citations'] for d in docs)} == {data['citations']}")
    if data["categoryTotal"] <= 50:
        check("分类引用之和 == 总引用",
              sum(c["citations"] for c in data["categories"]) == data["citations"],
              f"{sum(c['citations'] for c in data['categories'])} == {data['citations']}")
    check("占比在 0~1 之间",
          all(0 <= (d.get("share") or 0) <= 1 for d in docs + data["categories"]))

    devices = data["devices"]
    check("设备桶计数非负且带标签占比",
          all(r["count"] > 0 and r["label"] and r.get("share") is not None for r in devices),
          f"rows={len(devices)}")
    overview = stats_admin.overview()
    if overview["summary"]["sessions"]:
        check("设备桶会话之和 == 访问数",
              sum(r["count"] for r in devices) == overview["summary"]["sessions"],
              f"{sum(r['count'] for r in devices)} == {overview['summary']['sessions']}")
    check("设备键合法（pc/mobile/tablet/other/unknown）",
          all(r["key"] in {"pc", "mobile", "tablet", "other", "unknown"} for r in devices),
          str([r["key"] for r in devices]))
    if any(r["key"] == "unknown" for r in devices):
        check("历史未采集会话如实标注", "暂无设备数据" in (data["notes"].get("devices") or ""))

    check("应用排行返回空态（埋点未接入）", data["apps"] == [])
    check("应用空态有说明", "埋点尚未接入" in (data["notes"].get("apps") or ""),
          data["notes"].get("apps", "")[:40])
    if data["sentiments"]:
        check("情绪数据（如已打标）带占比",
              all(r["count"] > 0 and r.get("share") is not None for r in data["sentiments"]))
    else:
        check("情绪空态有说明（待 4.6）", "4.6" in (data["notes"].get("sentiments") or ""))

    one = stats_admin.rankings(limit=1)
    check("limit 生效", len(one["knowledge"]) <= 1 and len(one["categories"]) <= 1,
          f"k={len(one['knowledge'])} c={len(one['categories'])}")
    check("limit 只截断列表、总数不变",
          one["knowledgeTotal"] == data["knowledgeTotal"],
          f"{one['knowledgeTotal']} == {data['knowledgeTotal']}")


# ==================================================================== 3. 导出
def check_export() -> None:
    from app.services import stats_admin

    section("3. CSV 导出")
    overview = stats_admin.overview()
    rankings = stats_admin.rankings(limit=50)

    text, rows = stats_admin.export_csv("summary")
    check("summary 导出 7 项指标", rows == 7, f"rows={rows}")
    check("summary 带 UTF-8 BOM", text.startswith("\ufeff"))
    lines = text.lstrip("\ufeff").splitlines()
    check("summary 表头正确",
          lines[0] == ",".join(name for name, _ in stats_admin.EXPORT_COLUMNS["summary"]),
          lines[0])
    check("summary 行数 = 表头 + 数据行", len(lines) == rows + 1,
          f"{len(lines)} == {rows + 1}")

    pairs = [
        ("trend", len(overview["trend"])),
        ("channels", len(overview["channels"])),
        ("agents", len(overview["agents"])),
        ("knowledge", min(rankings["knowledgeTotal"], stats_admin.MAX_TOP)),
        ("categories", min(rankings["categoryTotal"], stats_admin.MAX_TOP)),
        ("devices", len(rankings["devices"])),
    ]
    for kind, expect in pairs:
        body, n = stats_admin.export_csv(kind)
        lines = body.lstrip("\ufeff").splitlines()
        check(f"{kind} 导出行数与接口同口径", n == expect, f"{n} == {expect}")
        check(f"{kind} 表头正确",
              lines[0] == ",".join(name for name, _ in stats_admin.EXPORT_COLUMNS[kind]))
        check(f"{kind} 行数 = 表头 + 数据行", len(lines) == n + 1,
              f"{len(lines)} == {n + 1}")

    empty, n = stats_admin.export_csv("sentiments")
    check("空态导出仍保留表头", n == 0 and len(empty.lstrip("\ufeff").splitlines()) == 1)

    try:
        stats_admin.export_csv("bogus")
        check("非法导出类型抛 ValueError", False)
    except ValueError:
        check("非法导出类型抛 ValueError", True)


# ==================================================================== 4. 设备
def check_device_capture(store: str) -> None:
    from types import SimpleNamespace

    from app.api.chat import _device_of

    section("4. 设备采集")
    cases = [
        ("iPhone", "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Mobile/15E148", "mobile"),
        ("iPad", "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15", "tablet"),
        ("Android 平板", "Mozilla/5.0 (Linux; Android 13; SM-X700) Safari/537.36", "tablet"),
        ("Windows", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 Safari/537.36", "pc"),
        ("缺失", "", "other"),
    ]
    for label, ua, expect in cases:
        got = _device_of(SimpleNamespace(headers={"user-agent": ua} if ua else {}))
        check(f"{label} UA → {expect}", got == expect, got)

    if store != "json":
        check("MySQL 模式跳过隔离 E2E（由 pytest 覆盖）", True)
        return

    import tempfile

    from fastapi.testclient import TestClient

    from app.db import chatlog
    from app.main import app
    from app.services import stats_admin

    original = chatlog.SESSIONS_PATH
    with tempfile.TemporaryDirectory() as tmp:
        chatlog.SESSIONS_PATH = Path(tmp) / "sessions.json"
        try:
            client = TestClient(app)
            for label, ua, expect in cases[:4]:
                sid = f"VF-STATS-{expect}-{label}"
                resp = client.post(
                    "/api/chat",
                    json={"question": "校园卡怎么补办？", "role": "本科生",
                          "sessionId": sid, "stream": False},
                    headers={"User-Agent": ua},
                )
                ok = resp.status_code == 200 and chatlog.load_session(sid).get("device") == expect
                check(f"/api/chat {label} 落到 device={expect}", ok,
                      f"status={resp.status_code}")
            check("设备首次写入不被覆盖",
                  chatlog.set_session_device("VF-STATS-mobile-iPhone", "pc") is False)

            devices = {d["key"]: d["count"] for d in stats_admin.rankings()["devices"]}
            check("排行反映三类设备",
                  devices.get("mobile") == 1 and devices.get("tablet") == 2 and devices.get("pc") == 1,
                  str(devices))
        finally:
            chatlog.SESSIONS_PATH = original


# ==================================================================== 5. 接口层
def _client(base: str):
    import httpx

    return httpx.Client(timeout=30, trust_env=False, base_url=base)


def check_api(port: int, store: str) -> None:
    from tools._server import read_log, spawn_server, stop_server, wait_port

    section("5. 接口层契约（真实服务）")
    env = dict(os.environ)
    env["KB_STORE"] = store
    env["AUTH_ENABLED"] = "false"
    env["RATE_LIMIT_ENABLED"] = "false"
    env["LLM_MOCK"] = "1"

    proc, log_path = spawn_server(port, env, SERVICE_DIR)
    base = f"http://127.0.0.1:{port}"
    try:
        if not wait_port(port, timeout=60):
            check("服务启动", False, read_log(log_path, 800)[-400:])
            return
        check("服务启动", True, f"port={port}")

        with _client(base) as cli:
            filters = cli.get("/api/admin/stats/filters")
            check("GET /stats/filters 200 + ok",
                  filters.status_code == 200 and filters.json().get("ok") is True,
                  f"status={filters.status_code}")

            ov = cli.get("/api/admin/stats/overview")
            check("GET /stats/overview 200 + ok",
                  ov.status_code == 200 and ov.json().get("ok") is True,
                  f"status={ov.status_code}")
            logs = cli.get("/api/admin/chatlogs", params={"pageSize": 1})
            if ov.status_code == 200 and logs.status_code == 200:
                check("接口层提问数 == 会话日志总数",
                      ov.json()["data"]["summary"]["questions"] == logs.json()["data"]["total"],
                      f"{ov.json()['data']['summary']['questions']} == {logs.json()['data']['total']}")

            rk = cli.get("/api/admin/stats/rankings", params={"limit": 5})
            check("GET /stats/rankings 200 + ok",
                  rk.status_code == 200 and rk.json().get("ok") is True,
                  f"status={rk.status_code}")
            if rk.status_code == 200:
                data = rk.json()["data"]
                check("排行 limit 生效",
                      len(data["knowledge"]) <= 5 and len(data["categories"]) <= 5,
                      f"k={len(data['knowledge'])}")
                check("排行含空态说明", isinstance(data.get("notes"), dict))

            for kind in ("summary", "trend", "knowledge", "devices"):
                r = cli.get("/api/admin/stats/export", params={"kind": kind})
                ctype = r.headers.get("content-type", "")
                disp = r.headers.get("content-disposition", "")
                check(f"导出 {kind} 200 + text/csv",
                      r.status_code == 200 and ctype.startswith("text/csv"),
                      f"status={r.status_code} ctype={ctype}")
                check(f"导出 {kind} 带 RFC 5987 文件名",
                      "filename*=UTF-8''" in disp, disp[:60])
                check(f"导出 {kind} 带 BOM + nosniff",
                      r.text.startswith("\ufeff")
                      and r.headers.get("x-content-type-options") == "nosniff")

            check("非法导出类型 → 422",
                  cli.get("/api/admin/stats/export", params={"kind": "bogus"}).status_code == 422)
            check("limit=0 → 422",
                  cli.get("/api/admin/stats/rankings", params={"limit": 0}).status_code == 422)
            check("limit=51 → 422",
                  cli.get("/api/admin/stats/rankings", params={"limit": 51}).status_code == 422)
    finally:
        stop_server(proc)


def check_auth(port: int, store: str) -> None:
    from tools._server import read_log, spawn_server, stop_server, wait_port

    section("6. 鉴权（AUTH_ENABLED=true）")
    env = dict(os.environ)
    env["KB_STORE"] = store
    env["AUTH_ENABLED"] = "true"
    env["RATE_LIMIT_ENABLED"] = "false"
    env["LLM_MOCK"] = "1"

    proc, log_path = spawn_server(port, env, SERVICE_DIR)
    base = f"http://127.0.0.1:{port}"
    try:
        if not wait_port(port, timeout=60):
            check("鉴权实例启动", False, read_log(log_path, 800)[-400:])
            return
        check("鉴权实例启动", True, f"port={port}")

        with _client(base) as cli:
            check("无 token → 401",
                  cli.get("/api/admin/stats/overview").status_code == 401)
            check("导出同样受保护 → 401",
                  cli.get("/api/admin/stats/export").status_code == 401)

            from app.security.auth import create_token

            guest = create_token("visitor-001", "访客", user_id=None, expires_minutes=5)
            r = cli.get("/api/admin/stats/overview",
                        headers={"Authorization": f"Bearer {guest}"})
            check("访客 token → 403", r.status_code == 403, f"status={r.status_code}")

            admin = create_token("admin-001", "管理员", user_id=1, expires_minutes=5)
            r = cli.get("/api/admin/stats/overview",
                        headers={"Authorization": f"Bearer {admin}"})
            check("管理员 token → 200", r.status_code == 200, f"status={r.status_code}")
    finally:
        stop_server(proc)


# ==================================================================== main
def main() -> int:
    parser = argparse.ArgumentParser(description="统计与运营分析验收（任务 3.10）")
    parser.add_argument("--store", default="json", choices=["json", "mysql"],
                        help="存储模式；默认 json（无需 MySQL）")
    parser.add_argument("--port", type=int, default=8031, help="接口层验收端口")
    parser.add_argument("--skip-auth", action="store_true", help="跳过鉴权组")
    args = parser.parse_args()

    os.environ["KB_STORE"] = args.store

    from app.config import settings

    print("=" * 68)
    print("统计与运营分析验收（任务 3.10）")
    print(f"存储模式：{settings.store_label}    数据目录：{settings.base_dir}")
    print("=" * 68)

    check_consistency()
    check_rankings()
    check_export()
    check_device_capture(settings.store_label)
    check_api(args.port, args.store)
    if not args.skip_auth:
        check_auth(args.port + 1, args.store)

    print("\n" + "=" * 68)
    print(f"结果：{PASSED} PASS / {FAILED} FAIL")
    print("=" * 68)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
