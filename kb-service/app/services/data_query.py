"""问数服务（任务 4.3 / P-DQ-01·02·03）。

把「我的课表」「我的成绩」「一卡通余额/流水」这类**个人数据查询**从知识问答里
识别出来，完成身份映射、权限校验、参数抽取与数据源调用，最后输出统一的结构化
卡片，由门户渲染成表格/指标，而不是让大模型凭空生成数字。

数据源：

* ``DQ_PROVIDER=mock``：内置确定性数据，联调与验收用；
* ``DQ_PROVIDER=http``：调用教务 / 一卡通接口，契约见 :func:`_fetch_schedule`
  等函数的文档。外部接口就绪后只改环境变量，前端与接口层零改动。

**安全边界**：

* 学工号只从登录 token 取；演示模式（``AUTH_ENABLED=false``）固定映射演示学号，
  前端不能通过请求参数指定他人学号；
* 教务数据仅本科生 / 研究生可查，一卡通仅在校师生可查；
* 上游令牌只进请求头，错误信息不回显响应体，卡片不包含任何凭据。
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

INTENTS = ("schedule", "grades", "card")

INTENT_LABELS = {
    "schedule": "课表查询",
    "grades": "成绩查询",
    "card": "一卡通余额 / 流水",
}

ACADEMIC_ROLES = ("本科生", "研究生")
CARD_ROLES = ("本科生", "研究生", "教职工", "离退休人员")

INTENT_WORDS: dict[str, tuple[str, ...]] = {
    "card": (
        "一卡通", "饭卡", "校园卡余额", "校园卡消费", "卡余额", "余额",
        "消费流水", "消费记录", "刷卡记录", "充值记录",
    ),
    "grades": ("成绩", "绩点", "gpa", "分数", "挂科", "考试成绩", "成绩单"),
    "schedule": (
        "课表", "课程表", "有课", "有什么课", "上课", "几节课", "本周课",
        "明天的课", "今天有什么课", "星期五有什么课",
    ),
}

_DAY_ALIASES = {"天": "日"}

_PROFILES: dict[str, dict[str, str]] = {
    "20230001": {
        "name": "李知远",
        "major": "计算机科学与技术",
        "grade": "2023级",
    },
    "20240001": {
        "name": "周雨桐",
        "major": "软件工程",
        "grade": "2024级",
    },
    "T10001": {
        "name": "陈老师",
        "major": "信息网络中心",
        "grade": "教职工",
    },
}

_UNDERGRAD_SCHEDULE = [
    {"day": "周一", "time": "08:00-09:40", "course": "高等数学 A（二）",
     "place": "教一 101", "teacher": "张明", "weeks": "1-16", "credit": 4.0},
    {"day": "周一", "time": "10:00-11:40", "course": "大学英语（四）",
     "place": "外语楼 302", "teacher": "王静", "weeks": "1-16", "credit": 2.0},
    {"day": "周二", "time": "14:00-15:40", "course": "数据结构",
     "place": "计算机楼 205", "teacher": "李强", "weeks": "1-16", "credit": 3.5},
    {"day": "周三", "time": "08:00-09:40", "course": "线性代数",
     "place": "教二 208", "teacher": "赵敏", "weeks": "1-16", "credit": 3.0},
    {"day": "周四", "time": "10:00-11:40", "course": "体育（羽毛球）",
     "place": "体育馆 3 号场", "teacher": "陈晨", "weeks": "1-16", "credit": 1.0},
    {"day": "周五", "time": "14:00-16:30", "course": "数据结构实验",
     "place": "实验楼 401", "teacher": "李强", "weeks": "2-16", "credit": 1.5},
]

_GRAD_SCHEDULE = [
    {"day": "周一", "time": "10:00-11:40", "course": "高级算法设计",
     "place": "计算机楼 406", "teacher": "周涛", "weeks": "1-16", "credit": 3.0},
    {"day": "周二", "time": "08:00-09:40", "course": "机器学习",
     "place": "人工智能楼 201", "teacher": "林雪", "weeks": "1-16", "credit": 3.5},
    {"day": "周三", "time": "14:00-15:40", "course": "学术英语写作",
     "place": "外语楼 501", "teacher": "刘洋", "weeks": "1-16", "credit": 2.0},
    {"day": "周五", "time": "09:00-11:30", "course": "科研实践",
     "place": "计算机楼 512", "teacher": "周涛", "weeks": "3-16", "credit": 2.0},
]

_UNDERGRAD_GRADES = [
    {"course": "高等数学 A（二）", "type": "必修", "credit": 4.0, "score": 92, "gpa": 4.2},
    {"course": "大学英语（四）", "type": "必修", "credit": 2.0, "score": 86, "gpa": 3.6},
    {"course": "数据结构", "type": "必修", "credit": 3.5, "score": 88, "gpa": 3.8},
    {"course": "线性代数", "type": "必修", "credit": 3.0, "score": 90, "gpa": 4.0},
    {"course": "体育（羽毛球）", "type": "必修", "credit": 1.0, "score": 84, "gpa": 3.4},
    {"course": "数据结构实验", "type": "选修", "credit": 1.5, "score": 95, "gpa": 4.5},
]

_GRAD_GRADES = [
    {"course": "高级算法设计", "type": "学位课", "credit": 3.0, "score": 91, "gpa": 4.1},
    {"course": "机器学习", "type": "学位课", "credit": 3.5, "score": 89, "gpa": 3.9},
    {"course": "学术英语写作", "type": "必修", "credit": 2.0, "score": 87, "gpa": 3.7},
    {"course": "科研实践", "type": "必修", "credit": 2.0, "score": 93, "gpa": 4.3},
]

# (daysAgo, time, place, type, amount)
_MOCK_TRANSACTIONS = [
    (0, "12:06", "第一食堂 · 二楼", "消费", -12.50),
    (1, "18:22", "西区超市", "消费", -23.80),
    (2, "09:12", "校园卡自助充值", "充值", 200.00),
    (4, "11:48", "第二食堂 · 一楼", "消费", -9.00),
    (6, "20:15", "洗衣房 A 栋", "消费", -4.00),
    (9, "07:55", "校车乘车", "消费", -2.00),
    (13, "16:30", "图书馆打印", "消费", -6.40),
    (18, "12:20", "第一食堂 · 二楼", "消费", -15.60),
    (25, "10:05", "校园卡自助充值", "充值", 100.00),
    (32, "19:42", "东区超市", "消费", -18.90),
    (45, "08:30", "校医院挂号", "消费", -10.00),
    (58, "13:10", "第一食堂 · 一楼", "消费", -11.20),
]


class DataQueryError(Exception):
    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _now() -> datetime:
    return datetime.now()


def _ok(data: dict) -> dict:
    return {"ok": True, "data": data, "error": None}


def _fail(error: str, status: int, intent: str | None = None) -> dict:
    return {"ok": False, "data": None, "error": error, "status": status, "intent": intent}


# ------------------------------------------------------------------ 意图与参数
def detect_intent(question: str) -> str | None:
    text = str(question or "").strip().lower()
    if not text:
        return None
    for intent in ("card", "grades", "schedule"):
        if any(word in text for word in INTENT_WORDS[intent]):
            return intent
    return None


def _normalize_role(role: str | None) -> str:
    value = str(role or "").strip()
    return "离退休人员" if value == "离退休" else value


def _extract_params(question: str, intent: str) -> dict:
    text = str(question or "")
    params: dict[str, Any] = {}
    if intent == "schedule":
        match = re.search(r"第\s*(\d{1,2})\s*周", text)
        if match:
            params["week"] = max(1, min(int(match.group(1)), 30))
        match = re.search(r"(?:周|星期)([一二三四五六日天])", text)
        if match:
            day = match.group(1)
            params["day"] = "周" + _DAY_ALIASES.get(day, day)
    elif intent == "grades":
        match = re.search(
            r"(\d{4}\s*-\s*\d{4}\s*学年\s*第?\s*[12一二]\s*学期)", text
        )
        if match:
            params["semester"] = re.sub(r"\s+", "", match.group(1))
    elif intent == "card":
        match = re.search(r"(?:最近|近|过去)\s*(\d{1,3})\s*天", text)
        if match:
            params["days"] = max(1, min(int(match.group(1)), 90))
        elif "本月" in text:
            params["days"] = max(1, _now().day)
        match = re.search(r"(\d{1,2})\s*条", text)
        if match:
            params["limit"] = max(1, min(int(match.group(1)), settings.dq_max_rows))
    return params


def _semester(now: datetime | None = None) -> tuple[str, int]:
    """返回 (学期标签, 当前教学周)。以秋季 9 月 / 春季 2 月下旬开学估算。"""
    moment = now or _now()
    if moment.month >= 9:
        start_year, term = moment.year, 1
    elif moment.month <= 1:
        start_year, term = moment.year - 1, 1
    else:
        start_year, term = moment.year - 1, 2
    label = f"{start_year}-{start_year + 1} 学年{'第一' if term == 1 else '第二'}学期"
    base = date(start_year, 9, 1) if term == 1 else date(start_year + 1, 2, 20)
    monday = base + timedelta(days=(7 - base.weekday()) % 7)
    week = max(1, min(30, (moment.date() - monday).days // 7 + 1))
    return label, week


def _week_in(weeks: str, week: int) -> bool:
    text = str(weeks or "").strip()
    if not text:
        return True
    for part in text.split(","):
        part = part.strip()
        if "-" in part:
            left, _, right = part.partition("-")
            try:
                if int(left) <= week <= int(right):
                    return True
            except ValueError:
                continue
        else:
            try:
                if int(part) == week:
                    return True
            except ValueError:
                continue
    return False


# ------------------------------------------------------------------ 身份
def _profile(user_no: str, role: str) -> dict[str, str]:
    known = _PROFILES.get(user_no)
    if known:
        return {"userNo": user_no, **known}
    return {
        "userNo": user_no,
        "name": ("老师" if role == "教职工" else "同学") + user_no[-4:],
        "major": "—",
        "grade": "—",
    }


def _identity(principal, role_hint: str | None, intent: str) -> tuple[dict | None, dict | None]:
    authenticated = bool(getattr(principal, "authenticated", False))
    if authenticated:
        role = _normalize_role(getattr(principal, "role", ""))
        user_no = str(getattr(principal, "user_no", "") or "").strip()
    else:
        if settings.auth_enabled:
            return None, _fail("请先登录统一身份认证后再查询个人数据", 401, intent)
        role = _normalize_role(role_hint or "访客")
        user_no = "20230001"

    allowed = ACADEMIC_ROLES if intent in ("schedule", "grades") else CARD_ROLES
    if role not in allowed:
        scope = "教务数据" if intent in ("schedule", "grades") else "一卡通数据"
        return None, _fail(f"当前身份「{role or '访客'}」无权查询{scope}", 403, intent)

    pattern = r"\d{8}" if intent in ("schedule", "grades") else r"(?:\d{8}|T\d{4,})"
    if not re.fullmatch(pattern, user_no):
        return None, _fail(
            f"学工号「{user_no or '未绑定'}」未在教务 / 一卡通系统完成映射，无法查询",
            403,
            intent,
        )
    return {"role": role, "authenticated": authenticated, **_profile(user_no, role)}, None


# ------------------------------------------------------------------ 通用卡片
def _money(value: float) -> str:
    return f"¥{float(value):,.2f}"


def _card_base(
    intent: str,
    identity: dict,
    *,
    title: str,
    subtitle: str,
    summary: list[dict],
    columns: list[dict],
    rows: list[dict],
    source: str,
    footnote: str,
) -> dict:
    return {
        "intent": intent,
        "type": intent,
        "title": title,
        "subtitle": subtitle,
        "identity": {
            "userNo": identity["userNo"],
            "userName": identity["name"],
            "role": identity["role"],
        },
        "summary": summary,
        "columns": columns,
        "rows": rows,
        "empty": not rows,
        "source": source,
        "generatedAt": _now().isoformat(timespec="seconds"),
        "footnote": footnote,
    }


def _schedule_card(
    identity: dict, params: dict, raw_rows: list[dict], source: str
) -> dict:
    semester, current_week = _semester()
    week = int(params.get("week") or current_week)
    day = str(params.get("day") or "")
    rows = []
    for raw in raw_rows:
        if day and raw.get("day") != day:
            continue
        if not _week_in(str(raw.get("weeks") or ""), week):
            continue
        rows.append({
            "day": raw.get("day") or "",
            "time": raw.get("time") or "",
            "course": raw.get("course") or "",
            "place": raw.get("place") or "",
            "teacher": raw.get("teacher") or "",
            "weeks": raw.get("weeks") or "",
        })
    rows.sort(key=lambda row: (
        ["周一", "周二", "周三", "周四", "周五", "周六", "周日"].index(row["day"])
        if row["day"] in ["周一", "周二", "周三", "周四", "周五", "周六", "周日"] else 9,
        row["time"],
    ))
    total_credit = sum(
        float(raw.get("credit") or 0)
        for raw in raw_rows
        if _week_in(str(raw.get("weeks") or ""), week)
        and (not day or raw.get("day") == day)
    )
    days = {row["day"] for row in rows}
    title = f"{day}课表" if day else "本周课表"
    subtitle = f"{semester} · 第 {week} 周"
    return _card_base(
        "schedule", identity, title=title, subtitle=subtitle,
        summary=[
            {"label": "课程", "value": f"{len(rows)} 门"},
            {"label": "总学分", "value": f"{total_credit:g}"},
            {"label": "上课天数", "value": f"{len(days)} 天"},
        ],
        columns=[
            {"key": "day", "label": "星期"},
            {"key": "time", "label": "时间"},
            {"key": "course", "label": "课程"},
            {"key": "place", "label": "地点"},
            {"key": "teacher", "label": "教师"},
        ],
        rows=rows,
        source=source,
        footnote="数据来源：教务系统 · 仅显示本人课表",
    )


def _grades_card(
    identity: dict, params: dict, raw_rows: list[dict], source: str
) -> dict:
    semester = str(params.get("semester") or _semester()[0])
    rows = [{
        "course": raw.get("course") or "",
        "type": raw.get("type") or "",
        "credit": f"{float(raw.get('credit') or 0):g}",
        "score": int(raw.get("score") or 0),
        "gpa": f"{float(raw.get('gpa') or 0):.1f}",
    } for raw in raw_rows]
    credit_sum = sum(float(raw.get("credit") or 0) for raw in raw_rows)
    score_avg = (
        sum(float(raw.get("score") or 0) * float(raw.get("credit") or 0)
            for raw in raw_rows) / credit_sum
        if credit_sum else 0
    )
    gpa_avg = (
        sum(float(raw.get("gpa") or 0) * float(raw.get("credit") or 0)
            for raw in raw_rows) / credit_sum
        if credit_sum else 0
    )
    return _card_base(
        "grades", identity, title="我的成绩单", subtitle=semester,
        summary=[
            {"label": "课程", "value": f"{len(rows)} 门"},
            {"label": "总学分", "value": f"{credit_sum:g}"},
            {"label": "加权平均", "value": f"{score_avg:.1f}"},
            {"label": "平均绩点", "value": f"{gpa_avg:.2f}"},
        ],
        columns=[
            {"key": "course", "label": "课程"},
            {"key": "type", "label": "类型"},
            {"key": "credit", "label": "学分"},
            {"key": "score", "label": "成绩"},
            {"key": "gpa", "label": "绩点"},
        ],
        rows=rows,
        source=source,
        footnote="数据来源：教务系统 · 成绩以教务系统正式记录为准",
    )


def _card_card(
    identity: dict, params: dict, payload: dict, source: str
) -> dict:
    days = max(1, min(int(params.get("days") or 30), 90))
    limit = max(1, min(int(params.get("limit") or 10), settings.dq_max_rows))
    balance = float(payload.get("balance") or 0)
    raw_rows = [raw for raw in payload.get("transactions") or [] if isinstance(raw, dict)]
    raw_rows = raw_rows[:limit]
    rows = []
    for raw in raw_rows:
        amount = float(raw.get("amount") or 0)
        rows.append({
            "time": raw.get("time") or "",
            "place": raw.get("place") or "",
            "type": raw.get("type") or ("充值" if amount > 0 else "消费"),
            "amount": ("+" if amount > 0 else "-") + _money(abs(amount)),
        })
    spend = abs(sum(float(raw.get("amount") or 0) for raw in raw_rows
                   if float(raw.get("amount") or 0) < 0))
    recharge = sum(float(raw.get("amount") or 0) for raw in raw_rows
                   if float(raw.get("amount") or 0) > 0)
    return _card_base(
        "card", identity, title="一卡通账户",
        subtitle=f"最近 {days} 天 · 共 {len(rows)} 笔流水",
        summary=[
            {"label": "可用余额", "value": _money(balance), "emphasis": True},
            {"label": f"近 {days} 天消费", "value": _money(spend)},
            {"label": f"近 {days} 天充值", "value": _money(recharge)},
        ],
        columns=[
            {"key": "time", "label": "时间"},
            {"key": "place", "label": "地点"},
            {"key": "type", "label": "类型"},
            {"key": "amount", "label": "金额"},
        ],
        rows=rows,
        source=source,
        footnote="数据来源：一卡通平台 · 金额以财务结算为准",
    )


# ------------------------------------------------------------------ mock 数据源
def _mock_schedule(identity: dict, params: dict) -> list[dict]:
    source = _GRAD_SCHEDULE if identity["role"] == "研究生" else _UNDERGRAD_SCHEDULE
    return [dict(item) for item in source]


def _mock_grades(identity: dict, params: dict) -> list[dict]:
    source = _GRAD_GRADES if identity["role"] == "研究生" else _UNDERGRAD_GRADES
    return [dict(item) for item in source]


def _mock_card(identity: dict, params: dict) -> dict:
    days = max(1, min(int(params.get("days") or 30), 90))
    limit = max(1, min(int(params.get("limit") or 10), settings.dq_max_rows))
    now = _now()
    rows = []
    for days_ago, clock, place, kind, amount in _MOCK_TRANSACTIONS:
        if days_ago >= days:
            continue
        moment = now - timedelta(days=days_ago)
        rows.append({
            "time": f"{moment.strftime('%m-%d')} {clock}",
            "place": place,
            "type": kind,
            "amount": amount,
        })
        if len(rows) >= limit:
            break
    balance = 152.30 if identity["role"] == "教职工" else 86.50
    return {"balance": balance, "transactions": rows}


# ------------------------------------------------------------------ http 数据源
def _http_json(url: str, token: str, params: dict) -> Any:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = httpx.get(
            url, headers=headers, params=params,
            timeout=settings.dq_timeout, follow_redirects=True,
        )
    except httpx.TimeoutException as exc:
        raise DataQueryError("外部数据接口超时，请稍后重试", 502) from exc
    except httpx.HTTPError as exc:
        raise DataQueryError("外部数据接口不可达，请稍后重试", 502) from exc
    if resp.status_code >= 400:
        raise DataQueryError(f"外部数据接口返回 HTTP {resp.status_code}", 502)
    try:
        return resp.json()
    except ValueError as exc:
        raise DataQueryError("外部数据接口返回的不是 JSON", 502) from exc


def _extract_items(body: Any, key: str = "items") -> list[dict]:
    data = body
    if isinstance(data, dict):
        for name in (key, "items", "list", "records"):
            value = data.get(name)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                nested = value.get("items") or value.get("list")
                if isinstance(nested, list):
                    return [item for item in nested if isinstance(item, dict)]
        nested = data.get("data")
        if isinstance(nested, (dict, list)):
            return _extract_items(nested, key)
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _fetch_schedule(identity: dict, params: dict) -> list[dict]:
    """``GET {DQ_JW_BASE_URL}/students/{userNo}/schedule?week=``。

    期望返回 ``{"items": [{day,time,course,place,teacher,weeks,credit}]}``；
    也兼容直接返回数组。
    """
    base = str(settings.dq_jw_base_url or "").strip().rstrip("/")
    if not base:
        raise DataQueryError("未配置 DQ_JW_BASE_URL，无法查询教务数据", 503)
    week = int(params.get("week") or _semester()[1])
    body = _http_json(
        f"{base}/students/{quote(identity['userNo'])}/schedule",
        settings.dq_jw_token, {"week": week},
    )
    return _extract_items(body)


def _fetch_grades(identity: dict, params: dict) -> list[dict]:
    """``GET {DQ_JW_BASE_URL}/students/{userNo}/grades?semester=``。"""
    base = str(settings.dq_jw_base_url or "").strip().rstrip("/")
    if not base:
        raise DataQueryError("未配置 DQ_JW_BASE_URL，无法查询教务数据", 503)
    semester = str(params.get("semester") or _semester()[0])
    body = _http_json(
        f"{base}/students/{quote(identity['userNo'])}/grades",
        settings.dq_jw_token, {"semester": semester},
    )
    return _extract_items(body)


def _fetch_card(identity: dict, params: dict) -> dict:
    """``GET {DQ_CARD_BASE_URL}/accounts/{userNo}/card?days=&limit=``。

    期望返回 ``{"balance": 86.5, "transactions": [{time,place,type,amount}]}``。
    """
    base = str(settings.dq_card_base_url or "").strip().rstrip("/")
    if not base:
        raise DataQueryError("未配置 DQ_CARD_BASE_URL，无法查询一卡通数据", 503)
    days = max(1, min(int(params.get("days") or 30), 90))
    limit = max(1, min(int(params.get("limit") or 10), settings.dq_max_rows))
    body = _http_json(
        f"{base}/accounts/{quote(identity['userNo'])}/card",
        settings.dq_card_token, {"days": days, "limit": limit},
    )
    data = body.get("data") if isinstance(body, dict) and "balance" not in body else body
    if not isinstance(data, dict):
        raise DataQueryError("一卡通接口返回格式不正确", 502)
    return {
        "balance": data.get("balance"),
        "transactions": _extract_items(data, "transactions"),
    }


# ------------------------------------------------------------------ 对外入口
def capabilities() -> dict:
    return {
        "enabled": bool(settings.dq_enabled),
        "provider": settings.dq_provider,
        "loginRequired": bool(settings.auth_enabled),
        "maxRows": settings.dq_max_rows,
        "intents": [
            {
                "code": intent,
                "label": INTENT_LABELS[intent],
                "roles": list(ACADEMIC_ROLES) if intent in ("schedule", "grades")
                else list(CARD_ROLES),
                "examples": {
                    "schedule": ["本周课表", "周一有什么课"],
                    "grades": ["我的成绩", "上学期绩点"],
                    "card": ["一卡通余额", "最近 7 天消费流水"],
                }[intent],
            }
            for intent in INTENTS
        ],
    }


def query(
    question: str,
    principal,
    role_hint: str | None = None,
    intent: str | None = None,
    params: dict | None = None,
) -> dict:
    """执行一次问数。返回 ``{ok,data|error,status,intent}``，不抛业务异常。"""
    if not settings.dq_enabled:
        return _fail("问数服务未启用", 503, intent)

    chosen = str(intent or detect_intent(question) or "").strip()
    if chosen not in INTENTS:
        return _fail(
            "未能识别要查询的数据，可尝试「本周课表」「我的成绩」「一卡通余额」",
            400,
            chosen or None,
        )

    merged = _extract_params(question, chosen)
    for key, value in (params or {}).items():
        if value is not None:
            merged[key] = value
    try:
        if chosen == "schedule":
            merged["week"] = max(1, min(int(merged.get("week") or _semester()[1]), 30))
        if chosen == "card":
            merged["days"] = max(1, min(int(merged.get("days") or 30), 90))
            merged["limit"] = max(
                1, min(int(merged.get("limit") or 10), settings.dq_max_rows)
            )
    except (TypeError, ValueError):
        return _fail("查询参数不合法：week / days / limit 需为整数", 400, chosen)

    identity, denied = _identity(principal, role_hint, chosen)
    if denied is not None:
        denied["intent"] = chosen
        return denied

    source = settings.dq_provider
    try:
        if source == "mock":
            if chosen == "schedule":
                card = _schedule_card(identity, merged, _mock_schedule(identity, merged), "mock")
            elif chosen == "grades":
                card = _grades_card(identity, merged, _mock_grades(identity, merged), "mock")
            else:
                card = _card_card(identity, merged, _mock_card(identity, merged), "mock")
        else:
            if chosen == "schedule":
                card = _schedule_card(identity, merged, _fetch_schedule(identity, merged), "http")
            elif chosen == "grades":
                card = _grades_card(identity, merged, _fetch_grades(identity, merged), "http")
            else:
                card = _card_card(identity, merged, _fetch_card(identity, merged), "http")
    except DataQueryError as exc:
        return _fail(exc.message, exc.status_code, chosen)
    except Exception:  # noqa: BLE001 - 问数失败不能影响问答主链路
        logger.warning("问数查询失败（intent=%s）", chosen, exc_info=True)
        return _fail("问数服务暂时不可用，请稍后重试", 502, chosen)

    card["identity"]["authenticated"] = identity["authenticated"]
    return _ok(card)


def _denial_card(intent: str, result: dict) -> dict:
    status = int(result.get("status") or 403)
    hint = (
        "请先登录统一身份认证后重试。"
        if status == 401
        else "仅本人可查询自己的数据；如身份有误请联系信息网络中心。"
    )
    return {
        "intent": intent,
        "type": "denied",
        "title": "暂时无法查询个人数据",
        "message": str(result.get("error") or "当前身份无权查询"),
        "status": status,
        "hint": hint,
        "footnote": "可尝试切换为本科生 / 研究生身份，或登录后查询。",
    }


def chat_cards(question: str, principal, role_hint: str | None = None) -> list[dict]:
    """对话增强：识别到问数意图时返回卡片；无意图或参数错误时静默。"""
    if not settings.dq_enabled:
        return []
    intent = detect_intent(question)
    if not intent:
        return []
    result = query(question, principal, role_hint, intent=intent)
    if result.get("ok"):
        return [result["data"]]
    if int(result.get("status") or 0) == 400:
        return []
    return [_denial_card(intent, result)]
