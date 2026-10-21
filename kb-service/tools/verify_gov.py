"""导办事项与免登录票据端到端验收（任务 4.5 / P-DB-01·02、M-G-01、I-03）。

覆盖 HTTP 层的关键契约：

1. 后台 / 大厅页面与静态资源可达；
2. 管理端鉴权（401 / 403 / 200）与 RBAC；
3. mock 同步幂等，且不覆盖本地启停与生效时段；
4. 事项 CRUD、校验、角色授权与生效窗口；
5. 公开检索：角色过滤、关键词匹配、停用 / 过期不可见；
6. 票据：登录签发、身份写入票据、一次性兑换、篡改 / 越权 / 直链；
7. 对话非流式与 SSE 均返回 ``guideItems``，无匹配时为空。

**数据隔离**：验收前备份 ``data/gov_items.json`` / ``data/gov_tickets.json``，
以空数据启动，结束后恢复。

用法::

    python tools/verify_gov.py
    python tools/verify_gov.py --port 8045 --headful

退出码：全部通过为 0，存在 FAIL 为 1。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
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
APP_PREFIX = "vf-gov-"


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


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".verify.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _find(items: list[dict], name: str) -> dict:
    return next((item for item in items if item.get("itemName") == name), {})


def _sse_data(text: str) -> list[dict]:
    events = []
    for line in text.splitlines():
        if line.startswith("data: "):
            try:
                events.append(json.loads(line[6:]))
            except ValueError:
                continue
    return events


def main() -> int:
    parser = argparse.ArgumentParser(description="导办事项与票据端到端验收（任务 4.5）")
    parser.add_argument("--port", type=int, default=8045, help="验收用服务端口")
    parser.add_argument("--headful", action="store_true", help="保留参数，便于与 UI 验收共用")
    args = parser.parse_args()

    from tools._server import read_log, spawn_server, stop_server, wait_port

    data_dir = SERVICE_DIR / "data"
    items_path = data_dir / "gov_items.json"
    tickets_path = data_dir / "gov_tickets.json"
    tmp_dir = Path(tempfile.mkdtemp(prefix="nxz-gov-"))
    backups = {
        "items": _backup(items_path, tmp_dir),
        "tickets": _backup(tickets_path, tmp_dir),
    }
    _write_json(items_path, {"items": {}, "nextId": 1})
    _write_json(tickets_path, {"tickets": {}})

    port = args.port
    base = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    env["KB_STORE"] = "json"
    env["AUTH_ENABLED"] = "true"
    env["RATE_LIMIT_ENABLED"] = "false"
    env["LLM_MOCK"] = "1"
    env["GOV_SYNC_PROVIDER"] = "mock"

    print(f"启动服务 127.0.0.1:{port}（KB_STORE=json, AUTH_ENABLED=true）…")
    proc, log_path = spawn_server(port, env, SERVICE_DIR)
    try:
        if not wait_port(port, timeout=60):
            print("服务启动失败，日志尾部：")
            print(read_log(log_path, 3000))
            return 1

        import httpx

        from app.security.auth import create_token

        admin_token = create_token("admin-001", "管理员", user_id=1, expires_minutes=10)
        student_token = create_token("20230001", "本科生", user_id=2, expires_minutes=10)
        teacher_token = create_token("T1001", "教职工", user_id=3, expires_minutes=10)
        admin_h = {"Authorization": f"Bearer {admin_token}"}
        student_h = {"Authorization": f"Bearer {student_token}"}
        teacher_h = {"Authorization": f"Bearer {teacher_token}"}

        with httpx.Client(timeout=20, trust_env=False, base_url=base) as cli:
            section("1. 页面与静态资源")
            gov_page = cli.get("/gov")
            check("导办后台页 200", gov_page.status_code == 200, str(gov_page.status_code))
            check("导办页加载脚本", "gov.js" in gov_page.text and "导办事项" in gov_page.text)
            hall_page = cli.get("/hall")
            check("大厅页 200", hall_page.status_code == 200, str(hall_page.status_code))
            check("大厅页加载脚本", "hall.js" in hall_page.text)
            check("大厅页禁止 Referer 泄露",
                  'name="referrer"' in hall_page.text and "no-referrer" in hall_page.text)
            check("大厅页带票据参数可打开",
                  cli.get("/hall?ticket=GT-DUMMY").status_code == 200)
            for asset in ("/gov.js", "/gov.css", "/hall.js", "/hall.css"):
                check(f"静态资源 {asset} 可访问", cli.get(asset).status_code == 200)

            section("2. 管理端鉴权")
            check("匿名访问事项清单 401",
                  cli.get("/api/admin/gov/items").status_code == 401)
            check("本科生访问事项清单 403",
                  cli.get("/api/admin/gov/items", headers=student_h).status_code == 403)
            check("匿名同步 401", cli.post("/api/admin/gov/sync").status_code == 401)
            check("本科生同步 403",
                  cli.post("/api/admin/gov/sync", headers=student_h).status_code == 403)
            meta = cli.get("/api/admin/gov/meta", headers=admin_h)
            meta_data = meta.json().get("data") or {}
            check("管理员读取字典 200", meta.status_code == 200, str(meta.status_code))
            check("主管部门字典 9 项", len(meta_data.get("deptOptions") or []) == 9,
                  str(len(meta_data.get("deptOptions") or [])))
            check("身份字典含访客", "访客" in (meta_data.get("roleOptions") or []))
            check("默认票据 TTL 120 秒", meta_data.get("ticketTtlSeconds") == 120,
                  str(meta_data.get("ticketTtlSeconds")))
            check("同步配置为 mock", (meta_data.get("sync") or {}).get("provider") == "mock")

            section("3. 一网通办事项同步（I-03）")
            synced = cli.post("/api/admin/gov/sync", headers=admin_h)
            sync_data = synced.json().get("data") or {}
            check("首次同步 200", synced.status_code == 200, str(synced.status_code))
            check("首次同步新增 9 条",
                  sync_data.get("created") == 9 and sync_data.get("updated") == 0,
                  str(sync_data))
            check("同步无跳过", sync_data.get("skipped") == 0 and sync_data.get("imported") == 9)
            again = cli.post("/api/admin/gov/sync", headers=admin_h).json().get("data") or {}
            check("重复同步不新增",
                  again.get("created") == 0 and again.get("updated") == 9, str(again))

            listed = cli.get("/api/admin/gov/items", headers=admin_h).json().get("data") or {}
            items = listed.get("items") or []
            card_item = _find(items, "校园卡补办")
            card_id = card_item.get("itemId")
            check("同步后事项可查询", len(items) == 9 and bool(card_id), str(len(items)))
            check("汇总口径正确",
                  listed.get("summary", {}).get("total") == 9
                  and listed.get("summary", {}).get("enabled") == 9
                  and listed.get("summary", {}).get("needTicket") == 8,
                  str(listed.get("summary")))

            local_from = (datetime.now() + timedelta(days=3)).isoformat(timespec="minutes")
            patched = cli.patch(
                f"/api/admin/gov/items/{card_id}",
                headers=admin_h,
                json={"status": 0, "effectiveFrom": local_from},
            )
            check("本地停用 + 生效时段 200", patched.status_code == 200,
                  str(patched.status_code))
            cli.post("/api/admin/gov/sync", headers=admin_h)
            kept = cli.get(f"/api/admin/gov/items/{card_id}", headers=admin_h)
            kept_data = kept.json().get("data") or {}
            check("再次同步保留本地停用", kept_data.get("status") == 0,
                  str(kept_data.get("status")))
            check("再次同步保留生效时段",
                  str(kept_data.get("effectiveFrom") or "").startswith(local_from[:10]),
                  str(kept_data.get("effectiveFrom")))
            cli.patch(
                f"/api/admin/gov/items/{card_id}",
                headers=admin_h,
                json={"status": 1, "effectiveFrom": None, "effectiveTo": None},
            )

            section("4. 事项 CRUD 与校验（M-G-01）")
            payload = {
                "itemName": f"{APP_PREFIX}验收事项",
                "deptId": 1,
                "hallUrl": "/hall",
                "needTicket": True,
                "conditions": "仅用于验收，请勿办理。",
                "materials": "无需材料。",
                "keywordTags": ["验收关键词", "临时事项"],
                "visibleRoles": ["本科生"],
                "status": 1,
                "effectiveFrom": "2099-01-01T00:00",
                "effectiveTo": None,
            }
            created = cli.post("/api/admin/gov/items", headers=admin_h, json=payload)
            created_data = created.json().get("data") or {}
            test_id = created_data.get("itemId")
            check("新建事项 200", created.status_code == 200, str(created.status_code))
            check("新建默认未生效", created_data.get("effectiveState") == "scheduled",
                  str(created_data.get("effectiveState")))
            check("新建返回主管部门名", created_data.get("deptName") == "信网中心",
                  str(created_data.get("deptName")))

            bad_name = cli.post(
                "/api/admin/gov/items", headers=admin_h,
                json={**payload, "itemName": "   "},
            )
            check("空名称被拒绝", bad_name.status_code in (400, 422),
                  str(bad_name.status_code))
            bad_url = cli.post(
                "/api/admin/gov/items", headers=admin_h,
                json={**payload, "itemName": f"{APP_PREFIX}坏链接",
                      "hallUrl": "javascript:alert(1)"},
            )
            check("危险链接被拒绝", bad_url.status_code == 400, str(bad_url.status_code))
            bad_dept = cli.post(
                "/api/admin/gov/items", headers=admin_h,
                json={**payload, "itemName": f"{APP_PREFIX}坏部门", "deptId": 999},
            )
            check("非法部门被拒绝", bad_dept.status_code in (400, 422),
                  str(bad_dept.status_code))
            inverted = cli.post(
                "/api/admin/gov/items", headers=admin_h,
                json={**payload, "itemName": f"{APP_PREFIX}倒置窗口",
                      "effectiveFrom": "2099-02-01T00:00",
                      "effectiveTo": "2099-01-01T00:00"},
            )
            check("生效窗口倒置被拒绝", inverted.status_code == 400,
                  str(inverted.status_code))
            duplicate = cli.post("/api/admin/gov/items", headers=admin_h, json=payload)
            check("同部门重名被拒绝", duplicate.status_code == 400,
                  str(duplicate.status_code))

            scheduled = cli.get(
                "/api/admin/gov/items",
                headers=admin_h,
                params={"q": APP_PREFIX, "status": "scheduled"},
            ).json().get("data") or {}
            check("按未生效筛选命中", scheduled.get("total") == 1,
                  str(scheduled.get("total")))

            future_public = cli.get(
                "/api/gov/items", params={"role": "本科生", "q": "验收关键词"}
            ).json().get("data") or {}
            check("未生效事项不对外展示", future_public.get("total") == 0,
                  str(future_public.get("total")))

            opened = cli.patch(
                f"/api/admin/gov/items/{test_id}",
                headers=admin_h,
                json={"effectiveFrom": None, "effectiveTo": None,
                      "visibleRoles": ["全部"]},
            )
            opened_data = opened.json().get("data") or {}
            check("清空生效窗口 200", opened.status_code == 200, str(opened.status_code))
            check("清空后立即生效", opened_data.get("effectiveState") == "active"
                  and opened_data.get("effectiveFrom") is None,
                  str(opened_data.get("effectiveState")))

            by_student = cli.get(
                "/api/gov/items", params={"role": "本科生", "q": "验收关键词"}
            ).json().get("data") or {}
            check("公开检索命中新建事项", by_student.get("total") == 1,
                  str(by_student.get("total")))
            check("公开检索返回匹配理由",
                  (by_student.get("items") or [{}])[0].get("matchReason") == "验收关键词",
                  str((by_student.get("items") or [{}])[0].get("matchReason")))

            cli.patch(f"/api/admin/gov/items/{test_id}", headers=admin_h,
                      json={"visibleRoles": ["教职工"]})
            denied = cli.get(
                "/api/gov/items", params={"role": "本科生", "q": "验收关键词"}
            ).json().get("data") or {}
            allowed = cli.get(
                "/api/gov/items", params={"role": "教职工", "q": "验收关键词"}
            ).json().get("data") or {}
            check("角色不符不展示", denied.get("total") == 0, str(denied.get("total")))
            check("角色匹配可展示", allowed.get("total") == 1, str(allowed.get("total")))

            cli.patch(f"/api/admin/gov/items/{test_id}", headers=admin_h,
                      json={"status": 0})
            disabled = cli.get(
                "/api/gov/items", params={"role": "教职工", "q": "验收关键词"}
            ).json().get("data") or {}
            check("停用事项不对外展示", disabled.get("total") == 0,
                  str(disabled.get("total")))
            deleted = cli.delete(f"/api/admin/gov/items/{test_id}", headers=admin_h)
            check("删除事项 200", deleted.status_code == 200, str(deleted.status_code))
            gone = cli.get(f"/api/admin/gov/items/{test_id}", headers=admin_h)
            check("删除后详情 404", gone.status_code == 404, str(gone.status_code))
            missing_del = cli.delete(f"/api/admin/gov/items/{test_id}", headers=admin_h)
            check("重复删除 404", missing_del.status_code == 404, str(missing_del.status_code))

            section("5. 公开检索（P-DB-01）")
            public = cli.get("/api/gov/items", params={"role": "本科生"})
            public_data = public.json().get("data") or {}
            check("公开清单 200", public.status_code == 200, str(public.status_code))
            check("本科生可见 8 条", public_data.get("total") == 8,
                  str(public_data.get("total")))
            names = [item.get("itemName") for item in public_data.get("items") or []]
            check("本科生看不到教职工事项", "教职工收入证明" not in names)
            check("公开卡片不含敏感字段",
                  all("conditions" not in item for item in public_data.get("items") or []))

            teacher = cli.get("/api/gov/items", params={"role": "教职工"}).json().get("data") or {}
            teacher_names = [item.get("itemName") for item in teacher.get("items") or []]
            check("教职工可见收入证明", "教职工收入证明" in teacher_names)
            check("教职工看不到学生事项", "学生证补办" not in teacher_names)

            search = cli.get(
                "/api/gov/items", params={"role": "本科生", "q": "校园卡丢了怎么补办"}
            ).json().get("data") or {}
            first = (search.get("items") or [{}])[0]
            check("关键词检索排序正确", first.get("itemName") == "校园卡补办",
                  str(first.get("itemName")))
            check("检索返回匹配理由", "校园卡" in str(first.get("matchReason")),
                  str(first.get("matchReason")))
            nohit = cli.get(
                "/api/gov/items", params={"role": "本科生", "q": "高等数学怎么复习"}
            ).json().get("data") or {}
            check("无匹配返回空", nohit.get("total") == 0, str(nohit.get("total")))

            section("6. 免登录票据（P-DB-02）")
            anonymous = cli.post(
                f"/api/gov/items/{card_id}/ticket",
                json={"itemId": card_id, "role": "本科生"},
            )
            check("未登录不能取票 401", anonymous.status_code == 401,
                  str(anonymous.status_code))

            ticket_resp = cli.post(
                f"/api/gov/items/{card_id}/ticket",
                headers=student_h,
                json={"itemId": card_id, "role": "本科生"},
            )
            ticket_data = ticket_resp.json().get("data") or {}
            ticket = ticket_data.get("ticket") or ""
            check("登录后取票 200", ticket_resp.status_code == 200,
                  str(ticket_resp.status_code))
            check("票据为高熵随机串", ticket.startswith("GT") and len(ticket) >= 24, ticket[:10])
            check("跳转链接携带票据",
                  ticket_data.get("jumpUrl") == f"/hall?ticket={ticket}",
                  str(ticket_data.get("jumpUrl")))
            check("票据不在链接中暴露身份", "20230001" not in str(ticket_data.get("jumpUrl")))
            check("票据有效期 120 秒", ticket_data.get("expiresIn") == 120,
                  str(ticket_data.get("expiresIn")))

            mismatch = cli.post(
                f"/api/gov/items/{card_id}/ticket",
                headers=student_h,
                json={"itemId": card_id + 1},
            )
            check("路径与载荷事项不一致 400", mismatch.status_code == 400,
                  str(mismatch.status_code))

            forbidden = cli.post(
                "/api/admin/gov/items",
                headers=student_h,
                json={**payload, "itemName": f"{APP_PREFIX}越权新建"},
            )
            check("学生不能新建事项 403", forbidden.status_code == 403,
                  str(forbidden.status_code))

            teacher_item = _find(
                cli.get("/api/admin/gov/items", headers=admin_h).json()["data"]["items"],
                "教职工收入证明",
            )
            role_denied = cli.post(
                f"/api/gov/items/{teacher_item['itemId']}/ticket",
                headers=student_h,
                json={"itemId": teacher_item["itemId"]},
            )
            check("身份无权取票 403", role_denied.status_code == 403,
                  str(role_denied.status_code))

            free_item = _find(
                cli.get("/api/admin/gov/items", headers=admin_h).json()["data"]["items"],
                "图书馆座位预约",
            )
            free = cli.post(
                f"/api/gov/items/{free_item['itemId']}/ticket",
                headers=student_h,
                json={"itemId": free_item["itemId"]},
            )
            free_data = free.json().get("data") or {}
            check("免票事项直接跳转",
                  free.status_code == 200 and free_data.get("needTicket") is False
                  and free_data.get("ticket") is None, str(free_data))

            verified = cli.post("/api/gov/tickets/verify", json={"ticket": ticket})
            verified_data = verified.json().get("data") or {}
            identity = verified_data.get("identity") or {}
            check("大厅兑换票据 200", verified.status_code == 200,
                  str(verified.status_code))
            check("票据身份来自登录态", identity.get("userNo") == "20230001"
                  and identity.get("role") == "本科生", str(identity))
            check("兑换返回事项办理信息",
                  (verified_data.get("item") or {}).get("itemName") == "校园卡补办"
                  and "身份证" in str((verified_data.get("item") or {}).get("materials")),
                  str((verified_data.get("item") or {}).get("itemName")))
            reused = cli.post("/api/gov/tickets/verify", json={"ticket": ticket})
            check("票据只能兑换一次 409", reused.status_code == 409,
                  str(reused.status_code))

            bad_format = cli.post("/api/gov/tickets/verify", json={"ticket": "bad ticket!"})
            check("非法票据格式 400", bad_format.status_code == 400,
                  str(bad_format.status_code))
            unknown = cli.post("/api/gov/tickets/verify", json={"ticket": "GTnotthere000000"})
            check("未知票据 404", unknown.status_code == 404, str(unknown.status_code))

            teacher_ticket = cli.post(
                f"/api/gov/items/{teacher_item['itemId']}/ticket",
                headers=teacher_h,
                json={"itemId": teacher_item["itemId"]},
            ).json().get("data") or {}
            teacher_verified = cli.post(
                "/api/gov/tickets/verify", json={"ticket": teacher_ticket.get("ticket")}
            )
            check("教职工可办理专属事项",
                  teacher_verified.status_code == 200
                  and (teacher_verified.json()["data"]["identity"]["role"] == "教职工"),
                  str(teacher_verified.status_code))

            section("7. 对话导办卡片")
            non_stream = cli.post(
                "/api/chat",
                headers=student_h,
                json={"question": "校园卡丢了怎么补办？", "role": "本科生"},
            )
            chat_data = non_stream.json().get("data") or {}
            guides = chat_data.get("guideItems") or []
            check("非流式问答推荐事项卡片", len(guides) >= 1,
                  str(len(guides)))
            check("推荐命中校园卡补办",
                  bool(guides) and guides[0].get("itemName") == "校园卡补办",
                  str((guides or [{}])[0].get("itemName")))
            check("卡片含办理入口与票据标记",
                  bool(guides) and guides[0].get("hallUrl") == "/hall"
                  and guides[0].get("needTicket") is True)
            check("卡片含匹配理由", bool(guides) and bool(guides[0].get("matchReason")),
                  str((guides or [{}])[0].get("matchReason")))

            unrelated = cli.post(
                "/api/chat",
                headers=student_h,
                json={"question": "高等数学怎么复习？", "role": "本科生"},
            ).json().get("data") or {}
            check("无关问题不推荐", unrelated.get("guideItems") == [],
                  str(unrelated.get("guideItems")))

            stream = cli.post(
                "/api/chat/stream",
                headers=student_h,
                json={"question": "校园卡丢了怎么补办？", "role": "本科生"},
            )
            done_events = [e for e in _sse_data(stream.text) if e.get("type") == "done"]
            stream_guides = (done_events[0].get("guideItems") if done_events else []) or []
            check("SSE done 携带事项卡片", len(stream_guides) >= 1,
                  str(len(stream_guides)))
            check("SSE 卡片同样命中校园卡补办",
                  bool(stream_guides)
                  and stream_guides[0].get("itemName") == "校园卡补办",
                  str((stream_guides or [{}])[0].get("itemName")))

            section("8. 运行时错误")
            errors = read_log(log_path, 20000)
            check("服务端无 500 堆栈", "Traceback (most recent call last)" not in errors)

        print(f"\n结果：{PASSED} PASS / {FAILED} FAIL")
        return 1 if FAILED else 0
    finally:
        stop_server(proc)
        _restore(items_path, backups["items"])
        _restore(tickets_path, backups["tickets"])
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
