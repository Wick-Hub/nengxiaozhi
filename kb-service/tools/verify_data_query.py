"""问数服务端到端验收（任务 4.3 / P-DQ-01·02·03）。

覆盖 HTTP 层关键契约（``AUTH_ENABLED=true``，身份取自 token）：

1. 门户与问数静态资源可达；
2. 能力状态：mock 数据源、三类意图、登录要求与行数上限；
3. 匿名查询 401；本科生课表/成绩 200；教职工一卡通 200、成绩 403；
   未映射学号 403；请求体角色不能覆盖 token 身份；
4. 参数：周次/星期过滤、条数上限、非法参数 400、未识别意图 400；
5. 对话非流式与 SSE 均返回 ``dataCards``；访客/越权得到拒绝卡片但不阻断问答；
6. 日志无 500 堆栈。

用法::

    python tools/verify_data_query.py
    python tools/verify_data_query.py --port 8047

退出码：全部通过为 0，存在 FAIL 为 1。
"""

from __future__ import annotations

import argparse
import json
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


def _sse_data(text: str) -> list[dict]:
    events = []
    for line in text.splitlines():
        if line.startswith("data: "):
            try:
                events.append(json.loads(line[6:]))
            except ValueError:
                continue
    return events


def _columns(card: dict) -> list[str]:
    return [str(col.get("key") or "") for col in card.get("columns") or []]


def main() -> int:
    parser = argparse.ArgumentParser(description="问数服务端到端验收（任务 4.3）")
    parser.add_argument("--port", type=int, default=8047, help="验收用服务端口")
    args = parser.parse_args()

    from tools._server import read_log, spawn_server, stop_server, wait_port

    port = args.port
    base = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    env["KB_STORE"] = "json"
    env["AUTH_ENABLED"] = "true"
    env["RATE_LIMIT_ENABLED"] = "false"
    env["LLM_MOCK"] = "1"
    env["DQ_PROVIDER"] = "mock"
    env["DQ_MAX_ROWS"] = "50"

    print(f"启动服务 127.0.0.1:{port}（KB_STORE=json, AUTH_ENABLED=true, DQ_PROVIDER=mock）…")
    proc, log_path = spawn_server(port, env, SERVICE_DIR)
    try:
        if not wait_port(port, timeout=60):
            print("服务启动失败，日志尾部：")
            print(read_log(log_path, 3000))
            return 1

        import httpx

        from app.security.auth import create_token

        student = {
            "Authorization": f"Bearer {create_token('20230001', '本科生', user_id=2, expires_minutes=10)}"
        }
        teacher = {
            "Authorization": f"Bearer {create_token('T10001', '教职工', user_id=3, expires_minutes=10)}"
        }
        unmapped = {
            "Authorization": f"Bearer {create_token('student-042', '本科生', user_id=4, expires_minutes=10)}"
        }

        with httpx.Client(timeout=30, trust_env=False, base_url=base) as cli:
            section("1. 页面与静态资源")
            index = cli.get("/")
            check("门户首页 200", index.status_code == 200, str(index.status_code))
            check("门户加载问答脚本", "app.js" in index.text and "style.css" in index.text)
            app_js = cli.get("/app.js")
            check("app.js 可访问", app_js.status_code == 200)
            check("前端含问数卡片渲染", "addDataCards" in app_js.text and "dq-table" in app_js.text)
            css = cli.get("/style.css")
            check("style.css 可访问", css.status_code == 200)
            check("样式含问数卡片", ".dq-cards" in css.text and ".dq-table" in css.text)

            section("2. 能力状态")
            caps = cli.get("/api/data/capabilities")
            caps_data = caps.json().get("data") or {}
            check("能力接口 200", caps.status_code == 200, str(caps.status_code))
            check("问数已启用", caps_data.get("enabled") is True)
            check("数据源为 mock", caps_data.get("provider") == "mock")
            check("要求登录", caps_data.get("loginRequired") is True)
            check("行数上限 50", caps_data.get("maxRows") == 50, str(caps_data.get("maxRows")))
            intents = caps_data.get("intents") or []
            check("三类意图", [i.get("code") for i in intents] == ["schedule", "grades", "card"])
            check("教务角色限本科/研究生", intents[0].get("roles") == ["本科生", "研究生"])
            check("一卡通含教职工", "教职工" in (intents[2].get("roles") or []))

            section("3. 鉴权与身份")
            anon = cli.post("/api/data/query", json={"question": "本周课表", "role": "本科生"})
            check("匿名查询 401", anon.status_code == 401, str(anon.status_code))
            check("401 提示登录", "登录" in str(anon.json().get("detail") or ""))

            resp = cli.post(
                "/api/data/query",
                json={"question": "本周课表", "role": "研究生"},
                headers=student,
            )
            schedule = (resp.json().get("data") or {})
            check("本科生课表 200", resp.status_code == 200, resp.text[:120])
            check("身份取自 token（非请求角色）",
                  schedule.get("identity", {}).get("role") == "本科生",
                  str(schedule.get("identity")))
            check("学号取自 token",
                  schedule.get("identity", {}).get("userNo") == "20230001")
            check("课表有数据行", len(schedule.get("rows") or []) > 0,
                  str(len(schedule.get("rows") or [])))
            check("课表列契约",
                  _columns(schedule) == ["day", "time", "course", "place", "teacher"],
                  str(_columns(schedule)))
            check("课表含学期周次", "第" in str(schedule.get("subtitle")) and "学期" in str(schedule.get("subtitle")))
            check("课表指标齐全", len(schedule.get("summary") or []) == 3)
            check("课表来源 mock", schedule.get("source") == "mock")

            resp = cli.post(
                "/api/data/query",
                json={"question": "第 3 周周一有什么课"},
                headers=student,
            )
            monday = resp.json().get("data") or {}
            monday_courses = [row.get("course") for row in monday.get("rows") or []]
            check("周次/星期过滤生效", monday_courses == ["高等数学 A（二）", "大学英语（四）"],
                  str(monday_courses))
            check("过滤后标题为周一课表", monday.get("title") == "周一课表", str(monday.get("title")))

            resp = cli.post("/api/data/query", json={"question": "我的成绩"}, headers=student)
            grades = resp.json().get("data") or {}
            check("本科生成绩 200", resp.status_code == 200, resp.text[:120])
            check("成绩 6 门", len(grades.get("rows") or []) == 6, str(len(grades.get("rows") or [])))
            check("成绩加权平均 89.6",
                  any(m.get("value") == "89.6" for m in grades.get("summary") or []),
                  str(grades.get("summary")))
            check("成绩列契约",
                  _columns(grades) == ["course", "type", "credit", "score", "gpa"],
                  str(_columns(grades)))

            resp = cli.post("/api/data/query", json={"question": "一卡通余额"}, headers=teacher)
            card = resp.json().get("data") or {}
            check("教职工一卡通 200", resp.status_code == 200, resp.text[:120])
            check("一卡通身份为陈老师",
                  card.get("identity", {}).get("userName") == "陈老师",
                  str(card.get("identity")))
            check("一卡通余额 ¥152.30",
                  any(m.get("value") == "¥152.30" for m in card.get("summary") or []),
                  str(card.get("summary")))
            check("一卡通流水非空", len(card.get("rows") or []) > 0)
            check("一卡通列契约",
                  _columns(card) == ["time", "place", "type", "amount"],
                  str(_columns(card)))

            resp = cli.post("/api/data/query", json={"question": "我的成绩"}, headers=teacher)
            check("教职工查成绩 403", resp.status_code == 403, str(resp.status_code))
            check("403 说明教务数据", "教务数据" in str(resp.json().get("detail") or ""))

            resp = cli.post("/api/data/query", json={"question": "本周课表"}, headers=unmapped)
            check("未映射学号 403", resp.status_code == 403, str(resp.status_code))
            check("403 说明未映射", "未在教务" in str(resp.json().get("detail") or ""))

            section("4. 参数与错误")
            resp = cli.post(
                "/api/data/query",
                json={"question": "一卡通", "intent": "card", "params": {"limit": 1}},
                headers=student,
            )
            limited = resp.json().get("data") or {}
            check("条数上限生效", len(limited.get("rows") or []) == 1,
                  str(len(limited.get("rows") or [])))

            resp = cli.post(
                "/api/data/query",
                json={"question": "一卡通", "intent": "card", "params": {"days": "x"}},
                headers=student,
            )
            check("非法参数 400", resp.status_code == 400, str(resp.status_code))
            check("400 说明整数参数", "整数" in str(resp.json().get("detail") or ""))

            resp = cli.post(
                "/api/data/query",
                json={"question": "高等数学怎么复习"},
                headers=student,
            )
            check("未识别意图 400", resp.status_code == 400, str(resp.status_code))

            resp = cli.post(
                "/api/data/query",
                json={"question": "一卡通", "intent": "unknown"},
                headers=student,
            )
            check("非法意图 400", resp.status_code == 400, str(resp.status_code))

            section("5. 对话集成")
            resp = cli.post(
                "/api/chat",
                json={"question": "本周课表", "role": "本科生"},
                headers=student,
            )
            data = resp.json().get("data") or {}
            cards = data.get("dataCards") or []
            check("非流式问答 200", resp.status_code == 200, resp.text[:120])
            check("非流式返回问数卡片", bool(cards) and cards[0].get("type") == "schedule")
            check("非流式仍返回回答", bool(data.get("answer")))

            resp = cli.post(
                "/api/chat/stream",
                json={"question": "一卡通余额，最近 7 天", "role": "本科生"},
                headers=student,
            )
            done = [e for e in _sse_data(resp.text) if e.get("type") == "done"]
            check("SSE 问答 200", resp.status_code == 200, str(resp.status_code))
            check("SSE done 含问数卡片",
                  bool(done) and (done[-1].get("dataCards") or [{}])[0].get("type") == "card")

            resp = cli.post(
                "/api/chat",
                json={"question": "校园网怎么开通", "role": "本科生"},
                headers=student,
            )
            check("无问数意图时为空",
                  resp.json().get("data", {}).get("dataCards") == [])

            resp = cli.post(
                "/api/chat",
                json={"question": "我的课表", "role": "本科生"},
            )
            guest_cards = resp.json().get("data", {}).get("dataCards") or []
            check("访客问答不被阻断", resp.status_code == 200 and bool(resp.json().get("data", {}).get("answer")))
            check("访客得到拒绝卡片",
                  bool(guest_cards) and guest_cards[0].get("type") == "denied",
                  str(guest_cards[:1]))
            check("拒绝卡片状态 401", guest_cards and guest_cards[0].get("status") == 401)

            resp = cli.post(
                "/api/chat",
                json={"question": "我的成绩", "role": "本科生"},
                headers=teacher,
            )
            teacher_cards = resp.json().get("data", {}).get("dataCards") or []
            check("教职工问答得到拒绝卡片",
                  bool(teacher_cards) and teacher_cards[0].get("status") == 403,
                  str(teacher_cards[:1]))

            section("6. 日志健康")
            log = read_log(log_path, 20000)
            check("日志无 traceback", "Traceback" not in log)
            check("日志无 500", " 500 " not in log)

    finally:
        stop_server(proc)

    print(f"\n结果：{PASSED} PASS / {FAILED} FAIL")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
