"""智能体广场应用管理与角色授权（任务 3.3 / M-C-05·06）。

* **应用**：名称、来源（智能体 / 外部链接）、分类、简介、图标、排序、启停；
* **授权**：``visibleRoles`` 按身份角色分组可见（全部 / 访客 / 本科生 /
  研究生 / 教职工 / 离退休人员），用户端广场按当前角色过滤；
* **来源**：``agent`` 绑定已有助手，助手下架或删除后应用自动从广场隐藏；
  ``link`` 指向外部应用，链接走协议白名单。

校验策略与渠道一致：收集全部错误一次返回（400），便于管理页一次改全。
"""

from __future__ import annotations

import re

from app.db import agent_store, app_store

MAX_NAME = 128
MAX_CATEGORY = 32
MAX_DESCRIPTION = 255
MAX_ICON = 255
MAX_LINK = 512
SORT_RANGE = (0, 9999)

ROLE_OPTIONS = [app_store.ALL_ROLE, *app_store.VISIBLE_ROLES]
SOURCE_OPTIONS = [
    {"value": "agent", "label": "智能体"},
    {"value": "link", "label": "外部链接"},
]
CATEGORY_SUGGESTIONS = ("学习学术", "校园生活", "事务办理", "信息服务", "其他")

_UNSAFE_SCHEME = ("javascript:", "vbscript:", "data:", "file:", "blob:")
_SAFE_PREFIX = ("http://", "https://", "/", "#", "./", "../")


def _ok(data) -> dict:
    return {"ok": True, "data": data, "error": None}


def _fail(message: str, status: int = 400) -> dict:
    return {"ok": False, "data": None, "error": message, "status": status}


def _clean_link(raw) -> tuple[str | None, str | None]:
    if raw is None:
        return "", None
    if not isinstance(raw, str):
        return None, "链接必须是字符串"
    url = raw.strip()
    if not url:
        return "", None
    if len(url) > MAX_LINK:
        return None, f"链接不能超过 {MAX_LINK} 个字符"
    if re.search(r"[\s\u0000-\u001f\u007f]", url):
        return None, "链接不能包含空白或控制字符"
    compact = url.lower()
    if compact.startswith(_UNSAFE_SCHEME):
        return None, "链接协议不安全，仅支持 http(s) 或站内路径"
    if not compact.startswith(_SAFE_PREFIX):
        return None, "链接需以 http(s)://、/ 或 # 开头"
    return url, None


def _clean_icon(raw) -> tuple[str | None, str | None]:
    """图标允许 emoji/短文本或 http(s)/站内 URL；脚本协议一律拒绝。"""
    if raw is None:
        return "", None
    if not isinstance(raw, str):
        return None, "图标必须是字符串"
    text = raw.strip()
    if not text:
        return "", None
    if len(text) > MAX_ICON:
        return None, f"图标不能超过 {MAX_ICON} 个字符"
    compact = text.lower()
    if compact.startswith(_UNSAFE_SCHEME):
        return None, "图标链接协议不安全"
    if "://" in compact and not compact.startswith(("http://", "https://")):
        return None, "图标链接需以 http(s):// 开头"
    return text, None


def _clean_roles(raw) -> tuple[list[str] | None, str | None]:
    if raw is None:
        return [app_store.ALL_ROLE], None
    if isinstance(raw, str):
        raw = raw.replace("、", ",").split(",")
    if not isinstance(raw, list):
        return None, "可见角色必须是数组"
    roles: list[str] = []
    for value in raw:
        name = str(value or "").strip()
        if not name:
            continue
        if name not in ROLE_OPTIONS:
            return None, f"未知角色：{name}"
        if name not in roles:
            roles.append(name)
    if not roles:
        return [app_store.ALL_ROLE], None
    if app_store.ALL_ROLE in roles:
        return [app_store.ALL_ROLE], None
    order = {name: idx for idx, name in enumerate(ROLE_OPTIONS)}
    roles.sort(key=lambda name: order.get(name, len(order)))
    return roles, None


def _as_int(raw, label: str, *, low: int, high: int, default=None):
    if raw in (None, ""):
        return default, None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None, f"{label}必须是整数"
    if not low <= value <= high:
        return None, f"{label}需在 {low}–{high} 之间"
    return value, None


def _validate(payload: dict, base: dict | None = None) -> tuple[dict | None, list[str]]:
    """`payload` 为 camelCase 增量；`base` 为已有应用（更新时），合并后整体校验。"""
    data = dict(base or {})
    data.update({key: value for key, value in (payload or {}).items() if value is not None})

    errors: list[str] = []

    name = str(data.get("appName") or "").strip()
    if not name:
        errors.append("应用名称不能为空")
    elif len(name) > MAX_NAME:
        errors.append(f"应用名称不能超过 {MAX_NAME} 个字符")

    category = str(data.get("category") or "").strip()
    if not category:
        errors.append("应用分类不能为空")
    elif len(category) > MAX_CATEGORY:
        errors.append(f"应用分类不能超过 {MAX_CATEGORY} 个字符")

    description = str(data.get("description") or "").strip()
    if len(description) > MAX_DESCRIPTION:
        errors.append(f"应用简介不能超过 {MAX_DESCRIPTION} 个字符")

    icon, err = _clean_icon(data.get("iconUrl"))
    if err:
        errors.append(err)

    source_type = str(data.get("sourceType") or "agent").strip()
    if source_type not in app_store.SOURCE_TYPES:
        errors.append("应用来源只能是智能体或外部链接")
        source_type = "agent"

    agent_id = None
    link_url = ""
    if source_type == "agent":
        raw_agent = data.get("agentId")
        agent_id, err = _as_int(raw_agent, "绑定智能体", low=1, high=2**31 - 1)
        if err:
            errors.append(err)
        elif agent_id is None:
            errors.append("来源为智能体时必须选择绑定助手")
        elif agent_store.get_agent(agent_id) is None:
            errors.append("绑定的智能体不存在")
    else:
        link_url, err = _clean_link(data.get("linkUrl"))
        if err:
            errors.append(err)
        elif not link_url:
            errors.append("来源为外部链接时必须填写链接地址")

    sort_no, err = _as_int(data.get("sortNo"), "排序", low=SORT_RANGE[0],
                           high=SORT_RANGE[1], default=0)
    if err:
        errors.append(err)

    visible_roles, err = _clean_roles(data.get("visibleRoles"))
    if err:
        errors.append(err)

    status, err = _as_int(data.get("status"), "状态", low=0, high=1, default=1)
    if err:
        errors.append(err)

    if errors:
        return None, errors
    return {
        "app_name": name,
        "source_type": source_type,
        "agent_id": agent_id,
        "link_url": link_url or "",
        "category": category,
        "description": description,
        "icon_url": icon or "",
        "sort_no": sort_no if sort_no is not None else 0,
        "visible_roles": visible_roles,
        "status": status if status is not None else 1,
    }, []


# ------------------------------------------------------------------ 管理端
def _decorate(item: dict) -> dict:
    if item.get("sourceType") == "agent" and item.get("agentId"):
        agent = agent_store.get_agent(int(item["agentId"]))
        item["agentName"] = agent["agentName"] if agent else ""
        item["agentStatus"] = int(agent.get("status", 1)) if agent else 0
        item["agentMissing"] = agent is None
    else:
        item["agentName"] = ""
        item["agentStatus"] = 0
        item["agentMissing"] = False
    return item


def list_apps() -> dict:
    items = [_decorate(item) for item in app_store.list_apps()]
    agents = [
        {
            "agentId": int(agent["agentId"]),
            "agentName": agent["agentName"],
            "status": int(agent.get("status", 1)),
        }
        for agent in agent_store.list_agents()
    ]
    agents.sort(key=lambda a: (a["status"] != 1, a["agentName"]))
    categories = list(CATEGORY_SUGGESTIONS)
    for item in items:
        if item["category"] and item["category"] not in categories:
            categories.append(item["category"])
    return _ok({
        "items": items,
        "total": len(items),
        "enabled": sum(1 for item in items if item["status"] == 1),
        "roleOptions": ROLE_OPTIONS,
        "sourceOptions": SOURCE_OPTIONS,
        "categoryOptions": categories,
        "agentOptions": agents,
    })


def get_app(app_id: int) -> dict:
    item = app_store.get_app(int(app_id))
    if item is None:
        return _fail("应用不存在", 404)
    return _ok(_decorate(item))


def create_app(payload: dict) -> dict:
    fields, errors = _validate(payload)
    if errors:
        return _fail("；".join(errors))
    app_id = app_store.create_app(**fields)
    return _ok(_decorate(app_store.get_app(app_id) or {}))


def update_app(app_id: int, payload: dict) -> dict:
    existing = app_store.get_app(int(app_id))
    if existing is None:
        return _fail("应用不存在", 404)
    fields, errors = _validate(payload, existing)
    if errors:
        return _fail("；".join(errors))
    app_store.update_app(int(app_id), **fields)
    return _ok(_decorate(app_store.get_app(int(app_id)) or {}))


def delete_app(app_id: int) -> dict:
    if not app_store.delete_app(int(app_id)):
        return _fail("应用不存在", 404)
    return _ok({"deleted": True, "appId": int(app_id)})


# ------------------------------------------------------------------ 用户端广场
def _match_role(item: dict, role: str) -> bool:
    roles = item.get("visibleRoles") or [app_store.ALL_ROLE]
    return app_store.ALL_ROLE in roles or role in roles


def public_apps(role: str | None) -> dict:
    """按身份角色返回上架应用；助手已删除/下架时该应用自动隐藏。"""
    current = (role or "").strip()
    if current not in app_store.VISIBLE_ROLES:
        current = "访客"
    apps: list[dict] = []
    for item in app_store.list_apps():
        if int(item.get("status", 0)) != 1 or not _match_role(item, current):
            continue
        agent_name = ""
        guide_questions: list[str] = []
        if item.get("sourceType") == "agent":
            agent = agent_store.get_agent(int(item.get("agentId") or 0))
            if agent is None or int(agent.get("status", 1)) != 1:
                continue
            agent_name = agent["agentName"]
            # 任务 3.8：从应用广场切到该助手时，首屏同步展示它的场景引导问题
            guide_questions = list(agent.get("guideQuestions") or [])
        apps.append({
            "appId": item["appId"],
            "appName": item["appName"],
            "sourceType": item["sourceType"],
            "agentId": item.get("agentId"),
            "agentName": agent_name,
            "guideQuestions": guide_questions,
            "linkUrl": item.get("linkUrl") or "",
            "category": item.get("category") or "",
            "description": item.get("description") or "",
            "iconUrl": item.get("iconUrl") or "",
            "sortNo": item.get("sortNo") or 0,
        })
    return _ok({"role": current, "total": len(apps), "apps": apps})
