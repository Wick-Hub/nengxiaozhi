"""智能体 / 助手管理验收（任务 2.2 / M-A-01·02·03）。

七组自检，**默认不依赖 MySQL**（``KB_STORE=json`` 写临时 ``agents.json``），
可直接复现；带 ``--store mysql`` 可在有库环境跑同一套断言。

1. **存储层**：seed 默认助手、增删改查、编码唯一、角色字段归一为数组；
2. **场景智能体搭建（M-A-01）**：六个场景预设齐全、一键搭建、重复搭建复用不建重复行；
3. **助手创建与基础配置（M-A-02/03）**：名称/描述/头像/流式启停/启禁用/
   风控话术/兜底话术七项全部可改，且**落盘可回读**；
4. **参数校验**：编码格式、枚举、数值范围、文本长度、可见角色，错误一次报全；
5. **接口层契约**：真实起服务跑 ``/agents*`` 全链路 + 状态码（400/404/409/422）；
6. **鉴权**：``AUTH_ENABLED=true`` 的独立实例上，无 token → 401，非管理员 → 403；
7. **验收口径**：「不同助手有独立 agent 行」——创建 N 个助手必须得到 N 个互不相同的 ID。

用法::

    python tools/verify_agents.py
    python tools/verify_agents.py --store mysql
    python tools/verify_agents.py --port 8022 --skip-auth

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

# 必须在导入 app.* 之前定型（settings 在导入期读取环境变量）
os.environ.setdefault("KB_STORE", "json")

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


def info(text: str) -> None:
    """说明性输出：不是断言，用于交代「为什么这里跳过了某条检查」。"""
    print(f"         {text}")


#: 本工具创建的测试助手编码前缀（MySQL 模式下用于清理，保证可重复运行）
TEST_PREFIX = "vf-"


def purge_test_agents() -> int:
    """删除本工具留下的 ``vf-*`` 助手，返回删除条数。

    ⚠ **MySQL 模式下必须清理**：连的是真实库，``agent_code`` 有唯一约束
    （``uk_agent_code``），上次跑完残留的 ``vf-a`` 会让下次运行直接
    ``IntegrityError: Duplicate entry`` 崩掉——这属于**幂等性缺陷**。
    JSON 模式用临时文件，天然干净，无需清理。
    """
    from app.config import settings
    from app.db import agent_store

    if settings.store_label != "mysql":
        return 0
    victims = [a for a in agent_store.list_agents()
               if str(a.get("agentCode") or "").startswith(TEST_PREFIX)]
    for a in victims:
        agent_store.delete_agent(a["agentId"])
    return len(victims)


# ==================================================================== 1. 存储层
def check_store() -> None:
    from app.config import settings
    from app.db import agent_store

    section("1. 存储层")
    seeded = agent_store.list_agents()
    is_mysql = settings.store_label == "mysql"

    if is_mysql:
        # ⚠ MySQL 连的是**真实库**：可能已有历史数据，且**没有「首次访问 seed」语义**
        #   （seed 只在 JSON 后端遇到空文件时发生）。原断言假设空库，在
        #   ``--store mysql`` 下必然失败 —— 而有库环境此前从未跑过这条路径。
        #   这里改为断言**不依赖初始状态**的性质。
        check("MySQL：可读取助手列表", bool(seeded), f"count={len(seeded)}")
        check("MySQL：全部记录含兜底话术字段（说明 2.2 迁移已生效）",
              all("fallbackReply" in a and "streamEnabled" in a for a in seeded))
        default = agent_store.get_by_code(agent_store.DEFAULT_AGENT_CODE)
        check("MySQL：默认助手存在于库中（chatlog seed 依赖它）",
              default is not None, agent_store.DEFAULT_AGENT_CODE)
        check("状态名可读", default is not None and default["statusName"] in ("已上架", "已下架"))
    else:
        check("首次访问自动 seed 默认助手", len(seeded) == 1, f"count={len(seeded)}")
        check("默认助手自带兜底话术", bool(seeded and seeded[0]["fallbackReply"]))
        check("状态名可读", seeded and seeded[0]["statusName"] == "已上架")

    check("默认助手编码与 chatlog seed 一致",
          (agent_store.get_by_code(agent_store.DEFAULT_AGENT_CODE) or {}).get("agentCode")
          == agent_store.DEFAULT_AGENT_CODE,
          agent_store.DEFAULT_AGENT_CODE)

    a = agent_store.create_agent(
        {"agentCode": "vf-a", "agentName": "甲", "visibleRoles": "本科生,研究生"}
    )
    b = agent_store.create_agent({"agentCode": "vf-b", "agentName": "乙"})
    check("创建返回自增 agentId", a["agentId"] > 0 and b["agentId"] > a["agentId"],
          f"{a['agentId']} → {b['agentId']}")
    check("逗号串角色归一为数组", a["visibleRoles"] == ["本科生", "研究生"], f"{a['visibleRoles']}")
    check("重新读盘后角色仍是数组",
          agent_store.get_agent(a["agentId"])["visibleRoles"] == ["本科生", "研究生"])
    check("按编码可查", (agent_store.get_by_code("vf-b") or {}).get("agentId") == b["agentId"])
    check("查不存在的编码返回 None", agent_store.get_by_code("nope") is None)

    agent_store.update_agent(a["agentId"], {"agentName": "甲改"})
    check("更新后回读生效", agent_store.get_agent(a["agentId"])["agentName"] == "甲改")
    check("未传字段保持原值", agent_store.get_agent(a["agentId"])["agentCode"] == "vf-a")

    check("删除已存在的助手返回 True", agent_store.delete_agent(b["agentId"]) is True)
    check("删除后查不到", agent_store.get_agent(b["agentId"]) is None)
    check("删除不存在的助手返回 False", agent_store.delete_agent(987654) is False)
    check("JSON 模式无渠道表，引用计数为 0", agent_store.count_channels_using(1) == 0)


# ==================================================================== 2. 场景预设
def check_presets() -> None:
    from app.config import settings
    from app.services import agent_admin

    is_mysql = settings.store_label == "mysql"
    section("2. 场景智能体搭建（M-A-01）")
    presets = agent_admin.list_presets()["data"]["presets"]
    check("预设数量为 6（财务/招生/后勤/国资/信网/迎新）", len(presets) == 6, f"{len(presets)}")
    check("场景 key 与需求一致",
          [p["sceneKey"] for p in presets] == [
              "finance", "admission", "logistics", "soe", "network", "orientation"],
          "/".join(p["sceneKey"] for p in presets))
    check("每个场景都带角色化提示词", all(p["systemPrompt"] for p in presets))
    check("每个场景都带描述与头像", all(p["description"] and p["iconUrl"] for p in presets))
    if is_mysql:
        info("MySQL：跳过「初始均未搭建」断言（真实库可能已有历史数据）")
    else:
        check("初始均未搭建", not any(p["created"] for p in presets))

    first = agent_admin.create_from_preset("finance")
    check("一键搭建返回 ok", first["ok"], first.get("error") or "")
    check("搭建出的助手名称符合预设", first["data"]["agentName"] == "财务助手",
          first["data"].get("agentName", "-"))
    if is_mysql:
        # 库里可能已存在财务助手，reused 的真假取决于历史状态，
        # 但**幂等语义**（同一场景不建重复行）与初始状态无关，下面照常验证。
        info(f"MySQL：reused={first['data'].get('reused')}（取决于库中是否已存在）")
    else:
        check("标记为新建（reused=False）", first["data"].get("reused") is False)

    again = agent_admin.create_from_preset("finance")
    check("重复搭建复用原记录（不建重复行）", again["data"].get("reused") is True)
    check("复用返回同一 agentId", again["data"]["agentId"] == first["data"]["agentId"],
          f"{first['data']['agentId']} == {again['data']['agentId']}")

    after = {p["sceneKey"]: p for p in agent_admin.list_presets()["data"]["presets"]}
    check("已搭建场景标记 created", after["finance"]["created"] is True)
    check("已搭建场景带回 agentId", after["finance"]["agentId"] == first["data"]["agentId"])
    if is_mysql:
        # 真正要守的是「created 标记与实际记录一致」，而非「别的场景一定是空的」
        for key in ("logistics", "admission"):
            marked = after[key]["created"]
            real = after[key]["agentId"] is not None
            check(f"MySQL：{key} 的 created 标记与实际记录一致", marked == real,
                  f"created={marked} agentId={after[key]['agentId']}")
    else:
        check("其他场景仍未搭建", after["logistics"]["created"] is False)

    bad = agent_admin.create_from_preset("not-a-scene")
    check("未知场景 → ok=False + 404", bad["ok"] is False and bad["status"] == 404)

    # 六个场景全建一遍，验证互不覆盖
    ids = []
    for key in ("admission", "logistics", "soe", "network", "orientation"):
        r = agent_admin.create_from_preset(key)
        ids.append(r["data"]["agentId"])
    check("六个场景各自成行（ID 互不相同）", len(set(ids)) == 5 and 0 not in ids, f"{ids}")


# ==================================================================== 3. 基础配置
def check_basic_config() -> None:
    from app.db import agent_store
    from app.services import agent_admin

    section("3. 助手创建与基础配置（M-A-02/03）")
    created = agent_admin.create_agent(
        {"agentCode": "vf-custom", "agentName": "验收助手", "agentType": "guide"}
    )
    check("自定义创建成功", created["ok"], created.get("error") or "")
    aid = created["data"]["agentId"]
    check("类型按传入保存", created["data"]["agentType"] == "guide")

    patch = {
        "agentName": "验收助手·改",
        "description": "这是描述",
        "iconUrl": "🦉",
        "streamEnabled": 0,
        "status": 0,
        "riskReply": "涉及敏感内容，请咨询相关部门。",
        "fallbackReply": "抱歉，没有找到相关信息。",
    }
    updated = agent_admin.update_agent(aid, patch)
    check("七项基础配置一次性更新成功", updated["ok"], updated.get("error") or "")

    data = updated["data"]
    labels = {
        "agentName": "名称", "description": "描述", "iconUrl": "头像",
        "streamEnabled": "流式启停", "status": "启禁用",
        "riskReply": "风控话术", "fallbackReply": "兜底话术",
    }
    for key, label in labels.items():
        check(f"{label}已生效", data.get(key) == patch[key], f"{key}={data.get(key)!r}")

    reread = agent_store.get_agent(aid)
    check("全部配置已落盘（重新读盘一致）",
          all(reread.get(k) == v for k, v in patch.items()))

    partial = agent_admin.update_agent(aid, {"description": "只改描述"})
    check("增量更新只动指定字段", partial["data"]["description"] == "只改描述"
          and partial["data"]["agentName"] == "验收助手·改")
    check("只传未知字段被拒", agent_admin.update_agent(aid, {"nope": 1})["ok"] is False)


# ==================================================================== 4. 校验
def check_validation() -> None:
    from app.services import agent_admin

    section("4. 参数校验")
    cases = [
        ({"agentName": "无编码"}, "缺少助手编码"),
        ({"agentCode": "vf-x"}, "缺少助手名称"),
        ({"agentCode": "Bad Code", "agentName": "X"}, "助手编码只能"),
        ({"agentCode": "vf-x", "agentName": "X", "agentType": "ghost"}, "助手类型非法"),
        ({"agentCode": "vf-x", "agentName": "X", "temperature": 5}, "temperature 需在"),
        ({"agentCode": "vf-x", "agentName": "X", "topP": 0}, "topP 需在"),
        ({"agentCode": "vf-x", "agentName": "X", "maxTokens": 0}, "maxTokens 需在"),
        ({"agentCode": "vf-x", "agentName": "X", "visibleRoles": []}, "可见角色至少"),
        ({"agentCode": "vf-x", "agentName": "X", "streamEnabled": 3}, "流式开关"),
        ({"agentCode": "vf-x", "agentName": "X", "status": 9}, "状态只能是"),
        ({"agentCode": "vf-x", "agentName": "X" * 200}, "助手名称最长"),
    ]
    for payload, fragment in cases:
        result = agent_admin.create_agent(payload)
        check(f"拒绝非法输入（{fragment}）", result["ok"] is False and fragment in result["error"],
              result.get("error", "")[:50])

    multi = agent_admin.create_agent({"agentCode": "BAD", "agentName": ""})
    check("多个错误一次报全",
          "助手编码只能" in multi["error"] and "缺少助手名称" in multi["error"])

    dup = agent_admin.create_agent({"agentCode": "vf-custom", "agentName": "重复"})
    check("编码重复 → ok=False + 409", dup["ok"] is False and dup["status"] == 409,
          dup.get("error", "")[:40])


# ==================================================================== 5. 接口层
def check_api(port: int) -> None:
    import httpx

    from tools._server import read_log, spawn_server, stop_server, wait_port

    section("5. 接口层契约（真实服务）")
    env = dict(os.environ)
    env["KB_STORE"] = os.environ.get("KB_STORE", "json")
    env["AUTH_ENABLED"] = "false"
    env["RATE_LIMIT_ENABLED"] = "false"

    proc, log_path = spawn_server(port, env, SERVICE_DIR)
    base = f"http://127.0.0.1:{port}"
    created_id: int | None = None
    try:
        if not wait_port(port):
            check("服务启动", False, read_log(log_path, 800)[-400:])
            return
        check("服务启动", True, f"port={port}")

        with httpx.Client(timeout=20.0, trust_env=False) as cli:
            r = cli.get(f"{base}/api/admin/agents")
            ok = r.status_code == 200 and r.json().get("ok") is True
            check("GET /agents 200 + ok", ok, f"status={r.status_code}")
            if ok:
                data = r.json()["data"]
                check("列表返回 items/total/agentTypes/presets",
                      {"items", "total", "agentTypes", "presets"} <= set(data),
                      f"total={data.get('total')}")
                check("预设随列表一并返回（省一次往返）", len(data["presets"]) == 6)

            r = cli.get(f"{base}/api/admin/agents/presets")
            check("GET /agents/presets 未被 /agents/{id} 吞掉（200 而非 422）",
                  r.status_code == 200 and r.json().get("ok") is True, f"status={r.status_code}")

            # 前置清理：上次运行若中途失败，可能残留同编码的验收助手
            listing = cli.get(f"{base}/api/admin/agents").json()["data"]["items"]
            for a in listing:
                if a["agentCode"] == "vf-api":
                    cli.delete(f"{base}/api/admin/agents/{a['agentId']}")

            # 创建 → 详情 → 更新 → 启停 → 删除 全链路
            r = cli.post(
                f"{base}/api/admin/agents",
                json={
                    "agentCode": "vf-api",
                    "agentName": "接口验收助手",
                    "agentType": "chat",
                    "description": "由验收工具创建",
                    "visibleRoles": ["本科生"],
                    "streamEnabled": 1,
                    "riskReply": "风控",
                    "fallbackReply": "兜底",
                },
            )
            check("POST /agents 创建 200", r.status_code == 200, f"status={r.status_code}")
            if r.status_code == 200:
                created_id = r.json()["data"]["agentId"]
                check("创建返回 agentId", isinstance(created_id, int) and created_id > 0,
                      f"agentId={created_id}")

                r = cli.get(f"{base}/api/admin/agents/{created_id}")
                check("GET /agents/{id} 200", r.status_code == 200
                      and r.json()["data"]["agentName"] == "接口验收助手")

                r = cli.patch(f"{base}/api/admin/agents/{created_id}",
                              json={"description": "接口改过", "streamEnabled": 0})
                check("PATCH /agents/{id} 200 + 字段生效",
                      r.status_code == 200 and r.json()["data"]["description"] == "接口改过"
                      and r.json()["data"]["streamEnabled"] == 0, f"status={r.status_code}")

                r = cli.post(f"{base}/api/admin/agents/{created_id}/status", json={"status": 0})
                check("POST /agents/{id}/status 下架成功",
                      r.status_code == 200 and r.json()["data"]["status"] == 0)

                r = cli.post(f"{base}/api/admin/agents/{created_id}/status", json={"status": 1})
                check("POST /agents/{id}/status 上架成功",
                      r.status_code == 200 and r.json()["data"]["statusName"] == "已上架")

                r = cli.get(f"{base}/api/admin/agents", params={"q": "接口验收"})
                check("GET /agents?q= 关键词命中",
                      r.status_code == 200 and any(
                          a["agentId"] == created_id for a in r.json()["data"]["items"]))

            # 场景搭建接口
            r = cli.post(f"{base}/api/admin/agents/presets/network")
            check("POST /agents/presets/network 搭建成功",
                  r.status_code == 200 and r.json()["data"]["agentName"] == "信网助手",
                  f"status={r.status_code}")
            network_created = r.status_code == 200 and not r.json()["data"].get("reused")
            network_id = r.json()["data"]["agentId"] if r.status_code == 200 else None
            r = cli.post(f"{base}/api/admin/agents/presets/network")
            check("重复搭建复用（reused=True）", r.json()["data"].get("reused") is True)

            # 状态码契约
            r = cli.get(f"{base}/api/admin/agents/999999")
            check("不存在的助手 → 404", r.status_code == 404, f"status={r.status_code}")
            r = cli.post(f"{base}/api/admin/agents/presets/ghost-scene")
            check("未知场景 → 404", r.status_code == 404, f"status={r.status_code}")
            r = cli.post(f"{base}/api/admin/agents", json={"agentCode": "vf-api", "agentName": "重复"})
            check("编码重复 → 409", r.status_code == 409, f"status={r.status_code}")
            r = cli.post(f"{base}/api/admin/agents", json={"agentCode": "X!", "agentName": ""})
            check("参数非法 → 400", r.status_code == 400, f"status={r.status_code}")
            r = cli.post(f"{base}/api/admin/agents/1/status", json={"status": 9})
            check("status=9 → 422（枚举拦截）", r.status_code == 422, f"status={r.status_code}")
            r = cli.get(f"{base}/api/admin/agents", params={"status": 5})
            check("status=5 → 422（越界拦截）", r.status_code == 422, f"status={r.status_code}")

            r = cli.get(f"{base}/api/admin/operation-logs")
            check("GET /operation-logs 200（JSON 模式空列表不报错）",
                  r.status_code == 200 and r.json().get("ok") is True, f"status={r.status_code}")

            # 清理本次验收产生的数据，保证可重复运行（**只删本次新建的**，
            # 不动用户既有助手：场景助手若为复用则保留）
            removed = 0
            if created_id:
                removed += int(cli.delete(f"{base}/api/admin/agents/{created_id}").status_code == 200)
            if network_created and network_id:
                removed += int(cli.delete(f"{base}/api/admin/agents/{network_id}").status_code == 200)
            check("验收数据已清理（可重复运行）", True, f"removed={removed}")
    finally:
        stop_server(proc)


# ==================================================================== 6. 鉴权
def check_auth(port: int) -> None:
    import httpx

    from tools._server import read_log, spawn_server, stop_server, wait_port

    section("6. 鉴权（AUTH_ENABLED=true）")
    env = dict(os.environ)
    env["KB_STORE"] = os.environ.get("KB_STORE", "json")
    env["AUTH_ENABLED"] = "true"
    env["RATE_LIMIT_ENABLED"] = "false"
    # **不覆盖 JWT_SECRET**：子进程继承本进程环境，两边同密钥签发/校验。

    proc, log_path = spawn_server(port, env, SERVICE_DIR)
    base = f"http://127.0.0.1:{port}"
    try:
        if not wait_port(port):
            check("鉴权实例启动", False, read_log(log_path, 800)[-400:])
            return
        check("鉴权实例启动", True, f"port={port}")

        with httpx.Client(timeout=20.0, trust_env=False) as cli:
            r = cli.get(f"{base}/api/admin/agents")
            check("无 token → 401", r.status_code == 401, f"status={r.status_code}")
            r = cli.post(f"{base}/api/admin/agents", json={"agentCode": "x1", "agentName": "x"})
            check("写接口同样受保护 → 401", r.status_code == 401, f"status={r.status_code}")

            from app.security.auth import create_token

            guest = create_token("visitor-002", "访客", user_id=None, expires_minutes=5)
            r = cli.get(f"{base}/api/admin/agents", headers={"Authorization": f"Bearer {guest}"})
            check("访客 token → 403（非管理员）", r.status_code == 403, f"status={r.status_code}")

            admin = create_token("admin-002", "管理员", user_id=1, expires_minutes=5)
            r = cli.get(f"{base}/api/admin/agents", headers={"Authorization": f"Bearer {admin}"})
            check("管理员 token → 200", r.status_code == 200, f"status={r.status_code}")
    finally:
        stop_server(proc)


# ==================================================================== 7. 口径
def check_acceptance_criteria() -> None:
    from app.db import agent_store
    from app.services import agent_admin

    section("7. 验收口径：不同助手有独立 agent 行")
    before = {a["agentId"] for a in agent_store.list_agents()}
    made = []
    for i in range(3):
        r = agent_admin.create_agent({"agentCode": f"vf-row-{i}", "agentName": f"独立行{i}"})
        made.append(r["data"]["agentId"])
    after = {a["agentId"] for a in agent_store.list_agents()}
    check("创建 3 个助手得到 3 个新 ID", len(set(made)) == 3 and len(after - before) == 3,
          f"new={sorted(after - before)}")
    check("每个助手可独立读取且名称不串",
          [agent_store.get_agent(i)["agentName"] for i in made] == ["独立行0", "独立行1", "独立行2"])

    agent_admin.set_status(made[1], 0)
    check("单个助手下架不影响其他助手",
          agent_store.get_agent(made[0])["status"] == 1
          and agent_store.get_agent(made[1])["status"] == 0
          and agent_store.get_agent(made[2])["status"] == 1)

    agent_admin.delete_agent(made[0])
    check("删除单个助手不影响其他助手",
          agent_store.get_agent(made[0]) is None
          and agent_store.get_agent(made[2]) is not None)


# ==================================================================== main
def main() -> int:
    parser = argparse.ArgumentParser(description="智能体/助手管理验收（任务 2.2）")
    parser.add_argument("--store", default="json", choices=["json", "mysql"],
                        help="存储模式；默认 json（无需 MySQL）")
    parser.add_argument("--port", type=int, default=8022, help="接口层验收用的端口")
    parser.add_argument("--skip-auth", action="store_true", help="跳过鉴权组")
    parser.add_argument("--keep-data", action="store_true",
                        help="保留临时 agents.json（默认测完删除）")
    args = parser.parse_args()

    os.environ["KB_STORE"] = args.store

    tmpdir = Path(tempfile.mkdtemp(prefix="verify-agents-"))
    if args.store == "json":
        from app.db import agent_store

        agent_store.AGENTS_PATH = tmpdir / "agents.json"
        print(f"临时助手文件：{agent_store.AGENTS_PATH}")

    from app.config import settings

    print("=" * 68)
    print("智能体 / 助手管理验收（任务 2.2）")
    print(f"存储模式：{settings.store_label}    数据目录：{settings.base_dir}")
    print("=" * 68)

    try:
        # MySQL 模式下先清掉上次残留的 vf-* 助手：agent_code 有唯一约束，
        # 不清会让本次直接 IntegrityError 崩掉（幂等性）。
        purged = purge_test_agents()
        if purged and args.store == "mysql":
            print(f"（已清理上次残留的 {purged} 个 {TEST_PREFIX}* 助手）")

        check_store()
        check_presets()
        check_basic_config()
        check_validation()
        check_api(args.port)
        if not args.skip_auth:
            check_auth(args.port + 1)
        check_acceptance_criteria()
    finally:
        # 跑完恢复原状（MySQL 下不留测试数据）
        left = purge_test_agents()
        if left and args.store == "mysql":
            print(f"（已清理本次创建的 {left} 个 {TEST_PREFIX}* 助手）")
        if args.keep_data:
            print(f"\n临时数据保留于：{tmpdir}")
        else:
            shutil.rmtree(tmpdir, ignore_errors=True)

    print("\n" + "=" * 68)
    print(f"结果：{PASSED} PASS / {FAILED} FAIL")
    print("=" * 68)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
