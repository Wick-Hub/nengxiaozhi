"""智能体（助手）管理服务（任务 2.2 / M-A-01·02·03）。

职责边界：

* **本任务**：场景智能体搭建、助手创建、基础配置（名称/描述/头像/流式启停/
  启禁用/风控话术/兜底话术），落 ``agent`` 表（JSON 模式落 ``data/agents.json``）。
* **不在本任务**：提示词在线生效（M-A-04）、知识库绑定与检索参数（M-A-05）、
  大模型 CRUD（M-A-06）——分别属任务 2.3 / 2.4。本层只把
  ``system_prompt`` / ``model_code`` / ``temperature`` / ``top_p`` / ``max_tokens``
  作为**可存储、可读取**的字段提供出来，供后续任务接线。

校验策略：**先收集全部错误再一次返回**（而不是遇到第一个就抛），
便于管理页把问题一次性提示给运营人员。
"""

from __future__ import annotations

import re

from app.core.generator import SYSTEM_PROMPT
from app.db import agent_store, app_store
from app.db.agent_store import AGENT_TYPES, STATUS_OFF, STATUS_ON
from app.services import agent_config

#: 助手编码：小写字母/数字开头，允许 ``-`` 与 ``_``，2–64 位
_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")

#: 可配置字段的上限（与 DDL 列宽一致，避免写入时被截断）
MAX_NAME = 128
MAX_DESCRIPTION = 255
MAX_REPLY = 512
MAX_ICON_URL = 255
MAX_PROMPT = 20000
# 生成参数的取值范围已下移到 ``agent_store``（任务 2.4：校验口径与数据层共用一个来源），
# 需要时用 ``agent_store.TEMPERATURE_RANGE`` / ``TOP_P_RANGE`` / ``MAX_TOKENS_RANGE``。

#: 场景智能体预设（M-A-01：财务/招生/后勤/国资/信网/迎新）。
#: ``code`` 固定，用于判断「该场景是否已搭建」，重复搭建会被拒绝而不是建出重复行。
SCENE_PRESETS: list[dict] = [
    {
        "sceneKey": "finance",
        "agentCode": "scene-finance",
        "agentName": "财务助手",
        "iconUrl": "💰",
        "description": "学费住宿费缴纳、报销流程、经费与票据业务咨询",
        "visibleRoles": ["全部"],
        "guideQuestions": [
            "学费和住宿费什么时候缴纳？",
            "差旅报销需要准备哪些材料？",
            "科研经费入账怎么办理？",
        ],
        "systemPrompt": (
            "你是「能小智·财务助手」，面向全校师生解答财务相关事务。"
            "严格依据检索到的财务制度与办事指南作答，涉及金额、时限、账户信息时必须引用原文；"
            "检索不到时如实说明并引导至计划财务处，不得推测。"
        ),
    },
    {
        "sceneKey": "admission",
        "agentCode": "scene-admission",
        "agentName": "招生助手",
        "iconUrl": "🎓",
        "description": "招生政策、专业介绍、录取与报到相关咨询",
        "visibleRoles": ["访客", "全部"],
        "guideQuestions": [
            "今年的招生计划是多少？",
            "录取通知书什么时候寄出？",
            "新生报到需要带哪些材料？",
        ],
        "systemPrompt": (
            "你是「能小智·招生助手」，面向考生与家长解答招生问题。"
            "招生政策、分数线、专业计划必须严格引用当年官方文件；"
            "不承诺录取结果，检索不到时引导至招生办公室。"
        ),
    },
    {
        "sceneKey": "logistics",
        "agentCode": "scene-logistics",
        "agentName": "后勤助手",
        "iconUrl": "🛠️",
        "description": "宿舍报修、餐饮服务、水电暖与校园环境事务",
        "visibleRoles": ["全部"],
        "guideQuestions": [
            "宿舍设施坏了怎么报修？",
            "食堂的营业时间是几点？",
            "水电费在哪里缴纳？",
        ],
        "systemPrompt": (
            "你是「能小智·后勤助手」，负责后勤保障类咨询。"
            "报修流程、服务网点、收费标准须引用后勤管理制度原文；"
            "紧急安全问题（漏水、停电、燃气）优先给出应急处置方式并提示报修电话。"
        ),
    },
    {
        "sceneKey": "soe",
        "agentCode": "scene-soe",
        "agentName": "国资助手",
        "iconUrl": "🏛️",
        "description": "国有资产登记、招标采购、资产处置流程咨询",
        "visibleRoles": ["教职工", "全部"],
        "guideQuestions": [
            "资产报废需要走哪些流程？",
            "采购限额标准是多少？",
            "招标需要准备哪些材料？",
        ],
        "systemPrompt": (
            "你是「能小智·国资助手」，面向教职工解答国有资产与采购事务。"
            "采购限额、审批权限、报废流程必须逐条引用国资管理制度；"
            "涉及金额阈值与审批层级时不得概括，须给出原文表述。"
        ),
    },
    {
        "sceneKey": "network",
        "agentCode": "scene-network",
        "agentName": "信网助手",
        "iconUrl": "🌐",
        "description": "校园网接入、一卡通、邮箱、VPN 与信息化服务",
        "visibleRoles": ["全部"],
        "guideQuestions": [
            "校园网怎么开通？",
            "一卡通丢了怎么补办？",
            "校外怎么连接 VPN？",
        ],
        "systemPrompt": (
            "你是「能小智·信网助手」，负责信息化服务咨询。"
            "网络开通、密码重置、VPN 使用须给出可执行的分步操作；"
            "涉及账号安全时提醒用户通过官方渠道办理，不索取任何密码。"
        ),
    },
    {
        "sceneKey": "orientation",
        "agentCode": "scene-orientation",
        "agentName": "迎新助手",
        "iconUrl": "🎒",
        "description": "新生报到流程、迎新安排、入学须知与材料准备",
        "visibleRoles": ["访客", "全部"],
        "guideQuestions": [
            "新生什么时候报到？",
            "报到需要准备哪些材料？",
            "迎新接站是怎么安排的？",
        ],
        "systemPrompt": (
            "你是「能小智·迎新助手」，面向新生与家长解答报到事宜。"
            "报到时间、地点、所需材料必须引用当年迎新方案原文；"
            "回答尽量分步骤、给时间节点，检索不到时引导至迎新办公室。"
        ),
    },
]

_PRESET_BY_KEY = {p["sceneKey"]: p for p in SCENE_PRESETS}


# ------------------------------------------------------------------ 校验
def _clip(text: str | None, limit: int) -> str:
    return (text or "")[:limit]


def _validate_retrieval(raw) -> list[str]:
    """检索配置（M-A-05）校验。口径与默认值统一在数据层，此处仅转发。"""
    return agent_store.validate_retrieval(raw)


def _validate(payload: dict, *, partial: bool, existing: dict | None = None) -> list[str]:
    """返回错误信息列表（空列表 = 通过）。"""
    errors: list[str] = []
    base = existing or {}

    def pick(key: str, default=None):
        if key in payload:
            return payload[key]
        return base.get(key, default)

    # 编码
    code = pick("agentCode")
    if code is None or str(code).strip() == "":
        if not partial:
            errors.append("缺少助手编码（agentCode）")
    elif not _CODE_RE.match(str(code).strip()):
        errors.append("助手编码只能是小写字母/数字/-/_，且以字母或数字开头，长度 2–64")

    # 名称
    name = pick("agentName")
    if name is None or str(name).strip() == "":
        if not partial:
            errors.append("缺少助手名称（agentName）")
    elif len(str(name).strip()) > MAX_NAME:
        errors.append(f"助手名称最长 {MAX_NAME} 字符")

    # 类型
    atype = pick("agentType", "chat")
    if atype not in AGENT_TYPES:
        errors.append(f"助手类型非法，可选：{'/'.join(AGENT_TYPES)}")

    # 生成参数（任务 2.4 / M-A-06）：口径与取值范围统一由 agent_store 提供，
    # 避免「服务层与数据层各写一套上下限」。None / 空串 = 沿用平台默认，是合法值。
    errors.extend(agent_store.validate_gen_params(payload))

    # 可见角色：接受数组（前端多选框）或逗号分隔串（脚本/导入场景），
    # 与 :func:`agent_store.split_roles` 的宽容口径保持一致。
    roles = pick("visibleRoles")
    if roles is not None:
        if isinstance(roles, (list, tuple)):
            role_items = [str(r).strip() for r in roles if str(r).strip()]
        else:
            role_items = [p.strip() for p in str(roles).split(",") if p.strip()]
        if not role_items:
            errors.append("可见角色至少选择一项")
        elif any(len(r) > 32 for r in role_items):
            errors.append("单个角色名最长 32 字符")

    # 启停与流式
    if "status" in payload and int(payload["status"]) not in (STATUS_ON, STATUS_OFF):
        errors.append("状态只能是 1（上架）或 0（下架）")
    if "streamEnabled" in payload and int(payload["streamEnabled"]) not in (0, 1):
        errors.append("流式开关只能是 1（开）或 0（关）")

    # 文本长度
    if len(str(pick("description", "") or "")) > MAX_DESCRIPTION:
        errors.append(f"描述最长 {MAX_DESCRIPTION} 字符")
    if len(str(pick("riskReply", "") or "")) > MAX_REPLY:
        errors.append(f"风控话术最长 {MAX_REPLY} 字符")
    if len(str(pick("fallbackReply", "") or "")) > MAX_REPLY:
        errors.append(f"兜底话术最长 {MAX_REPLY} 字符")
    if len(str(pick("iconUrl", "") or "")) > MAX_ICON_URL:
        errors.append(f"头像最长 {MAX_ICON_URL} 字符")
    if len(str(pick("systemPrompt", "") or "")) > MAX_PROMPT:
        errors.append(f"提示词最长 {MAX_PROMPT} 字符")

    # 检索配置（任务 2.3 / M-A-05）：只在显式传入时校验
    if "retrievalConfig" in payload:
        errors.extend(_validate_retrieval(payload["retrievalConfig"]))

    # 场景引导问题（任务 3.8 / P-QA-06）：null/[] 合法（回退渠道话术）
    if "guideQuestions" in payload:
        errors.extend(agent_store.validate_guide_questions(payload["guideQuestions"]))

    return errors


def _ok(data) -> dict:
    return {"ok": True, "data": data, "error": None}


def _fail(message: str, status: int = 400) -> dict:
    """失败结果。``status`` 供接口层映射 HTTP 状态码（400 参数 / 404 不存在 / 409 冲突）。"""
    return {"ok": False, "data": None, "error": message, "status": status}


# ------------------------------------------------------------------ 查询
def list_agents(*, status: int | None = None, keyword: str | None = None) -> dict:
    """助手列表 + 场景预设的搭建状态 + 编辑器所需的平台默认值。

    一次拿全是为了少几次往返：管理页打开「提示词」页签要展示平台默认提示词
    （让运营知道「留空」到底等于什么），打开「检索配置」页签要展示「沿用全局」
    的实际数值。
    """
    items = agent_store.list_agents(status=status, keyword=keyword)
    return _ok(
        {
            "items": items,
            "total": len(items),
            "agentTypes": list(AGENT_TYPES),
            "presets": _presets_with_status(),
            "defaultSystemPrompt": SYSTEM_PROMPT,
            "retrievalDefaults": agent_config.defaults(),
            "retrievalRanges": agent_config.ranges(),
            # 模型下拉 + 生成参数的平台默认值与区间。
            # 一次下发是为了让管理页能显示「留空到底等于什么」而**不写死第二份默认值**。
            # 模型的**唯一来源是网关**（`gateway_admin.choices()`），
            # 因此这里不再下发厂商模板 —— 厂商信息属于网关控制面。
            "modelDefaults": agent_config.model_defaults(),
            "modelRanges": agent_config.model_ranges(),
            "modelChoices": agent_config.model_choices(),
            # 2.11 起配置字段全部接线，保留空数组维持前端契约
            "pendingFields": [],
        }
    )


def _presets_with_status() -> list[dict]:
    created = {a["agentCode"]: a["agentId"] for a in agent_store.list_agents()}
    out: list[dict] = []
    for preset in SCENE_PRESETS:
        out.append(
            {
                **preset,
                "created": preset["agentCode"] in created,
                "agentId": created.get(preset["agentCode"]),
            }
        )
    return out


def get_agent(agent_id: int) -> dict:
    found = agent_store.get_agent(agent_id)
    if found is None:
        return _fail(f"助手不存在：{agent_id}", 404)
    return _ok(found)


def list_presets() -> dict:
    return _ok({"presets": _presets_with_status()})


def public_agents(role: str | None = None) -> dict:
    """门户助手矩阵（任务 3.8 / P-QA-06）：上架且对当前身份可见的助手。

    只下发展示与切换所需字段，``systemPrompt`` / 检索参数等配置不出公开端；
    默认助手（``campus-default``）排最前，供无渠道参数的根门户确定首屏助手。
    """
    current = (role or "").strip()
    if current not in app_store.VISIBLE_ROLES:
        current = "访客"
    items: list[dict] = []
    for agent in agent_store.list_agents(status=STATUS_ON):
        roles = agent.get("visibleRoles") or [app_store.ALL_ROLE]
        if app_store.ALL_ROLE not in roles and current not in roles:
            continue
        items.append(
            {
                "agentId": int(agent["agentId"]),
                "agentCode": agent.get("agentCode") or "",
                "agentName": agent.get("agentName") or "",
                "agentType": agent.get("agentType") or "chat",
                "iconUrl": agent.get("iconUrl") or "",
                "description": agent.get("description") or "",
                "guideQuestions": list(agent.get("guideQuestions") or []),
                "isDefault": (agent.get("agentCode") or "") == agent_store.DEFAULT_AGENT_CODE,
            }
        )
    items.sort(key=lambda a: (not a["isDefault"], a["agentId"]))
    return _ok(
        {
            "role": current,
            "total": len(items),
            "agents": items,
            "defaultAgentId": next((a["agentId"] for a in items if a["isDefault"]), None),
        }
    )


# ------------------------------------------------------------------ 写入
def create_agent(payload: dict) -> dict:
    """自定义创建助手（M-A-02）。"""
    errors = _validate(payload, partial=False)
    code = str(payload.get("agentCode") or "").strip()
    duplicate = bool(code) and agent_store.get_by_code(code) is not None
    if duplicate:
        errors.append(f"助手编码已存在：{code}")
    if errors:
        return _fail("；".join(errors), 409 if duplicate else 400)

    record = {
        "agentCode": code,
        "agentName": str(payload["agentName"]).strip(),
        "agentType": payload.get("agentType") or "chat",
        "systemPrompt": _clip(payload.get("systemPrompt"), MAX_PROMPT),
        "modelCode": payload.get("modelCode") or agent_store.DEFAULT_MODEL_CODE,
        # 生成参数（任务 2.4）：未传 / 传 null = 沿用平台默认（**不物化**具体数值）
        "temperature": payload.get("temperature"),
        "topP": payload.get("topP"),
        "maxTokens": payload.get("maxTokens"),
        "visibleRoles": payload.get("visibleRoles") or ["全部"],
        "iconUrl": _clip(payload.get("iconUrl"), MAX_ICON_URL),
        "description": _clip(payload.get("description"), MAX_DESCRIPTION),
        "streamEnabled": payload.get("streamEnabled", 1),
        "riskReply": _clip(payload.get("riskReply") or agent_store.DEFAULT_RISK_REPLY, MAX_REPLY),
        "fallbackReply": _clip(
            payload.get("fallbackReply") or agent_store.DEFAULT_FALLBACK_REPLY, MAX_REPLY
        ),
        "retrievalConfig": agent_store.normalize_retrieval(payload.get("retrievalConfig")),
        "guideQuestions": agent_store.normalize_guide_questions(payload.get("guideQuestions")),
        "status": payload.get("status", STATUS_ON),
    }
    return _ok(agent_store.create_agent(record))


def create_from_preset(scene_key: str, overrides: dict | None = None) -> dict:
    """一键搭建场景智能体（M-A-01）。已搭建时返回原记录而不是重复建行。"""
    preset = _PRESET_BY_KEY.get(str(scene_key))
    if preset is None:
        return _fail(f"未知场景：{scene_key}（可选：{'/'.join(_PRESET_BY_KEY)}）", 404)

    existed = agent_store.get_by_code(preset["agentCode"])
    if existed is not None:
        return _ok({**existed, "reused": True})

    payload = {
        "agentCode": preset["agentCode"],
        "agentName": preset["agentName"],
        "agentType": "chat",
        "systemPrompt": preset["systemPrompt"],
        "description": preset["description"],
        "iconUrl": preset["iconUrl"],
        "visibleRoles": preset["visibleRoles"],
        "guideQuestions": preset.get("guideQuestions") or [],
        **(overrides or {}),
    }
    result = create_agent(payload)
    if result["ok"]:
        result["data"]["reused"] = False
        result["data"]["sceneKey"] = preset["sceneKey"]
    return result


def update_agent(agent_id: int, patch: dict) -> dict:
    """基础配置更新（M-A-03）。只改传入的字段，未传字段保持原值。"""
    current = agent_store.get_agent(agent_id)
    if current is None:
        return _fail(f"助手不存在：{agent_id}", 404)

    if not patch:
        return _fail("没有需要更新的字段")

    errors = _validate(patch, partial=True, existing=current)
    new_code = patch.get("agentCode")
    conflict = False
    if new_code and str(new_code).strip() != current["agentCode"]:
        if agent_store.get_by_code(str(new_code).strip()) is not None:
            errors.append(f"助手编码已存在：{new_code}")
            conflict = True
    if errors:
        return _fail("；".join(errors), 409 if conflict else 400)

    # 只保留白名单字段，避免把 statusName / createdAt 之类的派生字段写回去
    allowed = {
        "agentCode",
        "agentName",
        "agentType",
        "systemPrompt",
        "modelCode",
        "temperature",
        "topP",
        "maxTokens",
        "visibleRoles",
        "iconUrl",
        "description",
        "streamEnabled",
        "riskReply",
        "fallbackReply",
        "retrievalConfig",
        "guideQuestions",
        "status",
    }
    clean = {k: v for k, v in patch.items() if k in allowed}
    if not clean:
        return _fail("没有可更新的字段（请检查字段名）")

    updated = agent_store.update_agent(agent_id, clean)
    if updated is None:
        return _fail(f"助手不存在：{agent_id}", 404)
    return _ok(updated)


def set_status(agent_id: int, status: int) -> dict:
    """上架 / 下架（M-A-03 启禁用）。"""
    if int(status) not in (STATUS_ON, STATUS_OFF):
        return _fail("状态只能是 1（上架）或 0（下架）")
    return update_agent(agent_id, {"status": int(status)})


def delete_agent(agent_id: int) -> dict:
    """删除助手。被渠道绑定为主助手时拒绝删除，避免渠道指向空。"""
    current = agent_store.get_agent(agent_id)
    if current is None:
        return _fail(f"助手不存在：{agent_id}", 404)

    used = agent_store.count_channels_using(agent_id)
    if used:
        return _fail(f"该助手被 {used} 个渠道绑定为主助手，请先改绑再删除", 409)

    if not agent_store.delete_agent(agent_id):
        return _fail(f"删除失败：助手不存在：{agent_id}", 404)
    return _ok({"agentId": int(agent_id), "agentCode": current["agentCode"], "deleted": True})
