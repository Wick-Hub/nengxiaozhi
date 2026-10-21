"""多智能体切换验收（任务 3.8 / P-QA-06）。

默认 **JSON 模式 + 临时数据文件**；``--store mysql`` 切到真实库
（验收数据统一 ``vf-sw-*`` / ``vf-swa-*`` 前缀，结束时清理）。

覆盖：

1. 引导问题校验：非数组、超过 6 条、单条超过 120 字；
2. 管理端往返：创建带引导问题 → 详情回读 → 清空 → 再写入，去重/去空白；
3. 公开助手矩阵 ``/api/agents?role=``：只出上架且身份可见的助手，
   默认助手恒在首位，携带 ``guideQuestions``，不下发提示词/检索等配置；
4. 渠道助手卡片：按身份过滤绑定助手，并携带场景引导问题；
5. 应用广场：智能体来源的应用携带对应助手的引导问题；
6. 角色归一：未知身份按「访客」过滤。

用法::

    python tools/verify_switcher.py
    python tools/verify_switcher.py --store mysql
    python tools/verify_switcher.py --keep-data

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

CHANNEL_CODE = "vf-sw-api"
AGENT_PREFIX = "vf-swa-"
APP_NAME = "切换验收应用"

GUIDES_A = ["学费什么时候缴纳？", "差旅报销需要哪些材料？"]
GUIDES_B = ["研究生奖学金怎么申请？", "导师双选什么时候开始？"]

CONFIG_KEYS = {
    "systemPrompt", "fallbackReply", "riskReply", "modelCode",
    "temperature", "topP", "maxTokens", "retrievalConfig",
    "visibleRoles", "streamEnabled", "status",
}


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


# ------------------------------------------------------------------ 数据准备
def purge() -> None:
    from app.db import agent_store, app_store, channel_store

    for item in app_store.list_apps():
        if item.get("appName") == APP_NAME:
            app_store.delete_app(item["appId"])
    for item in channel_store.list_channels():
        if str(item.get("channelCode") or "").startswith(CHANNEL_CODE):
            channel_store.delete_channel(item["channelId"])
    for item in agent_store.list_agents():
        if str(item.get("agentCode") or "").startswith(AGENT_PREFIX):
            agent_store.delete_agent(item["agentId"])


def _default_agent() -> dict:
    from app.db import agent_store

    return agent_store.list_agents()[0]


# ==================================================================== 1. 校验
def check_validation() -> None:
    from app.services import agent_admin

    section("1. 引导问题校验")
    base = {"agentName": "校验助手"}

    def fail(code: str, field, keyword: str) -> None:
        r = agent_admin.create_agent(
            {**base, "agentCode": f"{AGENT_PREFIX}{code}", "guideQuestions": field}
        )
        check(
            f"非法配置被拒：{keyword}",
            r["ok"] is False and r.get("status") == 400 and keyword in (r.get("error") or ""),
            f"{r.get('status')} {r.get('error')}",
        )

    fail("bad1", {"nodes": 1}, "字符串数组")
    fail("bad2", [f"问题{i}" for i in range(7)], "最多 6 条")
    fail("bad3", ["问" * 121], "120 字")

    r = agent_admin.create_agent({
        **base,
        "agentCode": f"{AGENT_PREFIX}valid",
        "guideQuestions": "  学费怎么交？ \n差旅怎么报销？\n学费怎么交？",
    })
    check(
        "多行文本按行拆分并去重/去空白",
        r["ok"] and r["data"]["guideQuestions"] == ["学费怎么交？", "差旅怎么报销？"],
        str((r.get("data") or {}).get("guideQuestions")),
    )
    if r["ok"]:
        agent_admin.delete_agent(r["data"]["agentId"])


# ==================================================================== 2. 管理端往返
def check_admin_roundtrip(client) -> tuple[int, int, int]:
    section("2. 管理端创建/回读/清空/再写入")
    default = _default_agent()
    a_id, b_id = 0, 0

    resp = client.post(
        "/api/admin/agents",
        json={
            "agentCode": f"{AGENT_PREFIX}a",
            "agentName": "切换验收助手甲",
            "agentType": "chat",
            "description": "面向全校的通用助手",
            "guideQuestions": GUIDES_A,
        },
    )
    check("创建助手甲 200", resp.status_code == 200, resp.text[:200])
    if resp.status_code == 200:
        a_id = int(resp.json()["data"]["agentId"])

    resp = client.post(
        "/api/admin/agents",
        json={
            "agentCode": f"{AGENT_PREFIX}b",
            "agentName": "切换验收助手乙",
            "agentType": "chat",
            "description": "仅本科生可见",
            "visibleRoles": ["本科生"],
            "guideQuestions": GUIDES_B,
        },
    )
    check("创建助手乙 200（仅本科生）", resp.status_code == 200, resp.text[:200])
    if resp.status_code == 200:
        b_id = int(resp.json()["data"]["agentId"])

    if not (a_id and b_id):
        return int(default["agentId"]), a_id, b_id

    detail = client.get(f"/api/admin/agents/{a_id}").json()["data"]
    check("详情回读引导问题（顺序保持）", detail["guideQuestions"] == GUIDES_A,
          str(detail.get("guideQuestions")))

    r = client.patch(f"/api/admin/agents/{a_id}", json={"guideQuestions": []})
    check("清空引导问题 200", r.status_code == 200 and r.json()["data"]["guideQuestions"] == [],
          r.text[:160])
    r = client.patch(f"/api/admin/agents/{a_id}", json={"guideQuestions": GUIDES_A})
    check("重新写入引导问题 200", r.status_code == 200
          and r.json()["data"]["guideQuestions"] == GUIDES_A, r.text[:160])
    r = client.patch(f"/api/admin/agents/{a_id}",
                     json={"guideQuestions": [f"问题{i}" for i in range(7)]})
    check("接口层超过 6 条 → 400", r.status_code == 400, str(r.status_code))
    return int(default["agentId"]), a_id, b_id


# ==================================================================== 3. 公开矩阵
def check_public_agents(client, default_id: int, a_id: int, b_id: int) -> None:
    section("3. 公开助手矩阵 /api/agents")

    undergrad = client.get("/api/agents", params={"role": "本科生"}).json()["data"]
    ids = [int(x["agentId"]) for x in undergrad["agents"]]
    b_card = next((x for x in undergrad["agents"] if int(x["agentId"]) == b_id), None)
    check("本科生可见助手乙", b_id in ids, str(ids))
    check(
        "公开卡片携带场景引导问题",
        b_card is not None and b_card["guideQuestions"] == GUIDES_B,
        str(b_card)[:200],
    )
    check(
        "默认助手恒在首位且标记 isDefault",
        int(undergrad["agents"][0]["agentId"]) == default_id
        and undergrad["agents"][0]["isDefault"] is True,
        str(ids),
    )
    check("公开接口返回身份与总数",
          undergrad["role"] == "本科生" and undergrad["total"] == len(ids),
          f"{undergrad.get('role')} / {undergrad.get('total')}")

    staff = client.get("/api/agents", params={"role": "教职工"}).json()["data"]
    staff_ids = [int(x["agentId"]) for x in staff["agents"]]
    check("教职工不可见仅本科生的助手乙", b_id not in staff_ids, str(staff_ids))
    check("教职工仍可见全校助手甲", a_id in staff_ids, str(staff_ids))

    unknown = client.get("/api/agents", params={"role": "外星人"}).json()["data"]
    unknown_ids = [int(x["agentId"]) for x in unknown["agents"]]
    check("未知身份按访客过滤（不含本科生助手）",
          unknown["role"] == "访客" and b_id not in unknown_ids,
          f"{unknown.get('role')} {unknown_ids}")

    leak = sorted(CONFIG_KEYS & set(undergrad["agents"][0]))
    check("公开卡片不泄露配置字段", not leak, str(leak))

    no_role = client.get("/api/agents").json()["data"]
    check("缺省 role 按访客返回", no_role["role"] == "访客", no_role.get("role"))


# ==================================================================== 4. 下架
def check_status_filter(client, a_id: int) -> None:
    section("4. 下架助手从公开矩阵消失")
    client.post(f"/api/admin/agents/{a_id}/status", json={"status": 0})
    ids = [int(x["agentId"]) for x in
           client.get("/api/agents", params={"role": "本科生"}).json()["data"]["agents"]]
    check("下架后不可见", a_id not in ids, str(ids))
    client.post(f"/api/admin/agents/{a_id}/status", json={"status": 1})
    ids = [int(x["agentId"]) for x in
           client.get("/api/agents", params={"role": "本科生"}).json()["data"]["agents"]]
    check("重新上架后恢复", a_id in ids, str(ids))


# ==================================================================== 5. 渠道卡片
def check_channel_cards(client, default_id: int, a_id: int, b_id: int) -> None:
    section("5. 渠道助手卡片（绑定集合 + 身份过滤）")
    resp = client.post(
        "/api/admin/channels",
        json={
            "channelCode": CHANNEL_CODE,
            "channelName": "切换验收渠道",
            "defaultAgentId": default_id,
            "welcomeText": "切换验收欢迎",
            "agentIds": [default_id, a_id, b_id],
            "needAuth": 0,
            "status": 1,
        },
    )
    check("创建渠道 200", resp.status_code == 200, resp.text[:200])
    if resp.status_code != 200:
        return

    undergrad = client.get(f"/api/channels/{CHANNEL_CODE}",
                           params={"role": "本科生"}).json()["data"]
    ids = [int(x["agentId"]) for x in undergrad["agents"]]
    card_a = next((x for x in undergrad["agents"] if int(x["agentId"]) == a_id), None)
    check("本科生渠道卡片含甲/乙", a_id in ids and b_id in ids, str(ids))
    check("渠道卡片携带引导问题", card_a is not None and card_a["guideQuestions"] == GUIDES_A,
          str(card_a)[:200])

    staff = client.get(f"/api/channels/{CHANNEL_CODE}",
                       params={"role": "教职工"}).json()["data"]
    staff_ids = [int(x["agentId"]) for x in staff["agents"]]
    check("教职工渠道卡片不含助手乙", b_id not in staff_ids, str(staff_ids))
    check("渠道默认助手不受身份影响", int(staff["defaultAgentId"]) == default_id,
          str(staff.get("defaultAgentId")))


# ==================================================================== 6. 应用广场
def check_app_square(client, b_id: int) -> None:
    section("6. 应用广场智能体卡片携带引导问题")
    resp = client.post(
        "/api/admin/apps",
        json={
            "appName": APP_NAME,
            "sourceType": "agent",
            "agentId": b_id,
            "category": "学业",
            "description": "切换验收用",
            "iconUrl": "🧪",
            "visibleRoles": ["全部"],
            "status": 1,
        },
    )
    check("创建智能体应用 200", resp.status_code == 200, resp.text[:200])
    if resp.status_code != 200:
        return
    apps = client.get("/api/apps", params={"role": "本科生"}).json()["data"]["apps"]
    hit = next((x for x in apps if x["appName"] == APP_NAME), None)
    check("应用广场可见该应用", hit is not None, str([x["appName"] for x in apps]))
    check("应用卡片携带助手引导问题",
          hit is not None and hit["agentId"] == b_id
          and hit["guideQuestions"] == GUIDES_B,
          str(hit)[:220])


# ==================================================================== main
def main() -> int:
    parser = argparse.ArgumentParser(description="多智能体切换验收（任务 3.8）")
    parser.add_argument("--store", default="json", choices=["json", "mysql"])
    parser.add_argument("--keep-data", action="store_true")
    args = parser.parse_args()

    os.environ["KB_STORE"] = args.store

    tmpdir = Path(tempfile.mkdtemp(prefix="verify-switcher-"))
    if args.store == "json":
        from app.db import agent_store, app_store, channel_store

        channel_store.CHANNELS_PATH = tmpdir / "channels.json"
        agent_store.AGENTS_PATH = tmpdir / "agents.json"
        app_store.APPS_PATH = tmpdir / "apps.json"
        print(f"临时数据目录：{tmpdir}")

    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import app

    print("=" * 68)
    print("多智能体切换验收（任务 3.8 · P-QA-06）")
    print(f"存储模式：{settings.store_label}    鉴权：AUTH_ENABLED={settings.auth_enabled}")
    print("=" * 68)

    client = TestClient(app)
    ok = False
    try:
        if args.store == "mysql":
            purge()
            info("已清理上次残留的 vf-sw-* 验收数据")
        check_validation()
        default_id, a_id, b_id = check_admin_roundtrip(client)
        if not (a_id and b_id):
            info("助手创建失败，跳过后续检查")
        else:
            check_public_agents(client, default_id, a_id, b_id)
            check_status_filter(client, a_id)
            check_channel_cards(client, default_id, a_id, b_id)
            check_app_square(client, b_id)
        ok = True
    finally:
        if args.store == "mysql":
            purge()
        if not args.keep_data:
            shutil.rmtree(tmpdir, ignore_errors=True)
        elif args.store == "json":
            print(f"临时数据保留在：{tmpdir}")

    print(f"\n结果：{PASSED} PASS / {FAILED} FAIL")
    return 0 if FAILED == 0 and ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
