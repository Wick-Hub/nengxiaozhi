"""渠道分身验收（任务 3.1 / 3.2 / M-C-01·02·04）。

默认 **JSON 模式 + 临时数据文件**，无需 MySQL 即可复现；``--store mysql``
切到真实库（测试渠道编码统一 ``vf-ch-*``，结束时清理）。

覆盖：

1. 存储层：新建/按编码读取/更新/删除、助手集合去重且主助手恒在首位；
2. 服务层校验：编码格式、名称、默认助手存在性、主题色/位置/文案、重复编码 409；
3. 集成代码：渠道 URL 固定 ``/?channel=<code>``、嵌入链接追加 ``embed=1``，
   iframe/浮窗代码含主题色与位置，名称做 HTML 转义（防注入）；
4. 公开接口：匿名可读、只返回已上架助手、默认助手下架时自动回退、
   渠道停用即 404；
5. 接口契约：``/api/admin/channels`` CRUD 全链路 + 400/404/409/422 状态码；
6. 门户个性化与复制（3.2）：形象/FAQ/快捷方式/推荐应用/客服反馈配置、
   脚本协议拦截、认证开关、一键复制（副本默认停用、可独立修改）；
7. 操作日志（仅 MySQL）：新增/更新/复制/删除各留一条 ``CHANNEL_*`` 记录。

用法::

    python tools/verify_channels.py
    python tools/verify_channels.py --store mysql
    python tools/verify_channels.py --keep-data

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
os.environ.setdefault("LLM_MOCK", "1")
os.environ.setdefault("AUTH_ENABLED", "false")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")

try:  # pragma: no cover - 仅影响交互输出
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

PASSED = 0
FAILED = 0

#: 本工具创建的渠道/助手编码前缀（便于 MySQL 模式清理，保证可重复运行）
CHANNEL_PREFIX = "vf-ch-"
AGENT_PREFIX = "vf-cha-"


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
    """清理上次残留的验收渠道与助手（MySQL 模式下唯一约束会挡住重复运行）。"""
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


# ==================================================================== 1. 存储层
def check_store() -> None:
    from app.db import channel_store

    section("1. 存储层")
    a_id, b_id = make_agents()
    cid = channel_store.create_channel(
        channel_code="vf-ch-store",
        channel_name="存储层渠道",
        default_agent_id=b_id,
        welcome_text="欢迎",
        theme_config={"primaryColor": "#123456"},
        agent_ids=[a_id, b_id, a_id],
    )
    check("新建返回自增 channelId", cid > 0, f"channelId={cid}")

    item = channel_store.get_channel(cid)
    check("按 ID 回读成功", item is not None and item["channelCode"] == "vf-ch-store")
    check("主助手排在首位", item["agentIds"] == [b_id, a_id], str(item["agentIds"]))
    check("重复 agentId 已去重", len(item["agentIds"]) == 2, str(item["agentIds"]))
    check("按编码可查", (channel_store.get_by_code("vf-ch-store") or {}).get("channelId") == cid)
    check("查不存在的编码返回 None", channel_store.get_by_code("vf-ch-missing") is None)

    channel_store.update_channel(
        cid, channel_name="存储层渠道·改", status=0, agent_ids=[a_id]
    )
    item = channel_store.get_channel(cid)
    check("更新后回读生效", item["channelName"] == "存储层渠道·改" and item["status"] == 0)
    check("主助手改动后仍排首位", item["agentIds"] == [b_id, a_id], str(item["agentIds"]))
    check("列表按新建在前排序", channel_store.list_channels()[0]["channelId"] == cid)

    check("删除已存在渠道返回 True", channel_store.delete_channel(cid) is True)
    check("删除后查不到", channel_store.get_channel(cid) is None)
    check("删除不存在渠道返回 False", channel_store.delete_channel(987654) is False)
    delete_agents(a_id, b_id)


# ==================================================================== 2. 校验
def check_validation() -> None:
    from app.services import channel_admin

    section("2. 服务层校验（错误一次报清）")
    a_id, b_id = make_agents()
    good = {
        "channelCode": "vf-ch-valid",
        "channelName": "校验渠道",
        "defaultAgentId": a_id,
        "agentIds": [a_id, b_id],
    }

    def fail(payload: dict, keyword: str, status: int = 400) -> None:
        r = channel_admin.create_channel(payload)
        check(
            f"非法输入被拒：{keyword}",
            r["ok"] is False and r.get("status") == status and keyword in (r.get("error") or ""),
            f"{r.get('status')} {r.get('error')}",
        )

    fail({**good, "channelCode": "X!"}, "渠道编码")
    fail({**good, "channelCode": "a"}, "渠道编码")
    fail({**good, "channelName": "   "}, "渠道名称")
    fail({**good, "defaultAgentId": None}, "默认助手")
    fail({**good, "defaultAgentId": 999999}, "默认助手不存在")
    fail({**good, "agentIds": [a_id, 999999]}, "助手不存在")
    fail({**good, "themeConfig": {"primaryColor": "red"}}, "主题色")
    fail({**good, "themeConfig": {"position": "top"}}, "浮窗位置")
    fail({**good, "themeConfig": {"launcherText": "文" * 17}}, "16 个字符")
    fail({**good, "themeConfig": {"blob": "x" * 20000}}, "16384 字节")
    fail({**good, "themeConfig": {"avatarUrl": "javascript:alert(1)"}}, "头像图片")
    fail({**good, "themeConfig": {"faq": [f"问题{i}" for i in range(9)]}}, "最多")
    fail(
        {
            **good,
            "themeConfig": {
                "shortcuts": [
                    {"label": "x", "action": "link", "content": "javascript:alert(1)"}
                ]
            },
        },
        "快捷方式",
    )
    fail(
        {**good, "themeConfig": {"recommendedApps": [{"name": "x", "url": ""}]}},
        "缺少链接",
    )
    fail(
        {**good, "themeConfig": {"serviceEntry": {"enabled": 1, "url": ""}}},
        "客服入口",
    )

    made = channel_admin.create_channel(good)
    check("合法输入创建成功", made["ok"] is True, made.get("error") or "")
    dup = channel_admin.create_channel({**good, "channelName": "另一个"})
    check("重复编码 → 409", dup["ok"] is False and dup["status"] == 409,
          str(dup.get("error")))

    if made["ok"]:
        cid = made["data"]["channelId"]
        bad_update = channel_admin.update_channel(cid, {"channelCode": "bad code"})
        check("更新时同样校验编码", bad_update["ok"] is False
              and bad_update["status"] == 400, str(bad_update.get("error")))
        channel_admin.delete_channel(cid)
    delete_agents(a_id, b_id)


# ==================================================================== 3. 集成代码
def check_assets() -> None:
    from app.services import channel_admin

    section("3. 渠道 URL 与嵌入代码")
    record = {
        "channelCode": "vf-ch-assets",
        "channelName": '招生"咨询<助手>',
        "themeConfig": {"primaryColor": "#ff6600", "position": "left", "launcherText": "咨询"},
    }
    assets = channel_admin.build_assets(record, "https://kb.example.edu/")
    check("渠道 URL 带 channel 参数",
          assets["channelUrl"] == "https://kb.example.edu/?channel=vf-ch-assets",
          assets["channelUrl"])
    check("嵌入链接追加 embed=1",
          assets["embedUrl"] == "https://kb.example.edu/?channel=vf-ch-assets&embed=1",
          assets["embedUrl"])
    import html as html_lib

    check("iframe 代码含嵌入地址（属性已转义）",
          html_lib.escape(assets["embedUrl"], quote=True) in assets["iframeSnippet"],
          assets["iframeSnippet"][:100])
    check("iframe 标题已 HTML 转义",
          'title="招生&quot;咨询&lt;助手&gt;"' in assets["iframeSnippet"],
          assets["iframeSnippet"][:120])
    check("浮窗代码含主题色", "#ff6600" in assets["floatSnippet"])
    check("浮窗位置为 left", "left: 24px" in assets["floatSnippet"])
    check("浮窗按钮文案生效", '"咨询"' in assets["floatSnippet"])
    check("embedSnippet 与 iframe 版本一致",
          assets["embedSnippet"] == assets["iframeSnippet"])
    check("SDK 脚本地址正确", assets["sdkUrl"] == "https://kb.example.edu/sdk/nxz.js",
          assets["sdkUrl"])
    check("SDK 代码携带渠道/位置/主题/文案",
          all(token in assets["sdkSnippet"] for token in (
              'data-channel="vf-ch-assets"',
              'data-position="left"',
              'data-color="#ff6600"',
              'data-label="咨询"',
          )), assets["sdkSnippet"][:180])

    relative = channel_admin.build_assets(record, "")
    check("未配置基址时保留相对地址", relative["channelUrl"] == "/?channel=vf-ch-assets",
          relative["channelUrl"])
    check("未配置基址时 SDK 为相对路径", relative["sdkUrl"] == "/sdk/nxz.js",
          relative["sdkUrl"])

    from app.config import settings

    old = settings.public_base_url
    settings.public_base_url = "https://campus.example.edu"
    try:
        via_settings = channel_admin.build_assets(record, "")
        check("PUBLIC_BASE_URL 生效",
              via_settings["channelUrl"] == "https://campus.example.edu/?channel=vf-ch-assets",
              via_settings["channelUrl"])
    finally:
        settings.public_base_url = old


# ==================================================================== 4. 接口层
def check_api(client) -> None:
    from app.db import channel_store

    section("4. 接口契约（TestClient）")
    a_id, b_id = make_agents()
    base = {
        "channelCode": "vf-ch-api",
        "channelName": "接口渠道",
        "defaultAgentId": a_id,
        "welcomeText": "接口欢迎语",
        "themeConfig": {"primaryColor": "#00aa88", "position": "right"},
        "needAuth": 0,
        "status": 1,
        "agentIds": [a_id, b_id],
    }

    r = client.get("/api/admin/channels")
    check("GET /admin/channels 200", r.status_code == 200 and r.json()["ok"] is True,
          str(r.status_code))
    if r.status_code == 200:
        data = r.json()["data"]
        check("列表返回 items/total/enabled/agentOptions",
              {"items", "total", "enabled", "agentOptions"} <= set(data))
        check("助手候选含全部助手", any(x["agentId"] == a_id for x in data["agentOptions"]))

    r = client.post("/api/admin/channels", json=base)
    check("POST /admin/channels 200", r.status_code == 200, r.text[:160])
    cid = r.json()["data"]["channelId"] if r.status_code == 200 else None
    if cid is not None:
        item = r.json()["data"]
        check("创建返回渠道 URL/嵌入代码",
              item["channelUrl"].endswith("/?channel=vf-ch-api")
              and "embed=1" in item["embedUrl"] and bool(item["iframeSnippet"])
              and bool(item["sdkSnippet"]))
        check("创建返回助手卡片与默认助手名",
              [x["agentId"] for x in item["agents"]] == [a_id, b_id]
              and item["defaultAgentName"] == "渠道助手甲")

        r = client.get(f"/api/admin/channels/{cid}")
        check("GET /admin/channels/{id} 200", r.status_code == 200
              and r.json()["data"]["channelName"] == "接口渠道")

        r = client.patch(f"/api/admin/channels/{cid}",
                         json={"welcomeText": "改过的欢迎语",
                               "themeConfig": {"primaryColor": "#112233"}})
        check("PATCH 更新 200 + 字段生效", r.status_code == 200
              and r.json()["data"]["welcomeText"] == "改过的欢迎语"
              and r.json()["data"]["themeConfig"]["primaryColor"] == "#112233",
              str(r.status_code))

        r = client.get("/api/channels/vf-ch-api")
        check("公开接口匿名可读 200", r.status_code == 200, str(r.status_code))
        if r.status_code == 200:
            pub = r.json()["data"]
            check("公开数据仅含门户需要的字段",
                  "channelUrl" not in pub and "embedSnippet" not in pub,
                  str(sorted(pub)))
            check("公开数据返回两个上架助手", len(pub["agents"]) == 2)
            check("公开数据默认助手正确", pub["defaultAgentId"] == a_id)
            check("公开数据主题/认证开关透传",
                  pub["themeConfig"]["primaryColor"] == "#112233" and pub["needAuth"] == 0)

        # 下架第二助手：公开端过滤
        r = client.post(f"/api/admin/agents/{b_id}/status", json={"status": 0})
        check("第二助手下架成功", r.status_code == 200 and r.json()["ok"] is True)
        pub = client.get("/api/channels/vf-ch-api").json()["data"]
        check("公开端不再返回已下架助手",
              [x["agentId"] for x in pub["agents"]] == [a_id], str(pub["agents"]))

        # 默认助手也下架：回退到第一个可用助手
        r = client.post(f"/api/admin/agents/{a_id}/status", json={"status": 0})
        check("默认助手下架成功", r.status_code == 200 and r.json()["ok"] is True)
        r = client.get("/api/channels/vf-ch-api")
        check("无可用助手时不报错（empty agents）", r.status_code == 200
              and r.json()["data"]["agents"] == [] and r.json()["data"]["defaultAgentId"] is None,
              r.text[:120])
        client.post(f"/api/admin/agents/{a_id}/status", json={"status": 1})
        client.post(f"/api/admin/agents/{b_id}/status", json={"status": 1})

        # 停用渠道：公开端 404
        r = client.patch(f"/api/admin/channels/{cid}", json={"status": 0})
        check("停用渠道 200", r.status_code == 200 and r.json()["data"]["status"] == 0)
        r = client.get("/api/channels/vf-ch-api")
        check("停用后公开接口 404", r.status_code == 404, str(r.status_code))
        client.patch(f"/api/admin/channels/{cid}", json={"status": 1})
        check("重新启用后公开接口恢复", client.get("/api/channels/vf-ch-api").status_code == 200)

        dup = client.post("/api/admin/channels", json={**base, "channelName": "重复"})
        check("重复编码 → 409", dup.status_code == 409, str(dup.status_code))
        bad = client.post("/api/admin/channels", json={**base, "channelCode": "BAD CODE"})
        check("非法参数 → 400", bad.status_code == 400, str(bad.status_code))
        missing = client.get("/api/admin/channels/999999")
        check("不存在的渠道 → 404", missing.status_code == 404, str(missing.status_code))
        check("不存在的公开编码 → 404",
              client.get("/api/channels/vf-ch-none").status_code == 404)
        check("status=9 → 422（枚举拦截）",
              client.patch(f"/api/admin/channels/{cid}", json={"status": 9}).status_code == 422)

        r = client.delete(f"/api/admin/channels/{cid}")
        check("DELETE 渠道 200", r.status_code == 200 and r.json()["data"]["deleted"] is True)
        check("删除后公开接口 404", client.get("/api/channels/vf-ch-api").status_code == 404)
        check("列表中不再出现", all(x["channelId"] != cid for x in
                                channel_store.list_channels()))

    delete_agents(a_id, b_id)


# ==================================================================== 5. 门户个性化与复制
def check_portal(client) -> None:
    section("5. 门户个性化与一键复制（任务 3.2 / M-C-02·04）")
    a_id, b_id = make_agents()
    theme = {
        "primaryColor": "#224466",
        "position": "left",
        "launcherText": "点我咨询",
        "assistantName": "门户小智",
        "avatarText": "智",
        "avatarUrl": "https://cdn.example.edu.cn/avatar.png",
        "faq": ["校园网怎么开通？", "一卡通丢了怎么办？"],
        "shortcuts": [
            {"icon": "🌐", "label": "校园网", "action": "ask", "content": "校园网怎么开通？"},
            {
                "icon": "🏛",
                "label": "办事大厅",
                "action": "link",
                "content": "https://ehall.example.edu.cn",
            },
        ],
        "recommendedApps": [
            {
                "icon": "📚",
                "name": "图书馆",
                "desc": "借阅与续借",
                "url": "https://lib.example.edu.cn",
            }
        ],
        "serviceEntry": {
            "enabled": 1,
            "label": "联系客服",
            "url": "https://support.example.edu.cn",
        },
        "feedback": {"enabled": 1, "label": "意见反馈", "url": "/feedback"},
    }
    base = {
        "channelCode": "vf-ch-portal",
        "channelName": "门户渠道",
        "defaultAgentId": a_id,
        "welcomeText": "门户欢迎语",
        "themeConfig": theme,
        "needAuth": 0,
        "status": 1,
        "agentIds": [a_id, b_id],
    }
    r = client.post("/api/admin/channels", json=base)
    check("门户配置创建成功", r.status_code == 200, r.text[:160])
    if r.status_code != 200:
        delete_agents(a_id, b_id)
        return
    item = r.json()["data"]
    cid = item["channelId"]
    check("门户字段完整回读", item["themeConfig"] == theme, str(item["themeConfig"])[:160])

    pub = client.get("/api/channels/vf-ch-portal").json()["data"]
    check("公开接口下发门户配置", pub["themeConfig"] == theme)

    bad = client.post(
        "/api/admin/channels",
        json={
            **base,
            "channelCode": "vf-ch-portal-bad",
            "themeConfig": {
                "recommendedApps": [{"name": "x", "url": "javascript:alert(1)"}]
            },
        },
    )
    check("推荐应用脚本协议被拒", bad.status_code == 400, str(bad.status_code))

    r = client.patch(f"/api/admin/channels/{cid}", json={"needAuth": 1})
    check("认证开关一键开启", r.status_code == 200
          and r.json()["data"]["needAuth"] == 1, str(r.status_code))
    r = client.patch(f"/api/admin/channels/{cid}", json={"needAuth": 0})
    check("认证开关一键关闭", r.status_code == 200
          and r.json()["data"]["needAuth"] == 0, str(r.status_code))

    r = client.post(f"/api/admin/channels/{cid}/copy", json={})
    check("一键复制返回 200", r.status_code == 200, r.text[:160])
    copy = r.json()["data"] if r.status_code == 200 else None
    if copy:
        check("副本编码/名称自动生成",
              copy["channelCode"] == "vf-ch-portal-copy"
              and copy["channelName"] == "门户渠道（副本）",
              f"{copy['channelCode']} / {copy['channelName']}")
        check("副本默认停用并记录来源",
              copy["status"] == 0 and copy["copyOf"] == cid,
              f"status={copy['status']} copyOf={copy['copyOf']}")
        check("副本继承全部配置",
              copy["themeConfig"] == theme
              and copy["needAuth"] == 0
              and copy["agentIds"] == item["agentIds"])
        check("副本停用时公开接口 404",
              client.get("/api/channels/vf-ch-portal-copy").status_code == 404)

        r = client.patch(
            f"/api/admin/channels/{copy['channelId']}",
            json={
                "status": 1,
                "channelName": "副本独立名",
                "themeConfig": {**theme, "primaryColor": "#445566"},
            },
        )
        check("副本可独立启用/改名/换色", r.status_code == 200
              and r.json()["data"]["status"] == 1
              and r.json()["data"]["channelName"] == "副本独立名"
              and r.json()["data"]["themeConfig"]["primaryColor"] == "#445566",
              str(r.status_code))
        src = client.get(f"/api/admin/channels/{cid}").json()["data"]
        check("源渠道配置不受副本影响",
              src["channelName"] == "门户渠道"
              and src["themeConfig"]["primaryColor"] == "#224466")
        client.delete(f"/api/admin/channels/{copy['channelId']}")

    client.delete(f"/api/admin/channels/{cid}")
    delete_agents(a_id, b_id)


# ==================================================================== 6. 操作日志
def check_oplog(client) -> None:
    from app.config import settings

    section("6. 操作日志（仅 MySQL）")
    if settings.store_label != "mysql":
        info("JSON 模式操作日志只写 logger、不落审计表，跳过本组")
        return

    a_id, _ = make_agents()
    payload = {"channelCode": "vf-ch-log", "channelName": "日志渠道", "defaultAgentId": a_id}
    cid = client.post("/api/admin/channels", json=payload).json()["data"]["channelId"]
    client.patch(f"/api/admin/channels/{cid}", json={"welcomeText": "改"})
    copied = client.post(f"/api/admin/channels/{cid}/copy", json={}).json()["data"]
    client.delete(f"/api/admin/channels/{copied['channelId']}")
    client.delete(f"/api/admin/channels/{cid}")

    logs = client.get(
        "/api/admin/operation-logs",
        params={"pageSize": 100},
    ).json()["data"]["items"]
    hits = {x["action"] for x in logs if x.get("targetType") == "channel"
            and x.get("targetId") == str(cid)}
    for action, label in (
        ("CHANNEL_CREATE", "新增"),
        ("CHANNEL_UPDATE", "更新"),
        ("CHANNEL_DELETE", "删除"),
    ):
        check(f"{label}渠道留痕（{action}）", action in hits, str(sorted(hits)))
    copy_logs = [
        x for x in logs
        if x.get("action") == "CHANNEL_COPY"
        and x.get("targetId") == str(copied["channelId"])
    ]
    check("复制渠道留痕（CHANNEL_COPY）", bool(copy_logs), str(sorted(hits)))
    delete_agents(a_id)


# ==================================================================== 辅助
def make_agents() -> tuple[int, int]:
    """创建两个验收助手（幂等：已存在则复用）。"""
    from app.db import agent_store

    ids = []
    for code, name in ((f"{AGENT_PREFIX}a", "渠道助手甲"), (f"{AGENT_PREFIX}b", "渠道助手乙")):
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


# ==================================================================== main
def main() -> int:
    parser = argparse.ArgumentParser(description="渠道分身验收（任务 3.1）")
    parser.add_argument("--store", default="json", choices=["json", "mysql"],
                        help="存储模式；默认 json（无需 MySQL）")
    parser.add_argument("--keep-data", action="store_true",
                        help="保留临时 channels.json（默认测完删除）")
    args = parser.parse_args()

    os.environ["KB_STORE"] = args.store

    tmpdir = Path(tempfile.mkdtemp(prefix="verify-channels-"))
    if args.store == "json":
        from app.db import agent_store, channel_store

        channel_store.CHANNELS_PATH = tmpdir / "channels.json"
        agent_store.AGENTS_PATH = tmpdir / "agents.json"
        print(f"临时渠道文件：{channel_store.CHANNELS_PATH}")

    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import app

    print("=" * 68)
    print("渠道分身验收（任务 3.1 / 3.2 · M-C-01·02·04）")
    print(f"存储模式：{settings.store_label}    鉴权：AUTH_ENABLED={settings.auth_enabled}")
    print("=" * 68)

    client = TestClient(app)
    ok = False
    try:
        if args.store == "mysql":
            channels, agents = purge_mysql()
            if channels or agents:
                print(f"（已清理上次残留：{channels} 个渠道 / {agents} 个助手）")
        check_store()
        check_validation()
        check_assets()
        check_api(client)
        check_portal(client)
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
