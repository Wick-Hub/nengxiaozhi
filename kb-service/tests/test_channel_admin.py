"""渠道接入管理测试（任务 3.1 / M-C-01）。

覆盖：

1. **服务/接口链路**：创建、清单、详情、更新、启停、删除；
2. **助手绑定**：默认助手恒在集合首位，可追加渠道内可见助手；
3. **URL / 嵌入代码**：`?channel=` 链接、iframe 与浮窗代码、主题色；
4. **公开端**：启用的渠道可匿名读取；停用/不存在统一 404；
5. **参数校验**：编码格式、编码唯一、默认助手/绑定助手存在、主题色格式；
6. **MySQL 分支（桩）**：渠道行 + channel_agent 关联的合并与默认助手补位。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db import agent_store, channel_store
from app.main import app
from app.services import channel_admin

client = TestClient(app)


@pytest.fixture()
def base_agent() -> dict:
    return agent_store.list_agents()[0]


@pytest.fixture()
def second_agent() -> dict:
    return agent_store.create_agent(
        {
            "agentCode": "ch-second",
            "agentName": "渠道第二助手",
            "agentType": "chat",
        }
    )


def _payload(base: dict, **overrides) -> dict:
    payload = {
        "channelCode": "vf-channel",
        "channelName": "验收渠道",
        "defaultAgentId": base["agentId"],
        "welcomeText": "欢迎咨询",
        "needAuth": 0,
        "status": 1,
        "themeConfig": {"primaryColor": "#112233", "position": "right", "launcherText": "问我"},
    }
    payload.update(overrides)
    return payload


# ------------------------------------------------------------------ 创建 / 清单 / 公开端
def test_create_channel_and_render_assets(base_agent: dict, second_agent: dict) -> None:
    resp = client.post(
        "/api/admin/channels",
        json=_payload(base_agent, agentIds=[base_agent["agentId"], second_agent["agentId"]]),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    item = body["data"]

    assert item["channelCode"] == "vf-channel"
    assert item["agentIds"] == [base_agent["agentId"], second_agent["agentId"]]
    assert item["defaultAgentName"] == base_agent["agentName"]
    assert [a["agentId"] for a in item["agents"]] == [
        base_agent["agentId"],
        second_agent["agentId"],
    ]
    assert item["channelUrl"].endswith("/?channel=vf-channel")
    assert "embed=1" in item["embedUrl"]
    assert item["iframeSnippet"].startswith("<iframe")
    assert "embed=1" in item["iframeSnippet"]
    assert "document.createElement('iframe')" in item["floatSnippet"]
    assert "vf-channel" in item["floatSnippet"]
    assert "#112233" in item["floatSnippet"]
    assert item["sdkUrl"].endswith("/sdk/nxz.js")
    assert item["sdkSnippet"].startswith("<script ")
    assert 'data-channel="vf-channel"' in item["sdkSnippet"]
    assert 'data-color="#112233"' in item["sdkSnippet"]
    assert 'data-label="问我"' in item["sdkSnippet"]
    assert item["themeConfig"]["primaryColor"] == "#112233"
    assert item["statusName"] == "启用"


def test_list_channels_returns_agent_options(base_agent: dict) -> None:
    client.post("/api/admin/channels", json=_payload(base_agent))
    data = client.get("/api/admin/channels").json()["data"]
    assert data["total"] == 1
    assert data["enabled"] == 1
    assert data["editable"] is True
    hit = next(x for x in data["agentOptions"] if x["agentId"] == base_agent["agentId"])
    assert hit["agentName"] == base_agent["agentName"]


def test_public_channel_is_anonymous_and_only_returns_enabled_agents(
    base_agent: dict, second_agent: dict
) -> None:
    created = client.post(
        "/api/admin/channels",
        json=_payload(base_agent, agentIds=[base_agent["agentId"], second_agent["agentId"]]),
    ).json()["data"]

    body = client.get("/api/channels/vf-channel").json()
    assert body["ok"] is True
    data = body["data"]
    assert data["channelCode"] == "vf-channel"
    assert data["defaultAgentId"] == base_agent["agentId"]
    assert len(data["agents"]) == 2
    assert not any("channelUrl" in a for a in data["agents"])

    # 下架第二助手后，公开端不再展示它；默认助手仍可用
    resp = client.post(
        f"/api/admin/agents/{second_agent['agentId']}/status", json={"status": 0}
    )
    assert resp.json()["ok"] is True
    data = client.get("/api/channels/vf-channel").json()["data"]
    assert [a["agentId"] for a in data["agents"]] == [base_agent["agentId"]]

    client.delete(f"/api/admin/channels/{created['channelId']}")
    assert client.get("/api/channels/vf-channel").status_code == 404


def test_public_channel_falls_back_to_first_enabled_agent(base_agent: dict, second_agent: dict) -> None:
    item = client.post(
        "/api/admin/channels",
        json=_payload(
            base_agent,
            defaultAgentId=base_agent["agentId"],
            agentIds=[base_agent["agentId"], second_agent["agentId"]],
        ),
    ).json()["data"]
    resp = client.post(
        f"/api/admin/agents/{base_agent['agentId']}/status", json={"status": 0}
    )
    assert resp.json()["ok"] is True
    data = client.get("/api/channels/vf-channel").json()["data"]
    assert data["defaultAgentId"] == second_agent["agentId"]
    client.delete(f"/api/admin/channels/{item['channelId']}")


# ------------------------------------------------------------------ 更新 / 启停 / 删除
def test_update_channel_binds_agents_and_switches_default(base_agent: dict, second_agent: dict) -> None:
    item = client.post("/api/admin/channels", json=_payload(base_agent)).json()["data"]
    updated = client.patch(
        f"/api/admin/channels/{item['channelId']}",
        json={
            "channelName": "官网入口",
            "defaultAgentId": second_agent["agentId"],
            "agentIds": [second_agent["agentId"], base_agent["agentId"]],
            "welcomeText": "新的欢迎语",
        },
    )
    assert updated.status_code == 200, updated.text
    data = updated.json()["data"]
    assert data["channelName"] == "官网入口"
    assert data["welcomeText"] == "新的欢迎语"
    assert data["defaultAgentId"] == second_agent["agentId"]
    assert data["agentIds"][0] == second_agent["agentId"]
    assert data["defaultAgentName"] == second_agent["agentName"]


def test_disable_channel_hides_public_config(base_agent: dict) -> None:
    item = client.post("/api/admin/channels", json=_payload(base_agent)).json()["data"]
    resp = client.patch(
        f"/api/admin/channels/{item['channelId']}", json={"status": 0}
    )
    assert resp.json()["data"]["status"] == 0
    assert client.get("/api/channels/vf-channel").status_code == 404
    client.patch(f"/api/admin/channels/{item['channelId']}", json={"status": 1})
    assert client.get("/api/channels/vf-channel").status_code == 200


def test_delete_channel(base_agent: dict) -> None:
    item = client.post("/api/admin/channels", json=_payload(base_agent)).json()["data"]
    assert client.delete(f"/api/admin/channels/{item['channelId']}").status_code == 200
    assert client.get(f"/api/admin/channels/{item['channelId']}").status_code == 404
    assert client.get("/api/channels/vf-channel").status_code == 404


# ------------------------------------------------------------------ 校验
def test_duplicate_channel_code_conflicts(base_agent: dict) -> None:
    assert client.post("/api/admin/channels", json=_payload(base_agent)).status_code == 200
    dup = client.post("/api/admin/channels", json=_payload(base_agent))
    assert dup.status_code == 409
    assert "已存在" in dup.json()["detail"]


def test_channel_code_validation(base_agent: dict) -> None:
    for code in ("A", "大写", "with space", "x"):
        resp = client.post("/api/admin/channels", json=_payload(base_agent, channelCode=code))
        assert resp.status_code == 400, code


def test_missing_agent_rejected(base_agent: dict) -> None:
    resp = client.post(
        "/api/admin/channels",
        json=_payload(base_agent, defaultAgentId=999999),
    )
    assert resp.status_code == 400
    assert "默认助手不存在" in resp.json()["detail"]

    resp = client.post(
        "/api/admin/channels",
        json=_payload(base_agent, agentIds=[base_agent["agentId"], 999999]),
    )
    assert resp.status_code == 400
    assert "不存在" in resp.json()["detail"]


def test_theme_validation(base_agent: dict) -> None:
    resp = client.post(
        "/api/admin/channels",
        json=_payload(base_agent, themeConfig={"primaryColor": "red"}),
    )
    assert resp.status_code == 400
    assert "主题色" in resp.json()["detail"]

    resp = client.post(
        "/api/admin/channels",
        json=_payload(base_agent, themeConfig={"position": "top"}),
    )
    assert resp.status_code == 400
    assert "浮窗位置" in resp.json()["detail"]


def test_update_unknown_channel_404() -> None:
    assert client.patch("/api/admin/channels/999999", json={"status": 0}).status_code == 404


# ------------------------------------------------------------------ 门户个性化（任务 3.2 / M-C-02）
def _portal_theme() -> dict:
    return {
        "primaryColor": "#123456",
        "position": "left",
        "launcherText": "点我咨询",
        "assistantName": "小智助手",
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
        "customBadge": "vip",
    }


def test_portal_theme_round_trip(base_agent: dict) -> None:
    theme = _portal_theme()
    resp = client.post(
        "/api/admin/channels",
        json=_payload(base_agent, themeConfig=theme, needAuth=1),
    )
    assert resp.status_code == 200, resp.text
    item = resp.json()["data"]
    assert item["themeConfig"] == theme
    # 未登记的扩展键也原样保留，便于后续任务继续扩展 theme_config
    assert item["themeConfig"]["customBadge"] == "vip"

    public = client.get("/api/channels/vf-channel").json()["data"]
    assert public["themeConfig"] == theme
    assert public["needAuth"] == 1


def test_portal_theme_rejects_invalid_config(base_agent: dict) -> None:
    cases = [
        ({"assistantName": "名" * 33}, "形象名称"),
        ({"avatarText": "头" * 7}, "头像文字"),
        ({"avatarUrl": "javascript:alert(1)"}, "头像图片"),
        (
            {"shortcuts": [{"label": "x", "action": "run", "content": "y"}]},
            "动作",
        ),
        (
            {
                "shortcuts": [
                    {"label": "x", "action": "link", "content": "javascript:alert(1)"}
                ]
            },
            "快捷方式",
        ),
        (
            {"shortcuts": [{"label": "x", "action": "ask", "content": "问" * 201}]},
            "不能超过",
        ),
        ({"shortcuts": [{"label": "x", "action": "ask", "content": "q"}] * 9}, "最多"),
        ({"recommendedApps": [{"name": "x", "url": ""}]}, "缺少链接"),
        ({"recommendedApps": [{"name": "x", "url": "ftp://example.com/a"}]}, "链接"),
        ({"recommendedApps": [{"name": "x", "url": "https://a.example"}] * 9}, "最多"),
        ({"serviceEntry": {"enabled": 1, "label": "客服", "url": ""}}, "客服入口"),
        ({"feedback": {"enabled": 1, "url": "data:text/html,hi"}}, "反馈入口"),
        ({"faq": [f"问题{i}" for i in range(9)]}, "最多"),
        ({"faq": ["问" * 121]}, "不能超过"),
    ]
    for idx, (theme, keyword) in enumerate(cases):
        resp = client.post(
            "/api/admin/channels",
            json=_payload(base_agent, channelCode=f"bad-theme-{idx}", themeConfig=theme),
        )
        assert resp.status_code == 400, (theme, resp.text)
        assert keyword in resp.json()["detail"], (theme, resp.json()["detail"])


def test_auth_switch_can_be_toggled(base_agent: dict) -> None:
    item = client.post(
        "/api/admin/channels", json=_payload(base_agent, needAuth=0)
    ).json()["data"]
    assert client.get("/api/channels/vf-channel").json()["data"]["needAuth"] == 0

    resp = client.patch(
        f"/api/admin/channels/{item['channelId']}", json={"needAuth": 1}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["needAuth"] == 1
    assert client.get("/api/channels/vf-channel").json()["data"]["needAuth"] == 1

    client.patch(f"/api/admin/channels/{item['channelId']}", json={"needAuth": 0})
    assert client.get("/api/channels/vf-channel").json()["data"]["needAuth"] == 0


# ------------------------------------------------------------------ 一键复制（任务 3.2 / M-C-04）
def test_copy_channel_copies_config_and_stays_disabled(
    base_agent: dict, second_agent: dict
) -> None:
    theme = _portal_theme()
    source = client.post(
        "/api/admin/channels",
        json=_payload(
            base_agent,
            needAuth=1,
            status=1,
            welcomeText="源渠道欢迎语",
            themeConfig=theme,
            agentIds=[base_agent["agentId"], second_agent["agentId"]],
        ),
    ).json()["data"]

    resp = client.post(f"/api/admin/channels/{source['channelId']}/copy", json={})
    assert resp.status_code == 200, resp.text
    copy = resp.json()["data"]
    assert copy["channelCode"] == "vf-channel-copy"
    assert copy["channelName"] == "验收渠道（副本）"
    assert copy["status"] == 0
    assert copy["copyOf"] == source["channelId"]
    assert copy["needAuth"] == 1
    assert copy["welcomeText"] == "源渠道欢迎语"
    assert copy["themeConfig"] == theme
    assert copy["defaultAgentId"] == source["defaultAgentId"]
    assert copy["agentIds"] == source["agentIds"]

    # 副本默认停用：公开端不可见
    assert client.get("/api/channels/vf-channel-copy").status_code == 404

    # 副本可独立修改，源渠道不受影响
    patched = client.patch(
        f"/api/admin/channels/{copy['channelId']}",
        json={"channelName": "副本改名", "status": 1, "themeConfig": {
            **theme, "primaryColor": "#abcdef",
        }},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["data"]["channelName"] == "副本改名"
    assert client.get("/api/channels/vf-channel-copy").status_code == 200
    assert client.get("/api/channels/vf-channel-copy").json()["data"][
        "themeConfig"
    ]["primaryColor"] == "#abcdef"
    source_after = client.get(
        f"/api/admin/channels/{source['channelId']}"
    ).json()["data"]
    assert source_after["channelName"] == "验收渠道"
    assert source_after["themeConfig"]["primaryColor"] == "#123456"

    # 再次复制自动追加序号
    second = client.post(
        f"/api/admin/channels/{source['channelId']}/copy", json={}
    ).json()["data"]
    assert second["channelCode"] == "vf-channel-copy-2"


def test_copy_channel_accepts_custom_code_and_rejects_duplicate(base_agent: dict) -> None:
    source = client.post("/api/admin/channels", json=_payload(base_agent)).json()["data"]
    resp = client.post(
        f"/api/admin/channels/{source['channelId']}/copy",
        json={"channelCode": "official-copy", "channelName": "官网副本"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["channelCode"] == "official-copy"
    assert resp.json()["data"]["channelName"] == "官网副本"

    dup = client.post(
        f"/api/admin/channels/{source['channelId']}/copy",
        json={"channelCode": "official-copy"},
    )
    assert dup.status_code == 409
    assert "已存在" in dup.json()["detail"]


def test_copy_unknown_channel_404() -> None:
    resp = client.post("/api/admin/channels/999999/copy", json={})
    assert resp.status_code == 404


# ------------------------------------------------------------------ MySQL 分支（桩）
class _Result:
    def __init__(self, rows=()) -> None:
        self._rows = [tuple(r) for r in rows]

    def all(self) -> list[tuple]:
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._rows[0][0] if self._rows else None


class _Conn:
    def __init__(self, results) -> None:
        self._queue = list(results)
        self.statements: list = []

    def execute(self, stmt, *args, **kwargs) -> _Result:
        self.statements.append(stmt)
        if not self._queue:
            raise AssertionError("MySQL 桩：查询次数多于预期")
        return self._queue.pop(0)


class _Session:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    def __enter__(self) -> _Conn:
        return self._conn

    def __exit__(self, *exc) -> bool:
        return False


def test_mysql_list_merges_channel_agent_rows(monkeypatch) -> None:
    now = datetime(2026, 10, 20, 12, 0, 0)
    channel_row = (
        7,
        "mysql-channel",
        "MySQL 渠道",
        3,
        "欢迎",
        {"primaryColor": "#abcdef"},
        {"routes": [{"role": "教职工", "agentId": 5}]},
        "https://ai.example.edu.cn/?channel=mysql-channel",
        "",
        0,
        None,
        1,
        now,
        now,
    )
    conn = _Conn([_Result([channel_row]), _Result([(7, 3), (7, 5)])])
    monkeypatch.setattr(settings, "kb_store", "mysql")
    monkeypatch.setattr(channel_store, "db_session", lambda: _Session(conn))

    items = channel_store.list_channels()
    assert len(items) == 1
    item = items[0]
    assert item["channelCode"] == "mysql-channel"
    assert item["themeConfig"]["primaryColor"] == "#abcdef"
    assert item["routeConfig"]["routes"][0]["role"] == "教职工"
    assert item["agentIds"] == [3, 5]
    assert item["createdAt"] == "2026-10-20T12:00:00"
    assert len(conn.statements) == 2


def test_normalize_agent_ids_always_puts_default_first() -> None:
    assert channel_store._normalize_agent_ids([5, 3, 5], 3) == [3, 5]
    assert channel_store._normalize_agent_ids([], 3) == [3]
    assert channel_store._normalize_agent_ids(None, 3) == [3]


def test_build_assets_uses_public_base_url(monkeypatch) -> None:
    monkeypatch.setattr(settings, "public_base_url", "https://ai.example.edu.cn/")
    assets = channel_admin.build_assets(
        {
            "channelCode": "code-x",
            "channelName": "渠道 X",
            "themeConfig": {"primaryColor": "#00aa00", "launcherText": "点我"},
        },
        "",
    )
    assert assets["channelUrl"] == "https://ai.example.edu.cn/?channel=code-x"
    assert "https://ai.example.edu.cn/?channel=code-x&amp;embed=1" in assets["iframeSnippet"]


# ------------------------------------------------------------------ 3.4 角色路由
def _route_payload(base_agent: dict, second_agent: dict, **overrides) -> dict:
    payload = _payload(
        base_agent,
        channelCode="vf-route",
        channelName="路由渠道",
        agentIds=[base_agent["agentId"], second_agent["agentId"]],
        routeConfig={
            "routes": [
                {
                    "role": "访客",
                    "agentId": base_agent["agentId"],
                    "welcomeText": "访客欢迎语",
                    "guideQuestions": ["校园网怎么开通？"],
                    "modules": {"appSquare": 0, "shortcuts": 0},
                },
                {
                    "role": "教职工",
                    "agentId": second_agent["agentId"],
                    "welcomeText": "教职工欢迎语",
                    "guideQuestions": ["差旅怎么报销？", "体检怎么预约？"],
                    "modules": {"recommendedApps": 0},
                },
            ]
        },
    )
    payload.update(overrides)
    return payload


def test_route_config_round_trip_and_public_resolution(
    base_agent: dict, second_agent: dict
) -> None:
    resp = client.post("/api/admin/channels", json=_route_payload(base_agent, second_agent))
    assert resp.status_code == 200, resp.text
    item = resp.json()["data"]
    assert [r["role"] for r in item["routeConfig"]["routes"]] == ["访客", "教职工"]

    guest = client.get("/api/channels/vf-route?role=访客").json()["data"]
    assert guest["defaultAgentId"] == base_agent["agentId"]
    assert guest["route"]["matched"] is True
    assert guest["route"]["welcomeText"] == "访客欢迎语"
    assert guest["route"]["guideQuestions"] == ["校园网怎么开通？"]
    assert guest["route"]["modules"]["appSquare"] == 0
    assert guest["route"]["modules"]["shortcuts"] == 0
    assert guest["route"]["modules"]["recommendedApps"] == 1
    assert guest["welcomeText"] == "访客欢迎语"

    staff = client.get("/api/channels/vf-route?role=教职工").json()["data"]
    assert staff["defaultAgentId"] == second_agent["agentId"]
    assert staff["route"]["matched"] is True
    assert staff["route"]["guideQuestions"] == ["差旅怎么报销？", "体检怎么预约？"]
    assert staff["route"]["modules"]["recommendedApps"] == 0
    assert staff["route"]["modules"]["appSquare"] == 1

    # 未配置身份与未知身份都沿用渠道默认；未知身份按访客归一
    inherited = client.get("/api/channels/vf-route?role=研究生").json()["data"]
    assert inherited["route"]["matched"] is False
    assert inherited["route"]["role"] == "研究生"
    assert inherited["route"]["agentId"] == base_agent["agentId"]
    assert inherited["route"]["guideQuestions"] == []
    assert inherited["route"]["modules"] == channel_admin.default_modules()

    unknown = client.get("/api/channels/vf-route?role=外星人").json()["data"]
    assert unknown["route"]["role"] == "访客"
    assert unknown["route"]["matched"] is True
    assert client.get("/api/channels/vf-route").json()["data"]["route"]["role"] == "访客"

    # 清空路由后回到渠道默认
    resp = client.patch("/api/admin/channels/%s" % item["channelId"], json={"routeConfig": {}})
    assert resp.status_code == 200
    assert resp.json()["data"]["routeConfig"] == {"routes": []}
    cleared = client.get("/api/channels/vf-route?role=教职工").json()["data"]
    assert cleared["route"]["matched"] is False
    assert cleared["defaultAgentId"] == base_agent["agentId"]


def test_route_validation(base_agent: dict, second_agent: dict) -> None:
    def fail(config: dict, keyword: str) -> None:
        resp = client.post(
            "/api/admin/channels",
            json=_route_payload(base_agent, second_agent, routeConfig=config),
        )
        assert resp.status_code == 400, resp.text
        assert keyword in resp.json()["detail"]

    fail({"routes": "x"}, "数组")
    fail({"routes": [{"role": "游客"}]}, "身份非法")
    fail({"routes": [{"role": "访客"}, {"role": "访客"}]}, "重复")
    fail({"routes": [{"role": "访客", "agentId": 999999}]}, "不在渠道绑定集合")
    fail(
        {"routes": [{"role": "访客", "guideQuestions": [f"问题{i}" for i in range(7)]}]},
        "最多 6 条",
    )
    fail(
        {"routes": [{"role": "访客", "guideQuestions": ["问" * 121]}]},
        "不能超过 120",
    )
    fail(
        {"routes": [{"role": "访客", "modules": {"unknown": 1}}]},
        "未知功能模块",
    )
    fail(
        {"routes": [{"role": "访客", "modules": {"appSquare": "yes"}}]},
        "取值非法",
    )


def test_route_falls_back_when_agent_unpublished(
    base_agent: dict, second_agent: dict
) -> None:
    item = client.post(
        "/api/admin/channels", json=_route_payload(base_agent, second_agent)
    ).json()["data"]
    agent_store.update_agent(second_agent["agentId"], {"status": 0})
    staff = client.get("/api/channels/vf-route?role=教职工").json()["data"]
    assert staff["route"]["matched"] is True
    assert staff["defaultAgentId"] == base_agent["agentId"]
    assert staff["route"]["welcomeText"] == "教职工欢迎语"
    client.delete(f"/api/admin/channels/{item['channelId']}")


def test_unbinding_agent_resets_route_to_channel_default(
    base_agent: dict, second_agent: dict
) -> None:
    item = client.post(
        "/api/admin/channels", json=_route_payload(base_agent, second_agent)
    ).json()["data"]
    resp = client.patch(
        f"/api/admin/channels/{item['channelId']}",
        json={"agentIds": [base_agent["agentId"]]},
    )
    assert resp.status_code == 200, resp.text
    routes = {r["role"]: r for r in resp.json()["data"]["routeConfig"]["routes"]}
    assert routes["教职工"]["agentId"] is None
    staff = client.get("/api/channels/vf-route?role=教职工").json()["data"]
    assert staff["defaultAgentId"] == base_agent["agentId"]
    assert staff["route"]["welcomeText"] == "教职工欢迎语"
    client.delete(f"/api/admin/channels/{item['channelId']}")


def test_copy_channel_copies_route_config(base_agent: dict, second_agent: dict) -> None:
    item = client.post(
        "/api/admin/channels", json=_route_payload(base_agent, second_agent)
    ).json()["data"]
    copied = client.post(f"/api/admin/channels/{item['channelId']}/copy", json={})
    assert copied.status_code == 200, copied.text
    copy = copied.json()["data"]
    assert copy["status"] == 0
    assert copy["routeConfig"] == item["routeConfig"]
    client.delete(f"/api/admin/channels/{copy['channelId']}")
    client.delete(f"/api/admin/channels/{item['channelId']}")
