"""渠道×角色会话路由验收（任务 3.4 / M-C-07 / P-B-08）。

默认 **JSON 模式 + 临时数据文件**，无需 MySQL 即可复现；``--store mysql``
切到真实库（验收渠道编码统一 ``vf-rt-*``，结束时清理）。

覆盖：

1. 服务层校验：身份白名单/重复、路由助手必须已绑定、引导问题条数与长度、
   模块键与取值、配置总大小；
2. 接口契约：``routeConfig`` 创建/回读/清空，公开接口按 ``?role=`` 解析；
3. 身份归一：未配置身份沿用渠道默认（``matched=false`` + 全模块开启），
   未知身份归一到「访客」；
4. 助手回退：路由助手下架后回退渠道默认助手，但欢迎语/引导问题仍按身份；
5. 解绑收敛：编辑渠道解绑助手时，失效路由自动回退渠道默认（不阻断保存）；
6. 复制继承：副本完整继承角色路由；
7. 操作日志（仅 MySQL）：更新渠道留痕包含 ``routeRoles``。

用法::

    python tools/verify_routes.py
    python tools/verify_routes.py --store mysql
    python tools/verify_routes.py --keep-data

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

CHANNEL_PREFIX = "vf-rt-"
AGENT_PREFIX = "vf-rta-"


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


def purge_mysql() -> tuple[int, int]:
    from app.config import settings
    from app.db import agent_store, channel_store

    if settings.store_label != "mysql":
        return 0, 0
    channels = [c for c in channel_store.list_channels()
                if str(c.get("channelCode") or "").startswith(CHANNEL_PREFIX)]
    for item in channels:
        channel_store.delete_channel(item["channelId"])
    agents = [a for a in agent_store.list_agents()
              if str(a.get("agentCode") or "").startswith(AGENT_PREFIX)]
    for agent in agents:
        agent_store.delete_agent(agent["agentId"])
    return len(channels), len(agents)


def make_agents() -> tuple[int, int]:
    from app.db import agent_store

    ids = []
    for code, name in (
        (f"{AGENT_PREFIX}a", "路由助手甲"),
        (f"{AGENT_PREFIX}b", "路由助手乙"),
    ):
        existing = agent_store.get_by_code(code)
        if existing is None:
            existing = agent_store.create_agent({"agentCode": code, "agentName": name})
        if int(existing.get("status", 1)) != 1:
            agent_store.update_agent(existing["agentId"], {"status": 1})
        ids.append(existing["agentId"])
    return ids[0], ids[1]


def delete_agents(*agent_ids: int) -> None:
    from app.db import agent_store

    for aid in agent_ids:
        if agent_store.get_agent(aid) is not None:
            agent_store.delete_agent(aid)


def route_payload(a_id: int, b_id: int, code: str = "vf-rt-api") -> dict:
    return {
        "channelCode": code,
        "channelName": "路由验收渠道",
        "defaultAgentId": a_id,
        "welcomeText": "渠道通用欢迎语",
        "agentIds": [a_id, b_id],
        "needAuth": 0,
        "status": 1,
        "routeConfig": {
            "routes": [
                {
                    "role": "访客",
                    "agentId": a_id,
                    "welcomeText": "访客欢迎语",
                    "guideQuestions": ["校园网怎么开通？"],
                    "modules": {
                        "appSquare": 0,
                        "shortcuts": 0,
                        "recommendedApps": 1,
                        "serviceEntry": 0,
                        "feedback": 0,
                    },
                },
                {
                    "role": "教职工",
                    "agentId": b_id,
                    "welcomeText": "教职工欢迎语",
                    "guideQuestions": ["差旅怎么报销？", "体检怎么预约？"],
                    "modules": {"recommendedApps": 0},
                },
            ]
        },
    }


# ==================================================================== 1. 校验
def check_validation() -> None:
    from app.services import channel_admin

    section("1. 服务层校验（错误一次报清）")
    a_id, b_id = make_agents()
    base = route_payload(a_id, b_id, "vf-rt-valid")

    def fail(config, keyword: str) -> None:
        r = channel_admin.create_channel({**base, "routeConfig": config})
        check(
            f"非法路由被拒：{keyword}",
            r["ok"] is False and r.get("status") == 400 and keyword in (r.get("error") or ""),
            f"{r.get('status')} {r.get('error')}",
        )

    fail({"routes": "x"}, "数组")
    fail({"routes": [{"role": "游客"}]}, "身份非法")
    fail({"routes": [{"role": "访客"}, {"role": "访客"}]}, "重复")
    fail({"routes": [{"role": "访客", "agentId": 999999}]}, "不在渠道绑定集合")
    fail(
        {"routes": [{"role": "访客", "guideQuestions": [f"问题{i}" for i in range(7)]}]},
        "最多 6 条",
    )
    fail({"routes": [{"role": "访客", "guideQuestions": ["问" * 121]}]}, "不能超过 120")
    fail({"routes": [{"role": "访客", "modules": {"unknown": 1}}]}, "未知功能模块")
    fail({"routes": [{"role": "访客", "modules": {"appSquare": "yes"}}]}, "取值非法")
    check(
        "默认模块全部开启",
        all(v == 1 for v in channel_admin.default_modules().values()),
        str(channel_admin.default_modules()),
    )
    check(
        "身份白名单为五类",
        channel_admin.ROLES == ("访客", "本科生", "研究生", "教职工", "离退休人员"),
        str(channel_admin.ROLES),
    )
    delete_agents(a_id, b_id)


# ==================================================================== 2. 接口与解析
def check_api(client) -> None:
    section("2. 接口契约与公开端解析")
    a_id, b_id = make_agents()
    payload = route_payload(a_id, b_id)
    r = client.post("/api/admin/channels", json=payload)
    check("创建带角色路由的渠道 200", r.status_code == 200, r.text[:200])
    if r.status_code != 200:
        delete_agents(a_id, b_id)
        return
    item = r.json()["data"]
    cid = item["channelId"]
    check(
        "创建回读 routeConfig（顺序与内容）",
        [x["role"] for x in item["routeConfig"]["routes"]] == ["访客", "教职工"]
        and item["routeConfig"]["routes"][0]["modules"]["appSquare"] == 0,
        str(item["routeConfig"])[:160],
    )

    guest = client.get("/api/channels/vf-rt-api?role=访客").json()["data"]
    check(
        "访客命中：路由助手/欢迎语/引导问题",
        guest["defaultAgentId"] == a_id
        and guest["route"]["matched"] is True
        and guest["route"]["welcomeText"] == "访客欢迎语"
        and guest["route"]["guideQuestions"] == ["校园网怎么开通？"],
        f"agentId={guest['defaultAgentId']}",
    )
    check(
        "访客模块开关透传",
        guest["route"]["modules"]["appSquare"] == 0
        and guest["route"]["modules"]["shortcuts"] == 0
        and guest["route"]["modules"]["recommendedApps"] == 1
        and guest["route"]["modules"]["guideQuestions"] == 1,
        str(guest["route"]["modules"]),
    )

    staff = client.get("/api/channels/vf-rt-api?role=教职工").json()["data"]
    check(
        "教职工命中：切换到第二助手",
        staff["defaultAgentId"] == b_id
        and staff["route"]["guideQuestions"] == ["差旅怎么报销？", "体检怎么预约？"]
        and staff["route"]["modules"]["recommendedApps"] == 0,
        f"agentId={staff['defaultAgentId']}",
    )

    grad = client.get("/api/channels/vf-rt-api?role=研究生").json()["data"]
    check(
        "未配置身份沿用渠道默认（matched=false）",
        grad["route"]["matched"] is False
        and grad["route"]["role"] == "研究生"
        and grad["defaultAgentId"] == a_id
        and grad["welcomeText"] == "渠道通用欢迎语"
        and grad["route"]["guideQuestions"] == [],
        f"agentId={grad['defaultAgentId']}",
    )
    from app.services import channel_admin

    check(
        "未配置身份默认全模块开启",
        grad["route"]["modules"] == channel_admin.default_modules(),
        str(grad["route"]["modules"]),
    )

    unknown = client.get("/api/channels/vf-rt-api?role=外星人").json()["data"]
    check(
        "未知身份归一到访客",
        unknown["route"]["role"] == "访客" and unknown["route"]["matched"] is True,
        str(unknown["route"]["role"]),
    )
    default_role = client.get("/api/channels/vf-rt-api").json()["data"]["route"]
    check("缺省 role 按访客解析", default_role["role"] == "访客"
          and default_role["matched"] is True)

    r = client.patch(f"/api/admin/channels/{cid}", json={"routeConfig": {}})
    check("清空路由 200", r.status_code == 200
          and r.json()["data"]["routeConfig"] == {"routes": []}, r.text[:160])
    cleared = client.get("/api/channels/vf-rt-api?role=教职工").json()["data"]
    check(
        "清空后按渠道默认解析",
        cleared["route"]["matched"] is False and cleared["defaultAgentId"] == a_id,
        f"agentId={cleared['defaultAgentId']}",
    )

    bad = client.patch(
        f"/api/admin/channels/{cid}",
        json={"routeConfig": {"routes": [{"role": "游客"}]}},
    )
    check("接口层非法身份 → 400", bad.status_code == 400, str(bad.status_code))

    # 解绑路由助手：失效路由自动回退渠道默认，保存不被旧路由阻断
    client.patch(f"/api/admin/channels/{cid}", json={"routeConfig": payload["routeConfig"]})
    r = client.patch(f"/api/admin/channels/{cid}", json={"agentIds": [a_id]})
    ok = r.status_code == 200
    routes = {}
    if ok:
        routes = {x["role"]: x for x in r.json()["data"]["routeConfig"]["routes"]}
    check(
        "解绑助手后路由自动回退（不报错）",
        ok and routes.get("教职工", {}).get("agentId") is None,
        r.text[:160],
    )
    fallback = client.get("/api/channels/vf-rt-api?role=教职工").json()["data"]
    check(
        "回退后教职工仍保留欢迎语/引导问题",
        fallback["defaultAgentId"] == a_id
        and fallback["route"]["welcomeText"] == "教职工欢迎语"
        and fallback["route"]["guideQuestions"] == ["差旅怎么报销？", "体检怎么预约？"],
        f"agentId={fallback['defaultAgentId']}",
    )

    # 恢复绑定并验证复制继承
    client.patch(f"/api/admin/channels/{cid}", json={"agentIds": [a_id, b_id]})
    client.patch(f"/api/admin/channels/{cid}", json={"routeConfig": payload["routeConfig"]})
    src = client.get(f"/api/admin/channels/{cid}").json()["data"]["routeConfig"]
    copied = client.post(f"/api/admin/channels/{cid}/copy", json={})
    check("复制渠道 200", copied.status_code == 200, copied.text[:160])
    if copied.status_code == 200:
        copy = copied.json()["data"]
        check(
            "副本完整继承角色路由",
            copy["routeConfig"] == src and src["routes"][1]["agentId"] == b_id,
            str(copy["routeConfig"])[:160],
        )
        check("副本默认停用", copy["status"] == 0)
        client.delete(f"/api/admin/channels/{copy['channelId']}")

    # 路由助手下架：回退渠道默认，身份文案仍生效
    client.post(f"/api/admin/agents/{b_id}/status", json={"status": 0})
    staff = client.get("/api/channels/vf-rt-api?role=教职工").json()["data"]
    check(
        "路由助手下架后回退渠道默认助手",
        staff["defaultAgentId"] == a_id
        and staff["route"]["matched"] is True
        and staff["route"]["agentId"] == a_id,
        f"agentId={staff['defaultAgentId']}",
    )
    client.post(f"/api/admin/agents/{b_id}/status", json={"status": 1})

    client.delete(f"/api/admin/channels/{cid}")
    check("删除后公开接口 404",
          client.get("/api/channels/vf-rt-api").status_code == 404)
    delete_agents(a_id, b_id)


# ==================================================================== 3. 操作日志
def check_oplog(client) -> None:
    from app.config import settings

    section("3. 操作日志（仅 MySQL）")
    if settings.store_label != "mysql":
        info("JSON 模式操作日志只写 logger、不落审计表，跳过本组")
        return
    a_id, b_id = make_agents()
    payload = route_payload(a_id, b_id, "vf-rt-log")
    cid = client.post("/api/admin/channels", json=payload).json()["data"]["channelId"]
    client.patch(f"/api/admin/channels/{cid}", json={"routeConfig": payload["routeConfig"]})
    logs = client.get("/api/admin/operation-logs", params={"pageSize": 100}).json()["data"]["items"]
    hits = [
        x for x in logs
        if x.get("action") == "CHANNEL_UPDATE" and x.get("targetId") == str(cid)
    ]
    detail = hits[0].get("detail") if hits else {}
    if isinstance(detail, str):
        import json as json_lib

        try:
            detail = json_lib.loads(detail)
        except ValueError:
            detail = {}
    check("更新渠道留痕含 routeRoles",
          bool(hits) and (detail or {}).get("routeRoles") == ["访客", "教职工"],
          str(detail)[:160])
    client.delete(f"/api/admin/channels/{cid}")
    delete_agents(a_id, b_id)


# ==================================================================== main
def main() -> int:
    parser = argparse.ArgumentParser(description="渠道×角色会话路由验收（任务 3.4）")
    parser.add_argument("--store", default="json", choices=["json", "mysql"])
    parser.add_argument("--keep-data", action="store_true")
    args = parser.parse_args()

    os.environ["KB_STORE"] = args.store

    tmpdir = Path(tempfile.mkdtemp(prefix="verify-routes-"))
    if args.store == "json":
        from app.db import agent_store, channel_store

        channel_store.CHANNELS_PATH = tmpdir / "channels.json"
        agent_store.AGENTS_PATH = tmpdir / "agents.json"
        print(f"临时数据目录：{tmpdir}")

    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import app

    print("=" * 68)
    print("渠道×角色会话路由验收（任务 3.4 · M-C-07 / P-B-08）")
    print(f"存储模式：{settings.store_label}    鉴权：AUTH_ENABLED={settings.auth_enabled}")
    print("=" * 68)

    client = TestClient(app)
    ok = False
    try:
        if args.store == "mysql":
            channels, agents = purge_mysql()
            if channels or agents:
                print(f"（已清理上次残留：{channels} 个渠道 / {agents} 个助手）")
        check_validation()
        check_api(client)
        check_oplog(client)
        ok = True
    finally:
        if args.store == "mysql":
            purge_mysql()
        if not args.keep_data:
            shutil.rmtree(tmpdir, ignore_errors=True)
        elif args.store == "json":
            print(f"临时数据保留在：{tmpdir}")

    print(f"\n结果：{PASSED} PASS / {FAILED} FAIL")
    return 0 if FAILED == 0 and ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
