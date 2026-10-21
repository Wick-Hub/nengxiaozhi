"""应用广场验收（任务 3.3 / M-C-05·06）。

默认 **JSON 模式 + 临时数据文件**，无需 MySQL；``--store mysql`` 切真实库
（验收应用名统一 ``vf-app-*``，结束时清理）。

覆盖：

1. 存储层：新建（智能体 / 外链）、排序、增量更新、删除；
2. 服务层校验：名称/分类/简介、来源、助手存在性、链接协议、图标、排序、角色；
3. 接口契约：管理端 CRUD 全链路 + 400/404/422；
4. 角色授权：不同身份看到不同应用，未知角色回落「访客」，token 角色优先；
5. 来源联动：助手下架/删除后智能体应用自动隐藏，外链应用不受影响；
6. 操作日志（仅 MySQL）：新增/更新/删除各留一条 ``APP_*`` 记录。

用法::

    python tools/verify_apps.py
    python tools/verify_apps.py --store mysql
    python tools/verify_apps.py --keep-data

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

APP_PREFIX = "vf-app-"
AGENT_PREFIX = "vf-appa-"


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
    from app.db import agent_store, app_store

    if settings.store_label != "mysql":
        return 0, 0
    apps = [a for a in app_store.list_apps()
            if str(a.get("appName") or "").startswith(APP_PREFIX)]
    for item in apps:
        app_store.delete_app(item["appId"])
    agents = [a for a in agent_store.list_agents()
              if str(a.get("agentCode") or "").startswith(AGENT_PREFIX)]
    for agent in agents:
        agent_store.delete_agent(agent["agentId"])
    return len(apps), len(agents)


# ==================================================================== 1. 存储层
def check_store() -> None:
    from app.db import app_store

    section("1. 存储层")
    agent_id = make_agent("store", "广场助手·存储")
    link_id = app_store.create_app(
        app_name=f"{APP_PREFIX}link",
        source_type="link",
        link_url="https://ehall.example.edu.cn",
        category="信息服务",
        description="办事大厅",
        icon_url="🏛",
        sort_no=1,
        visible_roles=["全部"],
        status=1,
    )
    agent_app_id = app_store.create_app(
        app_name=f"{APP_PREFIX}agent",
        source_type="agent",
        agent_id=agent_id,
        category="事务办理",
        sort_no=5,
        visible_roles=["教职工", "本科生"],
        status=1,
    )
    check("新建外链应用返回自增 appId", link_id > 0, f"appId={link_id}")
    check("新建智能体应用返回自增 appId", agent_app_id > link_id, f"appId={agent_app_id}")

    items = app_store.list_apps()
    check("列表按 sortNo 升序", [x["appId"] for x in items] == [link_id, agent_app_id],
          str([(x["appName"], x["sortNo"]) for x in items]))
    item = app_store.get_app(agent_app_id)
    check("按 ID 回读成功", item is not None and item["appName"] == f"{APP_PREFIX}agent")
    check("角色归一为身份顺序", item["visibleRoles"] == ["本科生", "教职工"],
          str(item["visibleRoles"]))

    app_store.update_app(agent_app_id, status=0, visible_roles=["研究生"], sort_no=0)
    item = app_store.get_app(agent_app_id)
    check("增量更新生效", item["status"] == 0
          and item["visibleRoles"] == ["研究生"] and item["sortNo"] == 0)
    check("未提交字段保持原值", item["appName"] == f"{APP_PREFIX}agent"
          and item["agentId"] == agent_id)
    check("更新后重排到首位", app_store.list_apps()[0]["appId"] == agent_app_id)

    check("删除已存在应用返回 True", app_store.delete_app(link_id) is True)
    check("删除后查不到", app_store.get_app(link_id) is None)
    check("删除不存在应用返回 False", app_store.delete_app(987654) is False)
    app_store.delete_app(agent_app_id)
    delete_agent(agent_id)


# ==================================================================== 2. 校验
def check_validation() -> None:
    from app.db import app_store
    from app.services import app_admin

    section("2. 服务层校验（错误一次报清）")
    agent_id = make_agent("valid", "广场助手·校验")
    good = {
        "appName": f"{APP_PREFIX}valid",
        "sourceType": "agent",
        "agentId": agent_id,
        "category": "事务办理",
        "visibleRoles": ["全部"],
        "status": 1,
    }

    def fail(payload: dict, keyword: str) -> None:
        r = app_admin.create_app(payload)
        check(
            f"非法输入被拒：{keyword}",
            r["ok"] is False and r.get("status") == 400 and keyword in (r.get("error") or ""),
            f"{r.get('status')} {r.get('error')}",
        )

    fail({**good, "appName": "  "}, "应用名称")
    fail({**good, "appName": "名" * 129}, "不能超过")
    fail({**good, "category": ""}, "应用分类")
    fail({**good, "description": "简" * 256}, "简介")
    fail({**good, "sourceType": "iframe"}, "来源")
    fail({**good, "agentId": None}, "绑定助手")
    fail({**good, "agentId": 999999}, "智能体不存在")
    fail({**good, "sourceType": "link", "linkUrl": ""}, "链接地址")
    fail({**good, "sourceType": "link", "linkUrl": "javascript:alert(1)"}, "协议不安全")
    fail({**good, "sourceType": "link", "linkUrl": "ftp://example.com/a"}, "http(s)")
    fail({**good, "sourceType": "link", "linkUrl": "https://a.example/a b"}, "空白")
    fail({**good, "iconUrl": "data:text/html,x"}, "图标链接协议不安全")
    fail({**good, "sortNo": 10000}, "排序")
    fail({**good, "visibleRoles": ["外星人"]}, "未知角色")

    made = app_admin.create_app(good)
    check("合法输入创建成功", made["ok"] is True, made.get("error") or "")
    if made["ok"]:
        app_id = made["data"]["appId"]
        bad = app_admin.update_app(app_id, {"sourceType": "link"})
        check("更新按合并后整体校验", bad["ok"] is False
              and "链接地址" in (bad.get("error") or ""), str(bad.get("error")))
        app_store.delete_app(app_id)
    delete_agent(agent_id)


# ==================================================================== 3. 接口层
def check_api(client) -> None:
    from app.db import agent_store, app_store
    from app.security import auth

    section("3. 接口契约（TestClient）")
    agent_id = make_agent("api", "广场助手·接口")
    base = {
        "appName": f"{APP_PREFIX}api-all",
        "sourceType": "agent",
        "agentId": agent_id,
        "category": "事务办理",
        "description": "所有人可见",
        "iconUrl": "🤖",
        "sortNo": 1,
        "visibleRoles": ["全部"],
        "status": 1,
    }

    r = client.get("/api/admin/apps")
    check("GET /admin/apps 200", r.status_code == 200 and r.json()["ok"] is True,
          str(r.status_code))
    if r.status_code == 200:
        data = r.json()["data"]
        check("清单返回角色/来源/分类/助手字典",
              {"items", "total", "enabled", "roleOptions",
               "sourceOptions", "categoryOptions", "agentOptions"} <= set(data))

    r = client.post("/api/admin/apps", json=base)
    check("POST /admin/apps 200", r.status_code == 200, r.text[:160])
    app_id = r.json()["data"]["appId"] if r.status_code == 200 else None
    if app_id is not None:
        check("创建返回助手名称/状态", r.json()["data"]["agentName"] == "广场助手·接口"
              and r.json()["data"]["agentStatus"] == 1)

        r = client.post("/api/admin/apps", json={
            **base, "appName": f"{APP_PREFIX}api-staff",
            "visibleRoles": ["教职工"], "sortNo": 2,
        })
        staff_id = r.json()["data"]["appId"]
        r = client.post("/api/admin/apps", json={
            **base, "appName": f"{APP_PREFIX}api-student",
            "visibleRoles": ["本科生", "研究生"], "sortNo": 3,
        })
        student_id = r.json()["data"]["appId"]

        pub = client.get("/api/apps", params={"role": "本科生"}).json()["data"]
        check("本科生看到「全部 + 学生」应用",
              [x["appId"] for x in pub["apps"]] == [app_id, student_id],
              str([x["appName"] for x in pub["apps"]]))
        pub = client.get("/api/apps", params={"role": "教职工"}).json()["data"]
        check("教职工看到「全部 + 教职工」应用",
              [x["appId"] for x in pub["apps"]] == [app_id, staff_id])
        pub = client.get("/api/apps").json()["data"]
        check("未带角色按访客过滤", pub["role"] == "访客"
              and [x["appId"] for x in pub["apps"]] == [app_id])
        pub = client.get("/api/apps", params={"role": "外星人"}).json()["data"]
        check("未知角色回落访客", pub["role"] == "访客" and pub["total"] == 1)
        check("公开端不泄露 visibleRoles",
              "visibleRoles" not in pub["apps"][0], str(sorted(pub["apps"][0])))

        token = auth.create_token("T9001", "教职工")
        pub = client.get("/api/apps", params={"role": "访客"},
                         headers={"Authorization": f"Bearer {token}"}).json()["data"]
        check("token 角色优先于查询参数", pub["role"] == "教职工"
              and student_id not in [x["appId"] for x in pub["apps"]])

        r = client.patch(f"/api/admin/apps/{student_id}",
                         json={"visibleRoles": ["研究生"], "status": 0})
        check("PATCH 角色/状态生效", r.status_code == 200
              and r.json()["data"]["visibleRoles"] == ["研究生"]
              and r.json()["data"]["status"] == 0, r.text[:160])
        pub = client.get("/api/apps", params={"role": "本科生"}).json()["data"]
        check("停用后本科生广场不再出现", student_id not in [x["appId"] for x in pub["apps"]])

        agent_store.update_agent(agent_id, {"status": 0})
        pub = client.get("/api/apps", params={"role": "教职工"}).json()["data"]
        check("助手下架后其应用自动隐藏", pub["apps"] == [],
              str([x["appName"] for x in pub["apps"]]))
        agent_store.update_agent(agent_id, {"status": 1})
        pub = client.get("/api/apps", params={"role": "教职工"}).json()["data"]
        check("助手恢复上架后应用重新出现", len(pub["apps"]) == 2)

        check("非法链接 → 400",
              client.post("/api/admin/apps", json={
                  **base, "appName": f"{APP_PREFIX}api-bad",
                  "sourceType": "link", "agentId": None,
                  "linkUrl": "javascript:alert(1)",
              }).status_code == 400)
        check("不存在的应用 → 404",
              client.get("/api/admin/apps/999999").status_code == 404)
        check("status=9 → 422",
              client.patch(f"/api/admin/apps/{app_id}", json={"status": 9}).status_code == 422)

        for target in (app_id, staff_id, student_id):
            client.delete(f"/api/admin/apps/{target}")
        check("删除后清单为空", app_store.list_apps() == [])

    agent_store.delete_agent(agent_id)


# ==================================================================== 4. 操作日志
def check_oplog(client) -> None:
    from app.config import settings

    section("4. 操作日志（仅 MySQL）")
    if settings.store_label != "mysql":
        info("JSON 模式操作日志只写 logger、不落审计表，跳过本组")
        return

    agent_id = make_agent("log", "广场助手·日志")
    payload = {
        "appName": f"{APP_PREFIX}log",
        "sourceType": "agent",
        "agentId": agent_id,
        "category": "其他",
    }
    app_id = client.post("/api/admin/apps", json=payload).json()["data"]["appId"]
    client.patch(f"/api/admin/apps/{app_id}", json={"status": 0})
    client.delete(f"/api/admin/apps/{app_id}")

    logs = client.get(
        "/api/admin/operation-logs",
        params={"pageSize": 100},
    ).json()["data"]["items"]
    hits = {x["action"] for x in logs if x.get("targetType") == "app"
            and x.get("targetId") == str(app_id)}
    for action, label in (
        ("APP_CREATE", "新增"),
        ("APP_UPDATE", "更新"),
        ("APP_DELETE", "删除"),
    ):
        check(f"{label}应用留痕（{action}）", action in hits, str(sorted(hits)))
    delete_agent(agent_id)


# ==================================================================== 辅助
def make_agent(suffix: str, name: str) -> int:
    from app.db import agent_store

    code = f"{AGENT_PREFIX}{suffix}"
    existing = agent_store.get_by_code(code)
    if existing is None:
        existing = agent_store.create_agent({"agentCode": code, "agentName": name})
    if int(existing.get("status", 1)) != 1:
        agent_store.update_agent(existing["agentId"], {"status": 1})
    return existing["agentId"]


def delete_agent(agent_id: int) -> None:
    from app.db import agent_store

    if agent_store.get_agent(agent_id) is not None:
        agent_store.delete_agent(agent_id)


def main() -> int:
    parser = argparse.ArgumentParser(description="应用广场验收（任务 3.3）")
    parser.add_argument("--store", default="json", choices=["json", "mysql"],
                        help="存储模式；默认 json（无需 MySQL）")
    parser.add_argument("--keep-data", action="store_true",
                        help="保留临时数据文件（默认测完删除）")
    args = parser.parse_args()

    os.environ["KB_STORE"] = args.store

    tmpdir = Path(tempfile.mkdtemp(prefix="verify-apps-"))
    if args.store == "json":
        from app.db import agent_store, app_store

        app_store.APPS_PATH = tmpdir / "apps.json"
        agent_store.AGENTS_PATH = tmpdir / "agents.json"
        print(f"临时应用文件：{app_store.APPS_PATH}")

    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import app

    print("=" * 68)
    print("应用广场验收（任务 3.3 · M-C-05/06）")
    print(f"存储模式：{settings.store_label}    鉴权：AUTH_ENABLED={settings.auth_enabled}")
    print("=" * 68)

    client = TestClient(app)
    ok = False
    try:
        if args.store == "mysql":
            apps, agents = purge_mysql()
            if apps or agents:
                print(f"（已清理上次残留：{apps} 个应用 / {agents} 个助手）")
        check_store()
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
