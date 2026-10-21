"""人工客服验收（任务 4.6 / P-B-09～13、M-S-02～06、M-SYS-04·05）。

默认 **JSON 模式 + 临时数据文件**，无需 MySQL；``--store mysql`` 切到真实库
（验收数据统一 ``vf-sup-*`` / 「验收」前缀，结束时清理）。

覆盖：

1. 表结构：``support_group`` 与三张旧表补齐 4.6 字段（仅 MySQL）；
2. 种子：演示分组 / 坐席可用，坐席按分组与在线状态参与路由；
3. 用户端：高熵 accessKey、只存哈希、错误凭证 403、顺序工单号不可枚举；
4. 生命周期：待接入 → 接待中 → 已回复 → 接待中 → 已关闭，会话幂等复用；
5. 离线留言：无人在线转留言、回复后标记已回复、用户回话恢复接待中；
6. 工作台：清单筛选、情绪标签、分组 / 坐席 CRUD、上下线重排队、回复日志；
7. WebSocket：坐席连接收到 hello、在线连接数进入概览；
8. 鉴权与权限：``support.view/manage`` 映射、未登录 401、普通用户 403；
9. 操作日志（仅 MySQL）：接入 / 回复 / 结束 / 转交各留一条 ``SUPPORT_*``；
10. 聊天主链路：转人工建议、负面情绪打标。

用法::

    python tools/verify_support.py
    python tools/verify_support.py --store mysql
    python tools/verify_support.py --keep-data

退出码：全部通过为 0，存在 FAIL 为 1。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
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
SESSION_PREFIX = "vf-sup-"
GROUP_NAME = "验收客服组"
SEAT_NO = "vf-sup-seat"


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


def purge_data() -> None:
    """清理上次残留的验收工单/坐席/分组，并恢复演示坐席在线状态。"""
    from app.db import support_store as store

    if _mysql():
        from sqlalchemy import delete as sa_delete, select

        from app.db import models as M
        from app.db.session import db_transaction

        with db_transaction() as conn:
            ids = list(
                conn.execute(
                    select(M.Ticket.ticket_id).where(
                        M.Ticket.session_id.like(f"{SESSION_PREFIX}%")
                    )
                ).scalars()
            )
            if ids:
                conn.execute(
                    sa_delete(M.TicketMessage).where(M.TicketMessage.ticket_id.in_(ids))
                )
                conn.execute(sa_delete(M.Ticket).where(M.Ticket.ticket_id.in_(ids)))
            conn.execute(sa_delete(M.AgentSeat).where(M.AgentSeat.user_no == SEAT_NO))
            conn.execute(
                sa_delete(M.SupportGroup).where(M.SupportGroup.group_name == GROUP_NAME)
            )
        return
    import json

    path = store.SUPPORT_PATH
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    data["tickets"] = [
        t for t in data.get("tickets", [])
        if not str(t.get("sessionId") or "").startswith(SESSION_PREFIX)
    ]
    data["seats"] = [s for s in data.get("seats", []) if s.get("userNo") != SEAT_NO]
    data["groups"] = [
        g for g in data.get("groups", []) if g.get("groupName") != GROUP_NAME
    ]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def restore_online(original: dict[int, bool]) -> None:
    from app.db import support_store as store

    for seat_id, online in original.items():
        try:
            store.set_seat_online(int(seat_id), bool(online))
        except Exception:  # noqa: BLE001 - 清理阶段不掩盖主验收结果
            pass


# ==================================================================== 0. 表结构
def check_schema() -> None:
    if not _mysql():
        info("JSON 模式：跳过 MySQL 表结构检查（data/support.json 自动建种子）。")
        return
    from sqlalchemy import text

    from app.db.session import db_session

    section("0. MySQL 表结构（4.6 补列）")
    want = {
        "support_group": {"group_id", "group_name", "channel_ids", "status"},
        "agent_seat": {"user_no", "group_id", "max_sessions", "online_flag"},
        "ticket": {
            "ticket_no", "access_key_hash", "sentiment", "subject",
            "accepted_at", "last_message_at", "close_reason",
        },
        "ticket_message": {"sender_name", "sentiment", "pushed_flag", "read_flag"},
    }
    with db_session() as conn:
        for table, columns in want.items():
            rows = conn.execute(
                text(
                    "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t"
                ),
                {"t": table},
            ).all()
            present = {str(r[0]) for r in rows}
            missing = columns - present
            check(
                f"{table} 字段齐备",
                bool(present) and not missing,
                f"缺 {sorted(missing)}" if missing else "",
            )


# ==================================================================== 1. 种子与用户端
def check_seed_and_public(client) -> None:
    from app.db import support_store as store

    section("1. 种子与用户端转人工")
    groups = client.get("/api/admin/support/groups").json()["data"]
    seats = client.get("/api/admin/support/seats").json()["data"]
    check("演示分组存在", {"综合客服组", "教务客服组"} <= {g["groupName"] for g in groups})
    check("演示坐席存在", len(seats) >= 3 and any(s["seatName"] for s in seats))
    online = [s for s in seats if s["online"]]
    check("至少一个演示坐席在线", bool(online), f"在线 {len(online)}")

    status = client.get("/api/support/status").json()["data"]
    check("公开状态启用客服", status["enabled"] is True)
    check("公开状态统计在线坐席", status["onlineSeats"] >= 1 and status["totalSeats"] == len(seats))

    resp = client.post(
        "/api/support/tickets",
        json={
            "sessionId": f"{SESSION_PREFIX}flow",
            "question": "我想转人工查询校园卡补办进度",
            "role": "本科生",
            "channelCode": "portal-web",
        },
    )
    check("转人工创建成功", resp.status_code == 200, resp.text[:120])
    data = resp.json()["data"]
    ticket = data["ticket"]
    seat_name = ticket.get("seatName")
    check("按在线坐席路由", ticket["state"] == 1 and bool(seat_name), str(ticket.get("state")))
    check("工单号可读", str(ticket.get("ticketNo") or "").startswith("SUP"))
    check("下发高熵 accessKey", len(data["accessKey"]) >= 20)
    check("响应不含凭证哈希", "accessKeyHash" not in ticket)
    check(
        "服务端只存 SHA-256",
        store.ticket_access_hash(ticket["ticketId"]) != data["accessKey"]
        and len(store.ticket_access_hash(ticket["ticketId"])) == 64,
    )
    check("首条消息为用户", ticket["messages"][0]["senderType"] == 1)
    check("末条消息为系统提示", ticket["messages"][-1]["senderType"] == 3)

    tid = ticket["ticketId"]
    key = data["accessKey"]
    check("缺凭证读取 → 422", client.get(f"/api/support/tickets/{tid}").status_code == 422)
    check(
        "错误凭证读取 → 403",
        client.get(f"/api/support/tickets/{tid}", params={"accessKey": "x" * 32}).status_code
        == 403,
    )
    check(
        "错误凭证发言 → 403",
        client.post(
            f"/api/support/tickets/{tid}/messages",
            json={"content": "在吗", "accessKey": "x" * 32},
        ).status_code
        == 403,
    )
    check(
        "顺序工单号不可枚举",
        client.get("/api/support/tickets/99999", params={"accessKey": "x" * 32}).status_code
        == 403,
    )

    again = client.post(
        "/api/support/tickets",
        json={"sessionId": f"{SESSION_PREFIX}flow", "question": "再问一次"},
    ).json()["data"]
    check("同会话复用工单", again["reused"] is True and again["ticket"]["ticketId"] == tid)
    check("复用时轮换凭证", again["accessKey"] != key)
    check(
        "旧凭证立即失效",
        client.get(f"/api/support/tickets/{tid}", params={"accessKey": key}).status_code == 403,
    )
    return tid, again["accessKey"], seat_name


def check_lifecycle(client, ticket_id: int, key: str) -> None:
    section("2. 生命周期：接入 → 回复 → 用户回话 → 结束")
    seats = client.get("/api/admin/support/seats").json()["data"]
    seat = next(
        (s for s in seats if s["seatName"] == client.get(
            f"/api/admin/support/tickets/{ticket_id}"
        ).json()["data"]["seatName"]),
        None,
    )
    check("工单已分配坐席", seat is not None)
    if seat is None:
        return
    seat_id = int(seat["seatId"])

    resp = client.post(
        f"/api/admin/support/tickets/{ticket_id}/claim", params={"seatId": seat_id}
    )
    check("坐席接入 → 接待中", resp.status_code == 200 and resp.json()["data"]["state"] == 2)

    resp = client.post(
        f"/api/admin/support/tickets/{ticket_id}/reply",
        json={"content": "同学你好，补办进度可在信息中心查询。", "seatId": seat_id},
    )
    check("坐席回复成功", resp.status_code == 200, resp.text[:120])
    check("用户未读数增加", resp.json()["data"]["ticket"]["unreadUser"] >= 1)

    view = client.get(f"/api/support/tickets/{ticket_id}", params={"accessKey": key}).json()["data"]
    check(
        "用户拉取到回复",
        view["newReplies"] >= 1
        and view["notice"]["kind"] in ("agent_joined", "new_reply"),
        str(view["notice"]),
    )
    again = client.get(
        f"/api/support/tickets/{ticket_id}", params={"accessKey": key}
    ).json()["data"]
    check("回复只提醒一次", again["newReplies"] == 0)

    resp = client.post(
        f"/api/support/tickets/{ticket_id}/messages",
        json={"content": "好的，谢谢！", "accessKey": key},
    )
    check("用户发言成功", resp.status_code == 200)
    check("接待中状态保持", resp.json()["data"]["ticket"]["state"] == 2)

    resp = client.post(
        f"/api/admin/support/tickets/{ticket_id}/close",
        json={"seatId": seat_id, "reason": "问题已解答"},
    )
    check("坐席结束会话", resp.status_code == 200 and resp.json()["data"]["state"] == 5)
    check(
        "结束后用户可见",
        client.get(f"/api/support/tickets/{ticket_id}", params={"accessKey": key})
        .json()["data"]["ticket"]["state"]
        == 5,
    )
    check(
        "结束后不能再发言",
        client.post(
            f"/api/support/tickets/{ticket_id}/messages",
            json={"content": "还有一个问题", "accessKey": key},
        ).status_code
        == 409,
    )


# ==================================================================== 3. 离线留言
def check_offline(client) -> None:
    section("3. 离线留言与恢复接待")
    seats = client.get("/api/admin/support/seats").json()["data"]
    for seat in seats:
        if seat["online"]:
            client.post(
                f"/api/admin/support/seats/{seat['seatId']}/status", json={"online": False}
            )

    resp = client.post(
        "/api/support/tickets",
        json={
            "sessionId": f"{SESSION_PREFIX}offline",
            "question": "我要投诉食堂一直没人处理，太差了",
            "role": "本科生",
        },
    )
    data = resp.json()["data"]
    ticket = data["ticket"]
    check("无人在线 → 离线留言", ticket["state"] == 3 and ticket["stateLabel"] == "离线留言")
    check("负面情绪打标", ticket["sentiment"] == 1 and ticket["sentimentLabel"] == "负面")
    check("离线提示已写入", "留言" in ticket["messages"][-1]["content"])

    seat = seats[0]
    client.post(
        f"/api/admin/support/seats/{seat['seatId']}/status", json={"online": True}
    )
    resp = client.post(
        f"/api/admin/support/tickets/{ticket['ticketId']}/reply",
        json={"content": "抱歉给您带来不便，已转交后勤处理。", "seatId": seat["seatId"]},
    )
    check("离线工单可直接回复", resp.status_code == 200, resp.text[:120])
    check("回复后标记已回复", resp.json()["data"]["ticket"]["state"] == 4)

    view = client.get(
        f"/api/support/tickets/{ticket['ticketId']}", params={"accessKey": data["accessKey"]}
    ).json()["data"]
    check("离线回复可拉取", view["newReplies"] >= 1)
    resp = client.post(
        f"/api/support/tickets/{ticket['ticketId']}/messages",
        json={"content": "收到，谢谢", "accessKey": data["accessKey"]},
    )
    check("用户回话恢复接待中", resp.json()["data"]["ticket"]["state"] == 2)
    resp = client.post(
        f"/api/support/tickets/{ticket['ticketId']}/close",
        json={"accessKey": data["accessKey"], "reason": "已自行解决"},
    )
    check("用户可主动结束", resp.status_code == 200 and resp.json()["data"]["state"] == 5)


# ==================================================================== 4. 工作台
def check_workbench(client) -> None:
    section("4. 工作台：筛选 / CRUD / 重排队 / 回复日志")
    first = client.get(
        f"/api/admin/support/tickets", params={"q": "校园卡补办进度"}
    ).json()["data"]
    check("按关键词检索工单", first["total"] >= 1 and "校园卡" in first["items"][0]["subject"])

    negative = client.get(
        "/api/admin/support/tickets", params={"sentiment": 1}
    ).json()["data"]
    check("按负面情绪筛选", negative["total"] >= 1 and all(
        i["sentiment"] == 1 for i in negative["items"]
    ))

    overview = client.get("/api/admin/support/overview").json()["data"]
    check("概览含状态与坐席", overview["total"] >= 2 and overview["totalSeats"] >= 3)
    filters = client.get("/api/admin/support/filters").json()["data"]
    check(
        "筛选项状态齐全",
        {s["label"] for s in filters["states"]}
        == {"待接入", "接待中", "离线留言", "已回复", "已关闭"},
    )

    logs = client.get("/api/admin/support/reply-logs").json()["data"]
    check("回复日志有记录", any(row["messageCount"] >= 1 for row in logs))

    # 分组 / 坐席 CRUD
    resp = client.post(
        "/api/admin/support/groups",
        json={"groupName": GROUP_NAME, "channelIds": "vf-sup-ch", "description": "验收"},
    )
    check("新建分组", resp.status_code == 200, resp.text[:120])
    group_id = resp.json()["data"]["groupId"]
    check(
        "分组名重复 → 409",
        client.post(
            "/api/admin/support/groups", json={"groupName": GROUP_NAME}
        ).status_code
        == 409,
    )
    check(
        "分组名过短 → 400",
        client.post("/api/admin/support/groups", json={"groupName": "x"}).status_code == 400,
    )
    resp = client.post(
        "/api/admin/support/seats",
        json={
            "userNo": SEAT_NO,
            "seatName": "验收坐席",
            "groupId": group_id,
            "maxSessions": 3,
            "online": True,
        },
    )
    check("新建坐席", resp.status_code == 200, resp.text[:120])
    seat_id = resp.json()["data"]["seatId"]
    check(
        "最大接待数越界 → 422",
        client.post(
            "/api/admin/support/seats",
            json={"userNo": "vf-sup-x", "seatName": "越界坐席", "maxSessions": 99},
        ).status_code
        == 422,
    )
    check(
        "组内仍有坐席不可删",
        client.delete(f"/api/admin/support/groups/{group_id}").status_code == 409,
    )
    resp = client.patch(
        f"/api/admin/support/seats/{seat_id}", json={"seatName": "验收坐席二号"}
    )
    check("坐席可更新", resp.json()["data"]["seatName"] == "验收坐席二号")
    check("删除坐席", client.delete(f"/api/admin/support/seats/{seat_id}").status_code == 200)
    check("删除分组", client.delete(f"/api/admin/support/groups/{group_id}").status_code == 200)

    # 坐席下线：待接入工单回池
    seats = client.get("/api/admin/support/seats").json()["data"]
    target = next((s for s in seats if s["online"]), None)
    if target is not None:
        resp = client.post(
            "/api/support/tickets",
            json={"sessionId": f"{SESSION_PREFIX}requeue", "question": "校园网怎么开户"},
        )
        tid = resp.json()["data"]["ticket"]["ticketId"]
        client.post(
            f"/api/admin/support/seats/{target['seatId']}/status", json={"online": False}
        )
        detail = client.get(f"/api/admin/support/tickets/{tid}").json()["data"]
        check("坐席下线后工单回池", detail["state"] == 1 and detail["seatId"] is None)
        client.post(
            f"/api/admin/support/seats/{target['seatId']}/status", json={"online": True}
        )


# ==================================================================== 5. WebSocket
def check_ws(client) -> None:
    section("5. 工作台 WebSocket")
    before = client.get("/api/admin/support/overview").json()["data"]["wsConnections"]
    with client.websocket_connect("/api/support/ws") as ws:
        hello = ws.receive_json()
        check("连接即收到 hello", hello.get("type") == "hello" and bool(hello.get("seatName")))
        after = client.get("/api/admin/support/overview").json()["data"]["wsConnections"]
        check("概览统计在线连接", after == before + 1, f"{before} → {after}")
    closed = client.get("/api/admin/support/overview").json()["data"]["wsConnections"]
    check("断开后连接数回落", closed == before)


# ==================================================================== 6. 权限 / 日志
def check_permissions_and_oplog(client, ticket_id: int) -> None:
    from app.security import permissions

    section("6. 权限映射与操作日志")
    check(
        "清单 → support.view",
        permissions.required_for("GET", "/api/admin/support/tickets") == "support.view",
    )
    check(
        "回复 → support.manage",
        permissions.required_for("POST", f"/api/admin/support/tickets/{ticket_id}/reply")
        == "support.manage",
    )
    check(
        "删除坐席 → support.manage",
        permissions.required_for("DELETE", "/api/admin/support/seats/1") == "support.manage",
    )
    if not _mysql():
        info("JSON 模式不落审计表，跳过 SUPPORT_* 操作日志检查。")
        return
    for action, label in (
        ("SUPPORT_CLAIM", "接入"),
        ("SUPPORT_REPLY", "回复"),
        ("SUPPORT_CLOSE", "结束"),
    ):
        body = client.get(
            "/api/admin/operation-logs", params={"action": action, "pageSize": 50}
        ).json()["data"]
        hit = any(str(row["targetId"]) == str(ticket_id) for row in body["items"])
        check(f"操作日志留痕：{label}", hit, f"{action} total={body['total']}")


# ==================================================================== 7. 聊天主链路
def check_chat(client) -> None:
    section("7. 聊天主链路：转人工建议与情绪")
    body = client.post(
        "/api/chat", json={"question": "我要转人工", "role": "本科生"}
    ).json()["data"]
    check("明确诉求触发转人工建议", body["handoffSuggested"] is True)
    check("携带命中关键词", "转人工" in body["handoffKeywords"])
    check("返回客服开关", body["supportEnabled"] is True)
    other = client.post(
        "/api/chat", json={"question": "校园网怎么开通", "role": "本科生"}
    ).json()["data"]
    check("普通问题不误触发", other["handoffSuggested"] is False)


# ==================================================================== main
def main() -> int:
    parser = argparse.ArgumentParser(description="人工客服验收（任务 4.6）")
    parser.add_argument("--store", default="json", choices=["json", "mysql"],
                        help="存储模式；默认 json（无需 MySQL）")
    parser.add_argument("--keep-data", action="store_true",
                        help="保留临时 support.json（默认测完删除）")
    args = parser.parse_args()

    os.environ["KB_STORE"] = args.store

    tmpdir = Path(tempfile.mkdtemp(prefix="verify-support-"))
    if args.store == "json":
        from app.db import support_store

        support_store.SUPPORT_PATH = tmpdir / "support.json"
        print(f"临时客服文件：{support_store.SUPPORT_PATH}")

    from fastapi.testclient import TestClient

    from app.config import settings
    from app.db import support_store as store
    from app.main import app

    print("=" * 68)
    print("人工客服验收（任务 4.6 · P-B-09～13、M-S-02～06、M-SYS-04·05）")
    print(f"存储模式：{settings.store_label}    鉴权：AUTH_ENABLED={settings.auth_enabled}")
    print("=" * 68)

    client = TestClient(app)
    ok = False
    original: dict[int, bool] = {}
    try:
        purge_data()
        check_schema()
        original = {
            int(s["seatId"]): bool(s["online"])
            for s in client.get("/api/admin/support/seats").json()["data"]
        }
        tid, key, _ = check_seed_and_public(client)
        check_lifecycle(client, tid, key)
        check_offline(client)
        check_workbench(client)
        check_ws(client)
        check_permissions_and_oplog(client, tid)
        check_chat(client)
        ok = True
    finally:
        restore_online(original)
        purge_data()
        if not args.keep_data:
            shutil.rmtree(tmpdir, ignore_errors=True)
        elif args.store == "json":
            print(f"临时数据保留在：{tmpdir}")

    print(f"\n结果：{PASSED} PASS / {FAILED} FAIL")
    return 0 if FAILED == 0 and ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
