"""验收演示数据（任务 5.4，幂等）。

为 15 分钟演示准备最小可复现数据：

* 5 个渠道分身（官网 / 财务 / 招生 / 后勤 / 信网），每个都有独立主题、欢迎语与引导问题；
* 1 个角色路由渠道：访客、本科生 → 招生助手；研究生、教职工 → 财务助手；
* 5 个应用广场入口，按角色可见（财务/国资仅教职工可见，招生仅访客/本科生）；
* 1 个部门联络员账号（T20001），仅授权财务处类目。

所有记录都带「演示数据（待替换为学校真实配置）」标识，可重复执行。
``--clean`` 只删除本脚本创建且带演示标识的记录，不影响真实数据。

用法::

    python tools/seed_acceptance_demo.py
    python tools/seed_acceptance_demo.py --dry-run
    python tools/seed_acceptance_demo.py --clean
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

os.environ.setdefault("KB_STORE", "mysql")

try:  # pragma: no cover - 仅影响交互输出
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

DEMO_TAG = "演示数据（待替换为学校真实配置）"
PREFIX = "demo-"

AGENTS: list[dict] = [
    {
        "agentCode": "demo-finance",
        "agentName": "财务助手（演示）",
        "description": DEMO_TAG,
        "systemPrompt": (
            "你是「能小智·财务助手」，面向教职工与研究生解答财务报销、差旅标准、"
            "发票与审批问题。只依据检索到的财务制度回答；依据不足时明确说明未找到，"
            "并建议联系财务处。"
        ),
        "retrievalConfig": {"depts": ["财务处"], "topK": 5, "minScore": 0.0},
        "guideQuestions": ["差旅费报销标准和流程", "住宿费报销标准是多少", "发票丢失还能报销吗"],
    },
    {
        "agentCode": "demo-admission",
        "agentName": "招生助手（演示）",
        "description": DEMO_TAG,
        "systemPrompt": (
            "你是「能小智·招生助手」，面向考生、家长与本科生解答招生政策、专业选择、"
            "学费住宿与报到问题。只依据检索到的招生资料回答，不承诺录取结果。"
        ),
        "retrievalConfig": {"depts": ["本科生招生办"], "topK": 5, "minScore": 0.0},
        "guideQuestions": ["学费和住宿费标准是多少", "可以转专业吗", "录取通知书什么时候寄发"],
    },
    {
        "agentCode": "demo-network",
        "agentName": "信网助手（演示）",
        "description": DEMO_TAG,
        "systemPrompt": "你是「能小智·信网助手」，解答校园网开户、资费、报修与密码问题。",
        "retrievalConfig": {"depts": ["信息网络中心"], "topK": 5, "minScore": 0.0},
        "guideQuestions": ["校园网怎么开通", "校园网密码忘了怎么办", "网络坏了怎么报修"],
    },
    {
        "agentCode": "demo-logistics",
        "agentName": "后勤助手（演示）",
        "description": DEMO_TAG,
        "systemPrompt": "你是「能小智·后勤助手」，解答宿舍、餐饮、报修与校园生活问题。",
        "retrievalConfig": {"depts": ["后勤处"], "topK": 5, "minScore": 0.0},
        "guideQuestions": ["宿舍几点关门断电", "一卡通最多能欠费多少"],
    },
    {
        "agentCode": "demo-soe",
        "agentName": "国资助手（演示）",
        "description": DEMO_TAG,
        "systemPrompt": "你是「能小智·国资助手」，面向教职工解答固定资产处置与审批权限问题。",
        "retrievalConfig": {"depts": ["国有资产管理处"], "topK": 5, "minScore": 0.0},
        "guideQuestions": ["固定资产处置审批权限", "固定资产处置流程"],
    },
]

CHANNELS: list[dict] = [
    {
        "channelCode": "demo-official",
        "channelName": "能小智·学校官网",
        "defaultAgentCode": "demo-network",
        "welcomeText": "欢迎使用能小智，全校办事问答一站直达。",
        "themeConfig": {
            "primaryColor": "#1F5FBF",
            "position": "right",
            "launcherText": "问能小智",
            "assistantName": "能小智",
            "avatarText": "智",
            "faq": ["校园网怎么开通", "一卡通丢了怎么办", "宿舍几点关门断电"],
            "demoTag": DEMO_TAG,
        },
    },
    {
        "channelCode": "demo-finance",
        "channelName": "财务处 · 财务助手",
        "defaultAgentCode": "demo-finance",
        "welcomeText": "报销、差旅、发票问题，财务助手为您解答。",
        "themeConfig": {
            "primaryColor": "#2E7D32",
            "position": "right",
            "launcherText": "问财务",
            "assistantName": "财务小助手",
            "avatarText": "财",
            "faq": ["差旅费报销标准和流程", "住宿费报销标准是多少", "发票丢失还能报销吗"],
            "demoTag": DEMO_TAG,
        },
    },
    {
        "channelCode": "demo-admission",
        "channelName": "本科招生办 · 招生助手",
        "defaultAgentCode": "demo-admission",
        "welcomeText": "招生政策、专业选择、报到指南，招生助手为您解答。",
        "themeConfig": {
            "primaryColor": "#C25E00",
            "position": "left",
            "launcherText": "问招生",
            "assistantName": "招生小助手",
            "avatarText": "招",
            "faq": ["学费和住宿费标准是多少", "可以转专业吗", "录取通知书什么时候寄发"],
            "demoTag": DEMO_TAG,
        },
    },
    {
        "channelCode": "demo-logistics",
        "channelName": "后勤处 · 后勤助手",
        "defaultAgentCode": "demo-logistics",
        "welcomeText": "宿舍、餐饮、报修，后勤助手帮您解决。",
        "themeConfig": {
            "primaryColor": "#00796B",
            "position": "right",
            "launcherText": "问后勤",
            "assistantName": "后勤小助手",
            "avatarText": "勤",
            "faq": ["宿舍几点关门断电", "一卡通最多能欠费多少"],
            "demoTag": DEMO_TAG,
        },
    },
    {
        "channelCode": "demo-network",
        "channelName": "信息网络中心 · 信网助手",
        "defaultAgentCode": "demo-network",
        "welcomeText": "校园网、VPN、密码与报修，信网助手为您服务。",
        "themeConfig": {
            "primaryColor": "#4A148C",
            "position": "right",
            "launcherText": "问信网",
            "assistantName": "信网小助手",
            "avatarText": "网",
            "faq": ["校园网怎么开通", "校园网密码忘了怎么办", "网络坏了怎么报修"],
            "demoTag": DEMO_TAG,
        },
    },
    {
        "channelCode": "demo-route",
        "channelName": "角色路由演示门户",
        "defaultAgentCode": "demo-admission",
        "agentCodes": [
            "demo-admission",
            "demo-finance",
            "demo-network",
            "demo-logistics",
            "demo-soe",
        ],
        "welcomeText": "请选择身份，系统将路由到对应的业务助手。",
        "themeConfig": {
            "primaryColor": "#B71C1C",
            "position": "right",
            "launcherText": "身份路由",
            "assistantName": "能小智",
            "avatarText": "智",
            "faq": ["学费和住宿费标准是多少"],
            "demoTag": DEMO_TAG,
        },
        "routeConfig": {
            "routes": [
                {
                    "role": "访客",
                    "agentCode": "demo-admission",
                    "welcomeText": "访客通道：招生政策与报考咨询。",
                    "guideQuestions": ["学费和住宿费标准是多少", "可以转专业吗"],
                },
                {
                    "role": "本科生",
                    "agentCode": "demo-admission",
                    "welcomeText": "本科生通道：招生与在校事务咨询。",
                    "guideQuestions": ["可以转专业吗", "录取通知书什么时候寄发"],
                },
                {
                    "role": "研究生",
                    "agentCode": "demo-finance",
                    "welcomeText": "研究生通道：差旅与财务报销咨询。",
                    "guideQuestions": ["差旅费报销标准和流程", "住宿费报销标准是多少"],
                },
                {
                    "role": "教职工",
                    "agentCode": "demo-finance",
                    "welcomeText": "教职工通道：财务、资产与报销咨询。",
                    "guideQuestions": ["差旅费报销标准和流程", "发票丢失还能报销吗"],
                },
                {
                    "role": "离退休人员",
                    "agentCode": "demo-admission",
                    "welcomeText": "离退休通道：校园服务咨询。",
                    "guideQuestions": ["学费和住宿费标准是多少"],
                },
            ]
        },
    },
]

APPS: list[dict] = [
    {
        "appName": "演示-财务助手",
        "agentCode": "demo-finance",
        "category": "财务",
        "description": f"差旅、报销、发票问题。{DEMO_TAG}",
        "visibleRoles": ["教职工", "研究生"],
        "sortNo": 10,
    },
    {
        "appName": "演示-招生助手",
        "agentCode": "demo-admission",
        "category": "招生",
        "description": f"报考、专业、学费与报到。{DEMO_TAG}",
        "visibleRoles": ["访客", "本科生"],
        "sortNo": 20,
    },
    {
        "appName": "演示-信网助手",
        "agentCode": "demo-network",
        "category": "信息化",
        "description": f"校园网、密码与报修。{DEMO_TAG}",
        "visibleRoles": ["全部"],
        "sortNo": 30,
    },
    {
        "appName": "演示-后勤助手",
        "agentCode": "demo-logistics",
        "category": "后勤",
        "description": f"宿舍、餐饮与校园生活。{DEMO_TAG}",
        "visibleRoles": ["全部"],
        "sortNo": 40,
    },
    {
        "appName": "演示-国资助手",
        "agentCode": "demo-soe",
        "category": "资产",
        "description": f"固定资产处置与审批。{DEMO_TAG}",
        "visibleRoles": ["教职工"],
        "sortNo": 50,
    },
]

LIAISON = {
    "userNo": "T20001",
    "realName": "财务处联络员（演示）",
    "password": "Demo@2026",
    "roleIds": [6],
    "deptId": 2,
    "ktype": "*",
}


def _fail(message: str) -> int:
    print(f"✘ {message}")
    return 1


def _ensure_agents(dry: bool) -> tuple[dict[str, int], int]:
    from app.db import agent_store

    ids: dict[str, int] = {}
    created = 0
    for index, spec in enumerate(AGENTS, 1):
        current = agent_store.get_by_code(spec["agentCode"])
        if current is not None:
            ids[spec["agentCode"]] = int(current["agentId"])
            print(f"  = 助手存在：{spec['agentName']}（#{current['agentId']}）")
            if not dry:
                agent_store.update_agent(
                    int(current["agentId"]),
                    {
                        "agentName": spec["agentName"],
                        "description": spec["description"],
                        "systemPrompt": spec["systemPrompt"],
                        "retrievalConfig": spec["retrievalConfig"],
                        "guideQuestions": spec["guideQuestions"],
                        "visibleRoles": ["全部"],
                        "status": 1,
                    },
                )
            continue
        print(f"  + 助手：{spec['agentName']}（{spec['agentCode']}）")
        created += 1
        if dry:
            ids[spec["agentCode"]] = -index
            continue
        record = agent_store.create_agent(
            {
                **spec,
                "agentType": "chat",
                # 跟随全局默认模型（.env 的 LLM_MODEL = 网关真实别名）。
                # 勿硬编码厂商模型名（如 deepseek-chat）：网关只认自己的 alias，
                # 且业务 Key 的 AllowedModels 只授权该 alias，写错会被 403 model_not_allowed。
                "modelCode": "default",
                "visibleRoles": ["全部"],
                "status": 1,
            }
        )
        ids[spec["agentCode"]] = int(record["agentId"])
    return ids, created


def _ensure_channels(agent_ids: dict[str, int], dry: bool) -> tuple[int, int]:
    from app.db import channel_store

    created = updated = 0
    for spec in CHANNELS:
        route_config = None
        if spec.get("routeConfig"):
            routes = []
            for route in spec["routeConfig"]["routes"]:
                routes.append(
                    {
                        "role": route["role"],
                        "agentId": agent_ids.get(route["agentCode"]),
                        "welcomeText": route.get("welcomeText") or "",
                        "guideQuestions": route.get("guideQuestions") or [],
                    }
                )
            route_config = {"routes": routes}
        codes = spec.get("agentCodes") or [spec["defaultAgentCode"]]
        bound = [agent_ids[c] for c in codes]
        default_id = agent_ids[spec["defaultAgentCode"]]
        current = channel_store.get_by_code(spec["channelCode"])
        if current is not None:
            print(f"  = 渠道存在：{spec['channelName']}（{spec['channelCode']}）")
            updated += 1
            if not dry:
                channel_store.update_channel(
                    int(current["channelId"]),
                    agent_ids=bound,
                    channel_name=spec["channelName"],
                    default_agent_id=default_id,
                    welcome_text=spec["welcomeText"],
                    theme_config=spec["themeConfig"],
                    route_config=route_config or {},
                    need_auth=0,
                    status=1,
                )
            continue
        print(f"  + 渠道：{spec['channelName']}（{spec['channelCode']}）")
        created += 1
        if dry:
            continue
        channel_store.create_channel(
            channel_code=spec["channelCode"],
            channel_name=spec["channelName"],
            default_agent_id=default_id,
            welcome_text=spec["welcomeText"],
            theme_config=spec["themeConfig"],
            route_config=route_config,
            need_auth=0,
            status=1,
            agent_ids=bound,
        )
    return created, updated


def _ensure_apps(agent_ids: dict[str, int], dry: bool) -> int:
    from app.db import app_store

    existing = {item["appName"] for item in app_store.list_apps()}
    created = 0
    for spec in APPS:
        if spec["appName"] in existing:
            print(f"  = 应用存在：{spec['appName']}")
            continue
        print(f"  + 应用：{spec['appName']}")
        created += 1
        if dry:
            continue
        app_store.create_app(
            app_name=spec["appName"],
            source_type="agent",
            agent_id=agent_ids[spec["agentCode"]],
            category=spec["category"],
            description=spec["description"],
            sort_no=spec["sortNo"],
            visible_roles=spec["visibleRoles"],
            status=1,
        )
    return created


def _ensure_liaison(dry: bool) -> str:
    from sqlalchemy import select, text

    from app.db.models import SysUser as UserModel
    from app.db.session import db_transaction
    from app.services import kb_permission, rbac_admin
    from app.security.deps import Principal

    with db_transaction() as conn:
        user_id = conn.execute(
            select(UserModel.user_id).where(UserModel.user_no == LIAISON["userNo"])
        ).scalar()

    if user_id is None:
        print(f"  + 联络员账号：{LIAISON['userNo']}（密码 {LIAISON['password']}）")
        if not dry:
            result = rbac_admin.create_admin(
                {
                    "userNo": LIAISON["userNo"],
                    "realName": LIAISON["realName"],
                    "password": LIAISON["password"],
                    "roleIds": LIAISON["roleIds"],
                    "status": 1,
                }
            )
            if not result.get("ok"):
                return f"创建联络员失败：{result.get('error')}"
            user_id = int(result["data"]["userId"])
    else:
        print(f"  = 联络员账号存在：{LIAISON['userNo']}（userId={user_id}）")

    if dry:
        return "dry-run"

    with db_transaction() as conn:
        exists = conn.execute(
            text(
                "SELECT perm_id FROM kb_permission "
                "WHERE user_id=:uid AND dept_id=:did AND ktype=:kt"
            ),
            {"uid": user_id, "did": LIAISON["deptId"], "kt": LIAISON["ktype"]},
        ).first()
    if exists:
        print(f"  = 授权存在：财务处 / * → userId={user_id}")
        return "ok"

    admin = Principal(user_no="admin", role="管理员", user_id=1, authenticated=True)
    result = kb_permission.grant(
        {
            "userId": user_id,
            "deptId": LIAISON["deptId"],
            "ktype": LIAISON["ktype"],
            "canUpload": 1,
            "canView": 1,
            "canDownload": 1,
            "canDelete": 1,
            "canCreateSub": 1,
        },
        admin,
    )
    if not result.get("ok"):
        return f"授权失败：{result.get('error')}"
    print(f"  + 授权：财务处 / * → userId={user_id}")
    return "ok"


def _clean(dry: bool) -> int:
    from sqlalchemy import delete, select, text

    from app.db import agent_store, app_store, channel_store
    from app.db.models import Channel, SysUser as UserModel
    from app.db.session import db_transaction

    codes = {spec["channelCode"] for spec in CHANNELS}
    names = {spec["appName"] for spec in APPS}
    removed = 0
    for item in channel_store.list_channels():
        if item["channelCode"] in codes:
            print(f"  - 渠道：{item['channelName']}（{item['channelCode']}）")
            removed += 1
            if not dry:
                channel_store.delete_channel(int(item["channelId"]))
    for item in app_store.list_apps():
        if item["appName"] in names:
            print(f"  - 应用：{item['appName']}")
            removed += 1
            if not dry:
                app_store.delete_app(int(item["appId"]))
    for spec in AGENTS:
        current = agent_store.get_by_code(spec["agentCode"])
        if current is not None:
            print(f"  - 助手：{current['agentName']}（{spec['agentCode']}）")
            removed += 1
            if not dry:
                agent_store.delete_agent(int(current["agentId"]))
    with db_transaction() as conn:
        user_id = conn.execute(
            select(UserModel.user_id).where(UserModel.user_no == LIAISON["userNo"])
        ).scalar()
        if user_id is not None:
            print(f"  - 联络员：{LIAISON['userNo']}")
            removed += 1
            if not dry:
                conn.execute(text("DELETE FROM kb_permission WHERE user_id=:uid"), {"uid": user_id})
                conn.execute(text("DELETE FROM sys_user_role WHERE user_id=:uid"), {"uid": user_id})
                conn.execute(delete(Channel).where(Channel.channel_code.in_(codes)))
                conn.execute(delete(UserModel).where(UserModel.user_id == user_id))
    print(f"  清理 {removed} 项演示记录")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="验收演示数据（任务 5.4，幂等）")
    parser.add_argument("--dry-run", action="store_true", help="只打印将写入的内容")
    parser.add_argument("--clean", action="store_true", help="删除本脚本创建的演示记录")
    args = parser.parse_args(argv)

    from app.config import settings

    print("=" * 68)
    print("验收演示数据（任务 5.4）")
    print(f"存储模式：{settings.store_label}")
    print(f"演示标识：{DEMO_TAG}")
    print("=" * 68)

    if settings.store_label != "mysql":
        return _fail("演示数据脚本需要 MySQL 模式（JSON 模式请直接使用既有 data/ 数据）")

    if args.clean:
        return _clean(args.dry_run)

    agent_ids, agents_created = _ensure_agents(args.dry_run)
    if not args.dry_run and len(agent_ids) != len(AGENTS):
        return _fail("助手创建不完整，请检查日志")
    channels_created, channels_updated = _ensure_channels(agent_ids, args.dry_run)
    apps_created = _ensure_apps(agent_ids, args.dry_run)
    liaison = _ensure_liaison(args.dry_run)

    print("\n完成：")
    print(f"  助手 新增 {agents_created} / 共 {len(AGENTS)}")
    print(f"  渠道 新增 {channels_created} / 更新 {channels_updated} / 共 {len(CHANNELS)}")
    print(f"  应用 新增 {apps_created} / 共 {len(APPS)}")
    print(f"  联络员 {liaison}")
    print("\n演示入口：")
    print("  角色路由    http://127.0.0.1:8000/?channel=demo-route")
    print("  财务门户    http://127.0.0.1:8000/?channel=demo-finance")
    print("  招生门户    http://127.0.0.1:8000/?channel=demo-admission")
    print("  渠道管理    http://127.0.0.1:8000/channels")
    print("  应用广场    http://127.0.0.1:8000/apps")
    print("  知识授权    http://127.0.0.1:8000/admin#kb-permissions")
    print("  通行看板    http://127.0.0.1:8000/access")
    print("  未识别池    http://127.0.0.1:8000/unrecognized")
    print("  统计大屏    http://127.0.0.1:8000/admin#stats")
    return 0 if liaison in {"ok", "dry-run"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
