"""验收文档校验与 15 分钟演示彩排（任务 5.4）。

用法::

    python tools/verify_acceptance.py            # 校验文档覆盖与证据文件
    python tools/verify_acceptance.py --rehearse # 追加六项 ■ 的接口级彩排
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

ACC_DIR = SERVICE_DIR / "artifacts" / "acceptance"
CHECKLIST = ACC_DIR / "验收用例清单.md"
DEMO = ACC_DIR / "演示脚本.md"
EVIDENCE = ACC_DIR / "佐证材料清单.md"
REHEARSAL = ACC_DIR / "rehearsal.md"

REQUIREMENT_IDS = (
    [f"P-B-{i:02d}" for i in range(1, 18)]
    + [f"P-QA-{i:02d}" for i in range(1, 9)]
    + [f"P-DQ-{i:02d}" for i in range(1, 6)]
    + [f"P-DB-{i:02d}" for i in range(1, 3)]
    + [f"M-A-{i:02d}" for i in range(1, 11)]
    + [f"M-C-{i:02d}" for i in range(1, 8)]
    + [f"M-K-{i:02d}" for i in range(1, 11)]
    + [f"M-S-{i:02d}" for i in range(1, 10)]
    + [f"M-G-{i:02d}" for i in range(1, 3)]
    + [f"M-T-{i:02d}" for i in range(1, 10)]
    + [f"M-SYS-{i:02d}" for i in range(1, 9)]
    + [f"C-{i:02d}" for i in range(1, 6)]
    + [f"I-{i:02d}" for i in range(1, 8)]
)

AC_IDS = [f"AC-{i:02d}" for i in range(1, 20)]

EVIDENCE_FILES = (
    "access-dashboard.png",
    "access-drilldown.png",
    "access-events.csv",
    "access-report.md",
    "ai-label.png",
    "apps-agent-created.png",
    "apps-chat-square.png",
    "apps-debug.png",
    "apps-empty.png",
    "apps-list.png",
    "apps-two.png",
    "asr-answer.png",
    "asr-recording.png",
    "asr-transcript.png",
    "build-offline-full.log",
    "channels-code.png",
    "channels-embed.png",
    "channels-list.png",
    "channels-modal.png",
    "channels-portal.png",
    "chatlog-qa.png",
    "chatlog-search.png",
    "chatlog-users.png",
    "data-center-ui-report.png",
    "data-center-ui-sync.png",
    "dq-card.png",
    "dq-denied.png",
    "dq-grades.png",
    "dq-schedule.png",
    "feedback-admin.png",
    "gateway_page.png",
    "gov-admin.png",
    "gov-guide-card.png",
    "gov-hall-replay.png",
    "gov-hall-success.png",
    "install-smoke.log",
    "integrations-ui-agent.png",
    "integrations-ui-empty.png",
    "integrations-ui-final.png",
    "integrations-ui-mcp.png",
    "integrations-ui-seeded.png",
    "integrations-ui-test.png",
    "kb-audit-filters.png",
    "kb-audit-preview.png",
    "kb-categories.png",
    "kb-permissions.png",
    "kb-points-editor.png",
    "kb-points-list.png",
    "models-list.png",
    "perf-report.md",
    "portal-desktop.png",
    "portal-kiosk.png",
    "portal-mobile.png",
    "pytest-after-5.3.log",
    "rbac-admin.png",
    "rbac-admin-docs.png",
    "rollback-smoke.log",
    "routes-guest.png",
    "routes-modal.png",
    "routes-staff.png",
    "sdk-cross-origin.png",
    "share-actions.png",
    "share-public.png",
    "sso-authed.png",
    "sso-guest.png",
    "sso-login.png",
    "stats-fullscreen.png",
    "stats-page.png",
    "stats-ui-empty.png",
    "stats-ui-fullscreen.png",
    "stats-ui.png",
    "support-ui-admin-replied.png",
    "support-ui-user-closed.png",
    "support-ui-user-created.png",
    "switcher-b.png",
    "switcher-kiosk.png",
    "switcher-matrix.png",
    "switcher-mobile.png",
    "switcher-root.png",
    "switcher-staff.png",
    "system-admin.png",
    "unrecognized-ui.png",
    "upgrade-smoke.log",
    "verify-offline-full.log",
    "verify-offline.log",
)


class CheckError(RuntimeError):
    pass


def _read(path: Path) -> str:
    if not path.exists():
        raise CheckError(f"缺少文件：{path.relative_to(SERVICE_DIR)}")
    return path.read_text(encoding="utf-8")


def check_documents() -> list[str]:
    failures: list[str] = []
    texts = {}
    for label, path in (("验收用例清单", CHECKLIST), ("演示脚本", DEMO), ("佐证材料清单", EVIDENCE)):
        try:
            texts[label] = _read(path)
        except CheckError as exc:
            failures.append(str(exc))
    if failures:
        return failures

    checklist = texts["验收用例清单"]
    missing = [rid for rid in REQUIREMENT_IDS if rid not in checklist]
    if missing:
        failures.append(f"验收用例清单缺少需求编号：{', '.join(missing)}")

    missing_ac = [ac for ac in AC_IDS if ac not in checklist]
    if missing_ac:
        failures.append(f"验收用例清单缺少采购条款编号：{', '.join(missing_ac)}")

    demo = texts["演示脚本"]
    demo_marks = demo.count("■")
    if demo_marks < 6:
        failures.append(f"演示脚本仅出现 {demo_marks} 次 ■，应覆盖 6 个演示项")
    if "15 分钟" not in demo:
        failures.append("演示脚本未声明 15 分钟时长")
    for token in ("■1", "■2", "■3", "■4", "■5", "■6"):
        if token not in demo:
            failures.append(f"演示脚本缺少 {token} 段落")

    evidence = texts["佐证材料清单"]
    for token in ("★", "▲", "■", "承诺函"):
        if token not in evidence:
            failures.append(f"佐证材料清单缺少「{token}」类材料")

    artifacts = SERVICE_DIR / "artifacts"
    missing_files = [name for name in EVIDENCE_FILES if not (artifacts / name).exists()]
    if missing_files:
        failures.append(f"佐证文件不存在：{', '.join(missing_files)}")

    return failures


def _data(resp) -> dict:
    if resp.status_code >= 400:
        raise CheckError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        body = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise CheckError(f"响应不是 JSON：{exc}") from exc
    if not body.get("ok"):
        raise CheckError(f"接口返回失败：{body.get('error') or body}")
    return body["data"]


def _theme(channel: dict) -> dict:
    theme = channel.get("themeConfig") or {}
    if isinstance(theme, str):
        import json

        try:
            theme = json.loads(theme)
        except ValueError as exc:
            raise CheckError(f"themeConfig 不是合法 JSON：{exc}") from exc
    return theme


def _ask(client, agent_id: int, role: str, question: str) -> dict:
    return _data(
        client.post(
            "/api/chat",
            json={
                "question": question,
                "role": role,
                "agentId": agent_id,
                "stream": False,
            },
        )
    )


def _chunk_prefixes(data: dict) -> list[str]:
    cites = data.get("citations") or []
    prefixes = []
    for item in cites:
        chunk_id = str(item.get("chunkId") or "")
        if "-" in chunk_id:
            prefixes.append(chunk_id.split("-", 1)[0])
    return sorted(set(prefixes))


def run_rehearsal() -> tuple[list[dict], list[str]]:
    os.environ["LLM_MOCK"] = "1"
    os.environ["AUTH_ENABLED"] = "false"
    os.environ["RATE_LIMIT_ENABLED"] = "false"

    from fastapi.testclient import TestClient

    from app.main import app

    rows: list[dict] = []
    warnings: list[str] = []
    q = "学费和住宿费标准是多少？"

    def record(key: str, clause: str, expectation: str, actual: str, status: str) -> None:
        rows.append(
            {
                "key": key,
                "clause": clause,
                "expectation": expectation,
                "actual": actual,
                "status": status,
            }
        )

    with TestClient(app) as client:
        health_resp = client.get("/api/health")
        health = health_resp.json() if health_resp.status_code == 200 else {}
        if not health.get("ok"):
            raise CheckError(f"健康检查失败：{health}")
        record(
            "R-00",
            "环境",
            "健康检查通过，MySQL 可用",
            f"store={health.get('store')}，docs={health.get('docs')}，chunks={health.get('chunks')}",
            "PASS",
        )

        channels = {}
        for code in ("demo-route", "demo-finance", "demo-admission"):
            channels[code] = _data(client.get(f"/api/channels/{code}"))
        route = channels["demo-route"]
        agents = {a.get("agentCode"): int(a.get("agentId")) for a in route.get("agents") or []}
        admission = agents.get("demo-admission")
        finance = agents.get("demo-finance")
        if not admission or not finance:
            raise CheckError("demo-route 未绑定 demo-admission / demo-finance，请先执行 seed 脚本")

        roles = {
            "访客": admission,
            "本科生": admission,
            "研究生": finance,
            "教职工": finance,
            "离退休人员": admission,
        }
        mismatched = []
        welcome = {}
        for role, expected in roles.items():
            cfg = _data(client.get("/api/channels/demo-route", params={"role": role}))
            welcome[role] = cfg.get("welcomeText") or ""
            if int(cfg.get("defaultAgentId") or 0) != expected:
                mismatched.append(f"{role}->{cfg.get('defaultAgentId')}（期望 {expected}）")
        if mismatched:
            record("R-01", "■1 / AC-01", "五角色路由到招生/财务助手", "；".join(mismatched), "FAIL")
        else:
            record(
                "R-01",
                "■1 / AC-01",
                "五角色路由到招生/财务助手",
                f"访客/本科/离退休->{admission}，研究生/教职工->{finance}",
                "PASS",
            )

        guest_answer = _ask(client, admission, "访客", q)
        staff_answer = _ask(client, finance, "教职工", q)
        guest_prefix = _chunk_prefixes(guest_answer)
        staff_prefix = _chunk_prefixes(staff_answer)
        guest_ok = "D007" in guest_prefix
        staff_ok = "D002" in staff_prefix
        if guest_ok and staff_ok and guest_prefix != staff_prefix:
            record(
                "R-02",
                "■1 / AC-01",
                "同题不同答：访客 D007，教职工 D002",
                f"访客 {guest_prefix}；教职工 {staff_prefix}",
                "PASS",
            )
        else:
            record(
                "R-02",
                "■1 / AC-01",
                "同题不同答：访客 D007，教职工 D002",
                f"访客 {guest_prefix}；教职工 {staff_prefix}",
                "FAIL",
            )
        if not guest_answer.get("aiGenerated") or not guest_answer.get("aiLabel"):
            warnings.append("R-02 回答缺少 aiGenerated/aiLabel，请检查 AI 标识")
        if welcome.get("访客") and welcome.get("教职工") and welcome["访客"] == welcome["教职工"]:
            warnings.append("R-01 访客与教职工欢迎语相同，演示前请确认角色化欢迎语")

        finance_theme = _theme(channels["demo-finance"])
        admission_theme = _theme(channels["demo-admission"])
        theme_ok = (
            finance_theme.get("primaryColor")
            and admission_theme.get("primaryColor")
            and finance_theme["primaryColor"] != admission_theme["primaryColor"]
            and finance_theme.get("assistantName")
            and admission_theme.get("assistantName")
            and finance_theme.get("faq")
            and admission_theme.get("faq")
        )
        record(
            "R-03",
            "■2 / AC-02",
            "两个渠道主题色/形象/FAQ 独立",
            (
                f"财务 {finance_theme.get('primaryColor')}/{finance_theme.get('assistantName')}/"
                f"FAQ {len(finance_theme.get('faq') or [])}；"
                f"招生 {admission_theme.get('primaryColor')}/{admission_theme.get('assistantName')}/"
                f"FAQ {len(admission_theme.get('faq') or [])}"
            ),
            "PASS" if theme_ok else "FAIL",
        )

        guest_apps = _data(client.get("/api/apps", params={"role": "访客"}))
        staff_apps = _data(client.get("/api/apps", params={"role": "教职工"}))
        guest_names = [str(a.get("appName") or "") for a in guest_apps.get("apps") or []]
        staff_names = [str(a.get("appName") or "") for a in staff_apps.get("apps") or []]
        guest_ok = len(guest_names) >= 3 and not any("财务" in n or "国资" in n for n in guest_names)
        staff_ok = any("财务" in n for n in staff_names) and any("国资" in n for n in staff_names)
        record(
            "R-04",
            "★2 / AC-13",
            "访客 3 个应用；教职工可见财务/国资",
            f"访客 {guest_names}；教职工 {staff_names}",
            "PASS" if guest_ok and staff_ok else "FAIL",
        )

        perms = _data(client.get("/api/admin/kb-permissions"))
        liaison = next(
            (row for row in perms.get("liaisons") or [] if row.get("userNo") == "T20001"),
            None,
        )
        if liaison is None:
            record(
                "R-05",
                "■3 / AC-03",
                "T20001 财务处联络员拥有五操作位",
                "未找到 T20001，请先执行 seed_acceptance_demo.py",
                "FAIL",
            )
        else:
            items = [
                row
                for row in perms.get("items") or []
                if int(row.get("userId") or 0) == int(liaison.get("userId") or 0)
            ]
            finance_items = [row for row in items if "财务" in str(row.get("dept") or "")]
            full = any(
                int(row.get("canUpload") or 0)
                and int(row.get("canView") or 0)
                and int(row.get("canDownload") or 0)
                and int(row.get("canDelete") or 0)
                and int(row.get("canCreateSub") or 0)
                for row in finance_items
            )
            record(
                "R-05",
                "■3 / AC-03",
                "T20001 财务处联络员拥有五操作位",
                f"userId={liaison.get('userId')}，财务处授权 {len(finance_items)} 条，五操作位={bool(full)}",
                "PASS" if full else "FAIL",
            )

        access = _data(client.get("/api/admin/access/overview"))
        summary = access.get("summary") or {}
        report = client.get("/api/admin/access/report")
        report_ok = report.status_code == 200 and "通行" in report.text
        total = int(summary.get("total") or 0)
        record(
            "R-06",
            "■4 / AC-04",
            "看板结构完整、有数据、报告可导出",
            f"total={total}，persons={summary.get('persons')}，abnormal={summary.get('abnormal')}，报告={report_ok}",
            "PASS" if total > 0 and summary and report_ok else "WARN",
        )
        if total <= 0:
            warnings.append("R-06 通行看板无数据，请执行 migrate_access_4_4.py --seed")

        question = "火星基地什么时候开放？"
        _ask(client, admission, "访客", question)
        pool = _data(
            client.get(
                "/api/admin/unrecognized",
                params={"q": "火星基地", "pageSize": 50},
            )
        )
        hit = any("火星基地" in str(item.get("question") or "") for item in pool.get("items") or [])
        record(
            "R-07",
            "■5 / AC-05",
            "无知识问题自动进入未识别池",
            f"命中={hit}，当前筛选 total={pool.get('total')}，阈值={pool.get('threshold')}",
            "PASS" if hit else "WARN",
        )
        if not hit:
            warnings.append("R-07 未命中未识别池：确认 UNRECOGNIZED_ENABLED=1 与阈值配置")

        overview = _data(client.get("/api/admin/stats/overview"))
        rankings = _data(client.get("/api/admin/stats/rankings"))
        stat_summary = overview.get("summary") or {}
        stat_ok = (
            isinstance(stat_summary, dict)
            and "questions" in stat_summary
            and isinstance(overview.get("trend"), list)
            and isinstance(rankings, dict)
        )
        record(
            "R-08",
            "■6 / AC-06",
            "统计概览/趋势/排行接口可用",
            (
                f"questions={stat_summary.get('questions')}，"
                f"sessions={stat_summary.get('sessions')}，"
                f"trend={len(overview.get('trend') or [])} 天，"
                f"rankings={len(rankings)} 组"
            ),
            "PASS" if stat_ok else "FAIL",
        )

    return rows, warnings


def write_rehearsal(rows: list[dict], warnings: list[str], failures: list[str]) -> None:
    ACC_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    passed = sum(1 for row in rows if row["status"] == "PASS")
    warned = sum(1 for row in rows if row["status"] == "WARN")
    failed = sum(1 for row in rows if row["status"] == "FAIL")
    lines = [
        "# 验收彩排记录",
        "",
        f"> 时间：{now}｜模式：`LLM_MOCK=1`、`AUTH_ENABLED=false`、`RATE_LIMIT_ENABLED=false`",
        f"> 结果：**PASS {passed} / WARN {warned} / FAIL {failed}**",
        "",
        "## 检查点",
        "",
        "| 编号 | 条款 | 预期 | 实际 | 结果 |",
        "|------|------|------|------|------|",
    ]
    for row in rows:
        lines.append(
            f"| {row['key']} | {row['clause']} | {row['expectation']} | "
            f"{row['actual']} | **{row['status']}** |"
        )
    lines += ["", "## 说明", ""]
    if warnings:
        lines += [f"- WARN：{w}" for w in warnings]
    else:
        lines.append("- 无 WARN。")
    lines.append(
        "- 本记录为接口级彩排；页面视觉与交互以《演示脚本》现场演示为准。"
    )
    lines.append(
        "- ■4 通行查询为管理看板 + 报告，不是自然语言对话查询，验收口径见《验收用例清单》§6 D-01。"
    )
    if failures:
        lines += ["", "## 文档校验失败", ""]
        lines += [f"- {item}" for item in failures]
    lines.append("")
    REHEARSAL.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="验收文档校验与演示彩排")
    parser.add_argument("--rehearse", action="store_true", help="执行六项演示接口彩排并写 rehearsal.md")
    args = parser.parse_args()

    failures = check_documents()
    if failures:
        print("文档校验失败：")
        for item in failures:
            print(f"  - {item}")
    else:
        print(f"文档校验通过：{len(REQUIREMENT_IDS)} 个需求编号、19 个采购条款、{len(EVIDENCE_FILES)} 个证据文件。")

    if not args.rehearse:
        return 1 if failures else 0

    try:
        rows, warnings = run_rehearsal()
    except Exception as exc:  # noqa: BLE001
        print(f"彩排失败：{type(exc).__name__}: {exc}")
        rows, warnings = [], []
        failures.append(f"彩排异常：{type(exc).__name__}: {exc}")
    write_rehearsal(rows, warnings, failures)

    for row in rows:
        print(f"{row['status']:<4} {row['key']} {row['clause']}：{row['actual']}")
    for item in warnings:
        print(f"WARN {item}")
    print(f"彩排记录：{REHEARSAL.relative_to(SERVICE_DIR)}")

    return 1 if failures or any(row["status"] == "FAIL" for row in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
