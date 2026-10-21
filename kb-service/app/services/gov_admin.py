"""导办事项管理、推荐与免登录票据（任务 4.5 / P-DB-01·02、M-G-01、I-03）。

* **事项同步**：``GOV_SYNC_PROVIDER=mock|http``。mock 返回内置演示事项；http 从
  一网通办事项接口拉取（Bearer Token），按「名称 + 部门」幂等 upsert。
* **检索推荐**：按名称 / 关键词标签 / 部门做轻量词面匹配，叠加办事意图判断；
  对话只在有把握时推荐，避免每个问题都挂一张无关卡片。
* **免登录票据**：登录用户在对话里点击「去办理」时签发短 TTL 一次性票据，
  大厅校验后换取身份，用户无需在外部系统再次登录。票据本身不含身份明文。
* **事项管理**：字段维护、单事项启停、生效时段、角色可见、同步导入。

校验风格与渠道 / 应用一致：收集全部错误一次返回（400），管理页可一次改全。
"""

from __future__ import annotations

import copy
import logging
import re
import secrets
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from app.config import settings
from app.db import gov_store

logger = logging.getLogger(__name__)

DEPTS: list[dict] = [
    {"deptId": 1, "deptName": "信网中心"},
    {"deptId": 2, "deptName": "教务处"},
    {"deptId": 3, "deptName": "学工部"},
    {"deptId": 4, "deptName": "财务处"},
    {"deptId": 5, "deptName": "后勤处"},
    {"deptId": 6, "deptName": "人事处"},
    {"deptId": 7, "deptName": "图书馆"},
    {"deptId": 8, "deptName": "保卫处"},
    {"deptId": 9, "deptName": "校医院"},
]
DEPT_NAMES = {int(d["deptId"]): d["deptName"] for d in DEPTS}
DEPT_IDS = set(DEPT_NAMES)

ROLE_OPTIONS = [gov_store.ALL_ROLE, *gov_store.VISIBLE_ROLES]
MAX_NAME = 255
MAX_TAGS = 20
MAX_TAG_LEN = 32
MAX_TEXT = 4000
MAX_HALL_URL = 512
MAX_SYNC_ITEMS = 5000

_UNSAFE_SCHEME = ("javascript:", "vbscript:", "data:", "file:", "blob:")
_SAFE_PREFIX = ("http://", "https://", "/", "#", "./", "../")

#: 办事意图。命中任一即认为用户在问「怎么办」，才允许无词面命中时回退常用事项
GOV_INTENT_WORDS = (
    "办理", "申请", "补办", "挂失", "预约", "材料", "流程", "手续",
    "证明", "在哪办", "怎么办", "怎么办理", "如何办理", "需要什么", "办事",
)
#: 明确浏览意图：用户想找事项入口，而不是问知识
GOV_BROWSE_WORDS = ("办事", "事项", "一网通办", "服务大厅", "导办", "办理大厅")

MOCK_ITEMS: list[dict] = [
    {
        "itemName": "校园卡补办",
        "deptName": "信网中心",
        "hallUrl": "/hall",
        "needTicket": True,
        "conditions": "校园卡丢失、损坏或无法读取的在校师生。",
        "materials": "本人身份证原件；一寸免冠照片 1 张（可现场采集）。",
        "keywordTags": ["校园卡", "一卡通", "补办", "挂失", "丢失"],
        "visibleRoles": ["全部"],
    },
    {
        "itemName": "校园网开通",
        "deptName": "信网中心",
        "hallUrl": "/hall",
        "needTicket": True,
        "conditions": "在校学生与教职工，已完成统一身份认证。",
        "materials": "学工号；宿舍楼栋与房间号。",
        "keywordTags": ["校园网", "网络", "上网", "宽带", "开通", "网费"],
        "visibleRoles": ["全部"],
    },
    {
        "itemName": "在读证明办理",
        "deptName": "教务处",
        "hallUrl": "/hall",
        "needTicket": True,
        "conditions": "在籍本科生、研究生。",
        "materials": "线上申请无需材料；出国用途如需英文版请备注。",
        "keywordTags": ["在读证明", "证明", "在读", "出国", "实习", "学籍"],
        "visibleRoles": ["本科生", "研究生"],
    },
    {
        "itemName": "中文成绩单打印",
        "deptName": "教务处",
        "hallUrl": "/hall",
        "needTicket": True,
        "conditions": "在籍学生或已毕业校友。",
        "materials": "学工号；如需盖章请选择人工窗口领取。",
        "keywordTags": ["成绩单", "成绩", "打印", "盖章", "绩点"],
        "visibleRoles": ["本科生", "研究生"],
    },
    {
        "itemName": "学生证补办",
        "deptName": "学工部",
        "hallUrl": "/hall",
        "needTicket": True,
        "conditions": "学生证遗失或损坏的全日制学生。",
        "materials": "身份证原件；院系签字的情况说明。",
        "keywordTags": ["学生证", "补办", "遗失", "火车票优惠卡"],
        "visibleRoles": ["本科生", "研究生"],
    },
    {
        "itemName": "宿舍报修",
        "deptName": "后勤处",
        "hallUrl": "/hall",
        "needTicket": True,
        "conditions": "在校住宿学生。",
        "materials": "宿舍楼栋、房间号；故障照片（可选）。",
        "keywordTags": ["报修", "宿舍", "维修", "水电", "空调", "门锁"],
        "visibleRoles": ["本科生", "研究生"],
    },
    {
        "itemName": "教职工收入证明",
        "deptName": "人事处",
        "hallUrl": "/hall",
        "needTicket": True,
        "conditions": "在职教职工。",
        "materials": "线上申请；贷款/签证用途请填写接收单位。",
        "keywordTags": ["收入证明", "在职证明", "工资", "贷款", "签证"],
        "visibleRoles": ["教职工"],
    },
    {
        "itemName": "图书馆座位预约",
        "deptName": "图书馆",
        "hallUrl": "/hall",
        "needTicket": False,
        "conditions": "持有效校园卡的在校师生。",
        "materials": "无需材料，选择馆舍、日期与时段即可。",
        "keywordTags": ["座位", "预约", "自习", "图书馆", "考研"],
        "visibleRoles": ["全部"],
    },
    {
        "itemName": "校医院就诊挂号",
        "deptName": "校医院",
        "hallUrl": "/hall",
        "needTicket": True,
        "conditions": "在校师生及离退休人员。",
        "materials": "校园卡或医保卡；既往病历（如有）。",
        "keywordTags": ["挂号", "就诊", "校医院", "医保", "看病"],
        "visibleRoles": ["全部"],
    },
]


def _ok(data) -> dict:
    return {"ok": True, "data": data, "error": None}


def _fail(message: str, status: int = 400) -> dict:
    return {"ok": False, "data": None, "error": message, "status": status}


def _now() -> datetime:
    return datetime.now()


def dept_name(dept_id: int) -> str:
    return DEPT_NAMES.get(int(dept_id or 0), f"部门#{dept_id}")


# ------------------------------------------------------------------ 校验
def _clean_tags(raw) -> tuple[list[str] | None, str | None]:
    if raw is None:
        return [], None
    if isinstance(raw, str):
        values = re.split(r"[,，、\s]+", raw)
    elif isinstance(raw, list):
        values = raw
    else:
        return None, "关键词标签必须是数组或逗号分隔字符串"
    tags: list[str] = []
    for value in values:
        tag = str(value or "").strip()
        if not tag:
            continue
        if len(tag) > MAX_TAG_LEN:
            return None, f"单个关键词不能超过 {MAX_TAG_LEN} 个字符"
        if tag not in tags:
            tags.append(tag)
    if len(tags) > MAX_TAGS:
        return None, f"关键词最多 {MAX_TAGS} 个"
    return tags, None


def _clean_roles(raw) -> tuple[list[str] | None, str | None]:
    if raw is None:
        return [gov_store.ALL_ROLE], None
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
    if not roles or gov_store.ALL_ROLE in roles:
        return [gov_store.ALL_ROLE], None
    order = {name: idx for idx, name in enumerate(ROLE_OPTIONS)}
    roles.sort(key=lambda name: order.get(name, len(order)))
    return roles, None


def _clean_url(raw) -> tuple[str | None, str | None]:
    if raw is None:
        return "", None
    if not isinstance(raw, str):
        return None, "办理地址必须是字符串"
    url = raw.strip()
    if not url:
        return "", None
    if len(url) > MAX_HALL_URL:
        return None, f"办理地址不能超过 {MAX_HALL_URL} 个字符"
    if re.search(r"[\s\u0000-\u001f\u007f]", url):
        return None, "办理地址不能包含空白或控制字符"
    compact = url.lower()
    if compact.startswith(_UNSAFE_SCHEME):
        return None, "办理地址协议不安全"
    if not compact.startswith(_SAFE_PREFIX):
        return None, "办理地址需以 http(s)://、/ 或 # 开头"
    return url, None


def _parse_dt(raw, label: str, errors: list[str]) -> datetime | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, datetime):
        return raw
    text = str(raw).strip()
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        errors.append(f"{label}格式不正确（示例：2026-10-21T09:00）")
        return None
    if value.tzinfo is not None:
        value = value.astimezone().replace(tzinfo=None)
    return value


def _as_bool(raw, default: bool = False) -> bool:
    if raw is None:
        return default
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return bool(raw)


def _validate(payload: dict, base: dict | None = None) -> tuple[dict | None, list[str]]:
    """``payload`` 为 camelCase 增量；``base`` 为已有事项（更新时），合并后整体校验。"""
    data = dict(base or {})
    for key, value in (payload or {}).items():
        if value is None:
            # 生效时段显式传 null = 清空窗口；其它字段的 null 仍视为「未传」
            if key in ("effectiveFrom", "effectiveTo"):
                data[key] = None
            continue
        data[key] = value

    errors: list[str] = []

    name = str(data.get("itemName") or "").strip()
    if not name:
        errors.append("事项名称不能为空")
    elif len(name) > MAX_NAME:
        errors.append(f"事项名称不能超过 {MAX_NAME} 个字符")

    try:
        dept_id = int(data.get("deptId"))
    except (TypeError, ValueError):
        dept_id = 0
    if dept_id not in DEPT_IDS:
        errors.append("请选择有效的主管部门")

    hall_url, err = _clean_url(data.get("hallUrl"))
    if err:
        errors.append(err)
    elif not hall_url:
        errors.append("办理地址不能为空")

    conditions = str(data.get("conditions") or "").strip()
    materials = str(data.get("materials") or "").strip()
    if len(conditions) > MAX_TEXT:
        errors.append(f"办理条件不能超过 {MAX_TEXT} 个字符")
    if len(materials) > MAX_TEXT:
        errors.append(f"所需材料不能超过 {MAX_TEXT} 个字符")

    tags, err = _clean_tags(data.get("keywordTags"))
    if err:
        errors.append(err)

    roles, err = _clean_roles(data.get("visibleRoles"))
    if err:
        errors.append(err)

    try:
        status = int(data.get("status", 1))
    except (TypeError, ValueError):
        status = -1
    if status not in (0, 1):
        errors.append("状态只能是启用或停用")

    effective_from = _parse_dt(data.get("effectiveFrom"), "生效开始时间", errors)
    effective_to = _parse_dt(data.get("effectiveTo"), "生效结束时间", errors)
    if effective_from and effective_to and effective_to <= effective_from:
        errors.append("生效结束时间必须晚于开始时间")

    if errors:
        return None, errors
    return {
        "item_name": name,
        "dept_id": dept_id,
        "hall_url": hall_url or "",
        "need_ticket": _as_bool(data.get("needTicket"), True),
        "conditions": conditions,
        "materials": materials,
        "keyword_tags": tags or [],
        "visible_roles": roles or [gov_store.ALL_ROLE],
        "status": status,
        "effective_from": effective_from,
        "effective_to": effective_to,
    }, []


# ------------------------------------------------------------------ 展示
def _effective_state(item: dict, now: datetime | None = None) -> str:
    moment = now or _now()
    if int(item.get("status", 0)) != 1:
        return "disabled"
    start = gov_store._as_dt(item.get("effectiveFrom"))
    end = gov_store._as_dt(item.get("effectiveTo"))
    if start and moment < start:
        return "scheduled"
    if end and moment > end:
        return "expired"
    return "active"


EFFECTIVE_LABELS = {
    "active": "生效中",
    "scheduled": "未生效",
    "expired": "已过期",
    "disabled": "已停用",
}


def _is_active(item: dict, now: datetime | None = None) -> bool:
    return _effective_state(item, now) == "active"


def _match_role(item: dict, role: str) -> bool:
    roles = item.get("visibleRoles") or [gov_store.ALL_ROLE]
    return gov_store.ALL_ROLE in roles or role in roles


def _summary(item: dict) -> str:
    for raw in (item.get("conditions"), item.get("materials")):
        text = str(raw or "").strip()
        if not text:
            continue
        first = re.split(r"[\n。；;]", text)[0].strip()
        if first:
            return first[:80]
    return f"前往{dept_name(item.get('deptId'))}在线办理"


def _card(item: dict, *, match_reason: str = "") -> dict:
    return {
        "itemId": int(item["itemId"]),
        "itemName": item.get("itemName") or "",
        "deptId": int(item.get("deptId") or 0),
        "deptName": dept_name(item.get("deptId")),
        "summary": _summary(item),
        "needTicket": bool(item.get("needTicket")),
        "hallUrl": item.get("hallUrl") or "",
        "keywordTags": list(item.get("keywordTags") or []),
        "matchReason": match_reason,
    }


def _decorate(item: dict) -> dict:
    state = _effective_state(item)
    out = dict(item)
    out["deptName"] = dept_name(item.get("deptId"))
    out["effectiveState"] = state
    out["effectiveLabel"] = EFFECTIVE_LABELS[state]
    out["summary"] = _summary(item)
    return out


# ------------------------------------------------------------------ 管理端
def list_admin(q: str | None = None, status: str | None = None) -> dict:
    items = [_decorate(item) for item in gov_store.list_items()]
    summary = {
        "total": len(items),
        "enabled": sum(1 for item in items if int(item["status"]) == 1),
        "active": sum(1 for item in items if item["effectiveState"] == "active"),
        "needTicket": sum(1 for item in items if item["needTicket"]),
    }

    keyword = str(q or "").strip().lower()
    if keyword:
        items = [
            item
            for item in items
            if keyword in str(item.get("itemName") or "").lower()
            or keyword in str(item.get("deptName") or "").lower()
            or keyword in str(item.get("conditions") or "").lower()
            or keyword in str(item.get("materials") or "").lower()
            or any(keyword in str(tag).lower() for tag in item.get("keywordTags") or [])
        ]

    state = str(status or "").strip()
    if state in ("0", "1"):
        items = [item for item in items if int(item["status"]) == int(state)]
    elif state in EFFECTIVE_LABELS:
        items = [item for item in items if item["effectiveState"] == state]

    items.sort(key=lambda item: (-int(item["status"]), item["itemId"]))
    return _ok({
        "items": items,
        "total": len(items),
        "summary": summary,
        "deptOptions": DEPTS,
        "roleOptions": ROLE_OPTIONS,
        "sync": sync_meta(),
        "ticketTtlSeconds": settings.gov_ticket_ttl_seconds,
    })


def get_admin_item(item_id: int) -> dict:
    item = gov_store.get_item(int(item_id))
    if item is None:
        return _fail("事项不存在", 404)
    return _ok(_decorate(item))


def _duplicate_error(fields: dict, exclude_id: int | None = None) -> str | None:
    existing = gov_store.find_duplicate(
        fields["item_name"], fields["dept_id"], exclude_id=exclude_id
    )
    if existing is not None:
        return f"同一部门下已存在事项「{fields['item_name']}」"
    return None


def create_item(payload: dict) -> dict:
    fields, errors = _validate(payload)
    if errors:
        return _fail("；".join(errors))
    duplicate = _duplicate_error(fields)
    if duplicate:
        return _fail(duplicate)
    item_id = gov_store.create_item(**fields)
    return _ok(_decorate(gov_store.get_item(item_id) or {}))


def update_item(item_id: int, payload: dict) -> dict:
    existing = gov_store.get_item(int(item_id))
    if existing is None:
        return _fail("事项不存在", 404)
    fields, errors = _validate(payload, existing)
    if errors:
        return _fail("；".join(errors))
    duplicate = _duplicate_error(fields, exclude_id=int(item_id))
    if duplicate:
        return _fail(duplicate)
    gov_store.update_item(int(item_id), **fields)
    return _ok(_decorate(gov_store.get_item(int(item_id)) or {}))


def delete_item(item_id: int) -> dict:
    if not gov_store.delete_item(int(item_id)):
        return _fail("事项不存在", 404)
    return _ok({"deleted": True, "itemId": int(item_id)})


# ------------------------------------------------------------------ 公开检索
def _score(item: dict, question: str) -> tuple[int, str]:
    text = question.strip().lower()
    score = 0
    reasons: list[str] = []
    name = str(item.get("itemName") or "").strip().lower()
    if name and name in text:
        score += 80
        reasons.append("事项名称")
    for tag in item.get("keywordTags") or []:
        tag_text = str(tag).strip().lower()
        if len(tag_text) >= 2 and tag_text in text:
            score += 30
            reasons.append(str(tag))
    department = dept_name(item.get("deptId")).lower()
    if department and department in text:
        score += 15
        if not reasons:
            reasons.append("主管部门")
    return score, "、".join(dict.fromkeys(reasons[:4]))


def public_items(role: str | None, q: str | None = None, limit: int = 20) -> dict:
    current = (role or "").strip()
    if current not in gov_store.VISIBLE_ROLES:
        current = "访客"
    if not settings.gov_enabled:
        return _ok({"role": current, "total": 0, "items": []})

    now = _now()
    candidates = [
        item
        for item in gov_store.list_items()
        if _is_active(item, now) and _match_role(item, current)
    ]
    question = str(q or "").strip()
    if question:
        scored = []
        for item in candidates:
            score, reason = _score(item, question)
            if score > 0:
                scored.append((score, item, reason))
        scored.sort(key=lambda row: (-row[0], row[1]["itemId"]))
        cards = [_card(item, match_reason=reason) for _, item, reason in scored]
    else:
        candidates.sort(key=lambda item: str(item.get("updatedAt") or ""), reverse=True)
        cards = [_card(item) for item in candidates]
    cards = cards[: max(1, min(int(limit or 20), 50))]
    return _ok({"role": current, "total": len(cards), "items": cards})


def recommend(question: str, role: str | None, limit: int | None = None) -> list[dict]:
    """对话推荐：无把握时不返回，避免无关卡片干扰回答。"""
    if not settings.gov_enabled:
        return []
    text = str(question or "").strip()
    if not text:
        return []
    cap = max(1, min(int(limit or settings.gov_recommend_limit), 5))
    current = (role or "").strip()
    if current not in gov_store.VISIBLE_ROLES:
        current = "访客"

    now = _now()
    candidates = [
        item
        for item in gov_store.list_items()
        if _is_active(item, now) and _match_role(item, current)
    ]
    scored: list[tuple[int, dict, str]] = []
    for item in candidates:
        score, reason = _score(item, text)
        if score > 0:
            scored.append((score, item, reason))
    scored.sort(key=lambda row: (-row[0], row[1]["itemId"]))

    if scored:
        return [_card(item, match_reason=reason) for _, item, reason in scored[:cap]]

    if any(word in text for word in GOV_BROWSE_WORDS) and any(
        word in text for word in GOV_INTENT_WORDS
    ):
        candidates.sort(key=lambda item: str(item.get("updatedAt") or ""), reverse=True)
        return [_card(item, match_reason="常用事项") for item in candidates[: min(cap, 3)]]
    return []


# ------------------------------------------------------------------ 票据
def _with_ticket(url: str, ticket: str) -> str:
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.append(("ticket", ticket))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def issue_ticket(item_id: int, principal, role_hint: str | None = None) -> dict:
    item = gov_store.get_item(int(item_id))
    if item is None:
        return _fail("事项不存在", 404)
    if not _is_active(item):
        return _fail("该事项当前已停用或不在生效时段", 409)

    authenticated = bool(getattr(principal, "authenticated", False))
    role = (getattr(principal, "role", "") if authenticated else (role_hint or "访客")) or "访客"
    if role not in gov_store.VISIBLE_ROLES:
        role = "访客"
    if not _match_role(item, role):
        return _fail("当前身份无权办理该事项", 403)

    hall_url = str(item.get("hallUrl") or "").strip()
    if not hall_url:
        return _fail("该事项尚未配置办理入口，请联系主管部门", 409)

    if not item.get("needTicket"):
        return _ok({
            "needTicket": False,
            "ticket": None,
            "jumpUrl": hall_url,
            "expiresIn": 0,
            "expiresAt": None,
        })

    if settings.auth_enabled and not authenticated:
        return _fail("办理事项需要登录统一身份认证，请登录后重试", 401)

    if authenticated:
        user_no = str(getattr(principal, "user_no", "") or "")
        user_name = user_no
    else:
        user_no = "demo"
        user_name = "演示用户"

    ttl = max(30, min(int(settings.gov_ticket_ttl_seconds), 300))
    issued_at = _now()
    expires_at = issued_at + timedelta(seconds=ttl)
    ticket = "GT" + secrets.token_urlsafe(24)
    gov_store.create_ticket({
        "ticket_id": ticket,
        "item_id": int(item["itemId"]),
        "user_no": user_no,
        "user_name": user_name,
        "role": role,
        "issued_at": issued_at,
        "expires_at": expires_at,
    })
    try:
        gov_store.purge_tickets(days=1)
    except Exception:  # noqa: BLE001 - 清理失败不影响本次签发
        logger.warning("导办票据清理失败", exc_info=True)

    return _ok({
        "needTicket": True,
        "ticket": ticket,
        "jumpUrl": _with_ticket(hall_url, ticket),
        "expiresIn": ttl,
        "expiresAt": expires_at.isoformat(timespec="seconds"),
        "item": _card(item),
    })


def verify_ticket(ticket: str) -> dict:
    value = str(ticket or "").strip()
    if not value or len(value) > 80 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return _fail("票据格式不正确", 400)

    record = gov_store.get_ticket(value)
    if record is None:
        return _fail("票据无效或已使用", 404)
    if record.get("usedAt"):
        return _fail("票据已使用，请返回对话重新发起办理", 409)

    now = _now()
    expires_at = gov_store._as_dt(record.get("expiresAt"))
    if expires_at is None or expires_at <= now:
        return _fail("票据已过期，请返回对话重新发起办理", 410)

    item = gov_store.get_item(int(record.get("itemId") or 0))
    if item is None:
        return _fail("票据对应的事项不存在", 404)
    if not _is_active(item, now):
        return _fail("该事项已停用或不在生效时段", 409)
    if not _match_role(item, str(record.get("role") or "访客")):
        return _fail("当前票据身份无权办理该事项", 403)

    if not gov_store.consume_ticket(value, now):
        return _fail("票据已使用，请返回对话重新发起办理", 409)

    return _ok({
        "valid": True,
        "usedAt": now.isoformat(timespec="seconds"),
        "issuedAt": record.get("issuedAt"),
        "expiresAt": record.get("expiresAt"),
        "identity": {
            "userNo": record.get("userNo") or "",
            "userName": record.get("userName") or "",
            "role": record.get("role") or "访客",
        },
        "item": {
            "itemId": int(item["itemId"]),
            "itemName": item.get("itemName") or "",
            "deptName": dept_name(item.get("deptId")),
            "hallUrl": item.get("hallUrl") or "",
            "conditions": item.get("conditions") or "",
            "materials": item.get("materials") or "",
        },
    })


# ------------------------------------------------------------------ 同步
def sync_meta() -> dict:
    return {
        "provider": settings.gov_sync_provider,
        "urlConfigured": bool(str(settings.gov_sync_url or "").strip()),
        "tokenConfigured": bool(str(settings.gov_sync_token or "").strip()),
        "mockCount": len(MOCK_ITEMS),
    }


def _extract_items(body) -> tuple[list | None, str | None]:
    data = body
    if isinstance(data, dict):
        for key in ("items", "data", "list"):
            value = data.get(key)
            if isinstance(value, list):
                data = value
                break
            if isinstance(value, dict):
                nested = value.get("items")
                if isinstance(nested, list):
                    data = nested
                    break
    if not isinstance(data, list):
        return None, "同步接口返回格式不正确（期望事项数组）"
    if len(data) > MAX_SYNC_ITEMS:
        return None, f"单次同步事项不能超过 {MAX_SYNC_ITEMS} 条"
    return data, None


def _fetch_http() -> tuple[list | None, str | None]:
    url = str(settings.gov_sync_url or "").strip()
    if not url:
        return None, "未配置 GOV_SYNC_URL，无法从一网通办同步事项"
    if not url.lower().startswith(("http://", "https://")):
        return None, "GOV_SYNC_URL 必须是 http(s) 地址"
    headers = {"Accept": "application/json"}
    token = str(settings.gov_sync_token or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = httpx.get(
            url,
            headers=headers,
            timeout=settings.gov_sync_timeout,
            follow_redirects=True,
        )
    except httpx.TimeoutException:
        return None, "一网通办事项接口超时"
    except httpx.HTTPError as exc:
        return None, f"一网通办事项接口不可达（{type(exc).__name__}）"
    if resp.status_code >= 400:
        return None, f"一网通办事项接口返回 HTTP {resp.status_code}"
    try:
        body = resp.json()
    except ValueError:
        return None, "一网通办事项接口返回的不是 JSON"
    return _extract_items(body)


def _normalize_sync_item(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("itemName") or raw.get("item_name") or raw.get("name") or "").strip()
    if not name or len(name) > MAX_NAME:
        return None
    dept_id = raw.get("deptId", raw.get("dept_id"))
    if dept_id in (None, ""):
        dept_label = str(raw.get("deptName") or raw.get("dept_name") or "").strip()
        dept_id = next(
            (int(d["deptId"]) for d in DEPTS if d["deptName"] == dept_label), 0
        )
    try:
        dept_id = int(dept_id)
    except (TypeError, ValueError):
        return None
    if dept_id not in DEPT_IDS:
        return None
    hall_url, err = _clean_url(raw.get("hallUrl", raw.get("hall_url")))
    if err or not hall_url:
        hall_url = "/hall"
    tags, err = _clean_tags(raw.get("keywordTags", raw.get("keyword_tags")))
    if err:
        tags = []
    roles, err = _clean_roles(raw.get("visibleRoles", raw.get("visible_roles")))
    if err:
        roles = [gov_store.ALL_ROLE]
    return {
        "item_name": name,
        "dept_id": dept_id,
        "hall_url": hall_url,
        "need_ticket": _as_bool(raw.get("needTicket", raw.get("need_ticket")), True),
        "conditions": str(raw.get("conditions") or "")[:MAX_TEXT],
        "materials": str(raw.get("materials") or "")[:MAX_TEXT],
        "keyword_tags": tags or [],
        "visible_roles": roles or [gov_store.ALL_ROLE],
    }


def sync_items() -> dict:
    provider = settings.gov_sync_provider
    if provider == "mock":
        raw_items = copy.deepcopy(MOCK_ITEMS)
    else:
        raw_items, error = _fetch_http()
        if error:
            return _fail(error, 502)
        assert raw_items is not None

    normalized: list[dict] = []
    for raw in raw_items:
        item = _normalize_sync_item(raw)
        if item is not None:
            normalized.append(item)

    counts = gov_store.upsert_items(normalized)
    return _ok({
        "provider": provider,
        "total": len(raw_items),
        "created": counts["created"],
        "updated": counts["updated"],
        "skipped": len(raw_items) - len(normalized),
        "imported": len(normalized),
    })
