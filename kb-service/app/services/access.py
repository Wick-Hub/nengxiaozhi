"""通行查询智能体（任务 4.4 / P-DQ-04、I-04）。

* **本地同步分析库**：``ACCESS_SYNC_PROVIDER=mock|http`` 拉取门禁流水后整窗替换
  本地 ``access_event``（MySQL）或 ``data/access_events.json``（JSON），所有统计与
  下钻都在本地完成，不让每次看板查询都压门禁平台。
* **多维分析**：出入总量、校区、学院、门禁点、逐日趋势、24 小时分布、
  非正常时段（23:00–06:00）与上游异常标记。
* **层级下钻**：校区 → 学院 / 门禁点 / 人员，学院或门禁点 → 时段与人员排行。
* **排行**：个人 TOP10、学院 TOP5；学工号在接口与报告中统一脱敏。
* **智能报告**：基于聚合指标生成 Markdown 分析报告（不向外部模型发送个人明细）。

仅管理员可用：接口挂 ``require_admin`` + RBAC ``access.view/manage/export``。
"""

from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from urllib.parse import urlencode

import httpx

from app.config import settings
from app.db import access_store
from app.security import masking

logger = logging.getLogger(__name__)

NIGHT_START = 23
NIGHT_END = 6
NIGHT_REASON = "深夜出入"
TAIL_REASONS = ("尾随告警", "无有效刷卡", "异常折返")
DEFAULT_TOP_PERSONS = 10
DEFAULT_TOP_COLLEGES = 5

CAMPUSES: list[dict] = [
    {"campusId": 1, "campusName": "主校区"},
    {"campusId": 2, "campusName": "东校区"},
    {"campusId": 3, "campusName": "南校区"},
]
CAMPUS_NAMES = {int(item["campusId"]): item["campusName"] for item in CAMPUSES}
GATES: list[dict] = [
    {"gateId": 1, "gateName": "南门", "campusId": 1},
    {"gateId": 2, "gateName": "北门", "campusId": 1},
    {"gateId": 3, "gateName": "图书馆闸机", "campusId": 1},
    {"gateId": 4, "gateName": "教学楼 A 座", "campusId": 1},
    {"gateId": 5, "gateName": "东门", "campusId": 2},
    {"gateId": 6, "gateName": "实验楼闸机", "campusId": 2},
    {"gateId": 7, "gateName": "南门", "campusId": 3},
    {"gateId": 8, "gateName": "体育馆闸机", "campusId": 3},
]
GATE_NAMES = {
    int(item["gateId"]): f"{CAMPUS_NAMES.get(int(item['campusId']), '')}{item['gateName']}"
    for item in GATES
}
COLLEGES: list[str] = [
    "计算机学院",
    "机械工程学院",
    "经济管理学院",
    "外国语学院",
    "材料科学与工程学院",
    "马克思主义学院",
]
ROLES: tuple[str, ...] = ("本科生", "研究生", "教职工", "访客")
VISITOR_COLLEGE = "校外人员"

SURNAMES = list("赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许")
GIVEN_NAMES = [
    "子涵", "雨桐", "浩然", "欣怡", "宇航", "思远", "梓萱", "俊杰", "雨泽", "若曦",
    "明轩", "诗涵", "天宇", "佳怡", "博文", "晨曦", "嘉豪", "梦琪", "亦辰", "静怡",
    "泽宇", "可欣", "文昊", "雅婷", "一诺",
]


def _ok(data) -> dict:
    return {"ok": True, "data": data, "error": None}


def _fail(message: str, status: int = 400) -> dict:
    return {"ok": False, "data": None, "error": message, "status": status}


def _hash(*parts) -> int:
    raw = "|".join(str(part) for part in parts).encode("utf-8")
    return int(hashlib.sha256(raw).hexdigest()[:12], 16)


def _ratio(part: int, total: int) -> float:
    return round(part / total, 4) if total else 0.0


def _mask_no(value: str) -> str:
    text = masking.mask_text(str(value or "")) or ""
    if text and text == str(value) and len(text) > 4:
        text = text[:4] + "*" * (len(text) - 4)
    return text


def _campus_gates(campus_id: int) -> list[dict]:
    return [gate for gate in GATES if int(gate["campusId"]) == int(campus_id)]


def _build_persons() -> list[dict]:
    persons: list[dict] = []
    for idx in range(48):
        if idx < 30:
            role, user_no = "本科生", f"2023{idx + 1:04d}"
        elif idx < 36:
            role, user_no = "研究生", f"2024{idx - 29:04d}"
        else:
            role, user_no = "教职工", f"T{10001 + idx - 36:05d}"
        campus = CAMPUSES[idx % len(CAMPUSES)]
        gates = _campus_gates(int(campus["campusId"]))
        persons.append({
            "userNo": user_no,
            "userName": SURNAMES[idx % len(SURNAMES)] + GIVEN_NAMES[(idx * 7) % len(GIVEN_NAMES)],
            "role": role,
            "collegeName": COLLEGES[idx % len(COLLEGES)],
            "campusId": int(campus["campusId"]),
            "campusName": campus["campusName"],
            "gateId": int(gates[idx % len(gates)]["gateId"]),
            "gateName": gates[idx % len(gates)]["gateName"],
        })
    return persons


PERSONS: list[dict] = _build_persons()
VISITORS: list[dict] = [
    {
        "userNo": f"V2026{i:04d}",
        "userName": SURNAMES[(i * 5) % len(SURNAMES)] + "访客",
        "role": "访客",
        "collegeName": VISITOR_COLLEGE,
    }
    for i in range(8)
]


# ------------------------------------------------------------------ mock 数据
def _is_active(person_idx: int, day: date) -> bool:
    digest = _hash("active", person_idx, day.isoformat())
    if day.weekday() >= 5:
        return digest % 100 < 18
    return digest % 100 < 82


def _moment(day: date, start_minute: int, span: int, *seed) -> datetime:
    minute = start_minute + _hash("minute", *seed) % max(1, span)
    return datetime.combine(day, time()) + timedelta(minutes=minute)


def _event(
    person: dict,
    moment: datetime,
    direction: str,
    *,
    gate: dict | None = None,
    reason: str = "",
) -> dict:
    moment = moment.replace(second=0, microsecond=0)
    if moment.hour >= NIGHT_START or moment.hour < NIGHT_END:
        reason = reason or NIGHT_REASON
    campus = CAMPUSES[int(person.get("campusId") or 1) - 1] if person.get("campusId") else CAMPUSES[0]
    chosen = gate or {
        "gateId": person.get("gateId") or 0,
        "gateName": person.get("gateName") or "",
        "campusId": campus["campusId"],
    }
    return {
        "eventTime": moment.isoformat(sep=" ", timespec="seconds"),
        "direction": direction,
        "campusId": int(chosen.get("campusId") or campus["campusId"]),
        "campusName": campus["campusName"],
        "collegeName": person.get("collegeName") or "",
        "gateId": int(chosen.get("gateId") or 0),
        "gateName": chosen.get("gateName") or "",
        "userNo": person.get("userNo") or "",
        "userName": person.get("userName") or "",
        "role": person.get("role") or "",
        "abnormal": bool(reason),
        "abnormalReason": reason,
    }


def _pick_gate(person: dict, day: date, slot: str) -> dict:
    gates = _campus_gates(int(person.get("campusId") or 1))
    if not gates:
        return {}
    if _hash("gate", person.get("userNo"), day.isoformat(), slot) % 100 < 78:
        return next(
            (gate for gate in gates if int(gate["gateId"]) == int(person.get("gateId") or 0)),
            gates[0],
        )
    return gates[_hash("gate-alt", person.get("userNo"), day.isoformat(), slot) % len(gates)]


def _day_events(events: list[dict], day: date) -> None:
    for idx, person in enumerate(PERSONS):
        if not _is_active(idx, day):
            continue
        gate_in = _pick_gate(person, day, "in")
        gate_out = _pick_gate(person, day, "out")
        events.append(_event(
            person,
            _moment(day, 7 * 60, 130, "morning", idx, day.isoformat()),
            "in",
            gate=gate_in,
        ))
        events.append(_event(
            person,
            _moment(day, 17 * 60, 150, "evening", idx, day.isoformat()),
            "out",
            gate=gate_out,
        ))

        if _hash("midday", idx, day.isoformat()) % 100 < 26:
            events.append(_event(
                person,
                _moment(day, 11 * 60 + 30, 70, "lunch-out", idx, day.isoformat()),
                "out",
                gate=gate_out,
            ))
            events.append(_event(
                person,
                _moment(day, 12 * 60 + 30, 80, "lunch-in", idx, day.isoformat()),
                "in",
                gate=gate_in,
            ))

        if _hash("late", idx, day.isoformat()) % 100 < 8:
            events.append(_event(
                person,
                _moment(day, 21 * 60, 140, "late", idx, day.isoformat()),
                "in",
                gate=gate_in,
            ))

        if _hash("night", idx, day.isoformat()) % 100 < 4:
            events.append(_event(
                person,
                _moment(day, 5, 330, "night", idx, day.isoformat()),
                "out" if _hash("night-dir", idx, day.isoformat()) % 2 else "in",
                gate=gate_out,
            ))

        if _hash("tail", idx, day.isoformat()) % 1000 < 9:
            reason = TAIL_REASONS[_hash("tail-reason", idx, day.isoformat()) % len(TAIL_REASONS)]
            events.append(_event(
                person,
                _moment(day, 10 * 60, 360, "tail", idx, day.isoformat()),
                "in" if _hash("tail-dir", idx, day.isoformat()) % 2 else "out",
                gate=_pick_gate(person, day, "tail"),
                reason=reason,
            ))

    for slot in range(6):
        if _hash("visitor", day.isoformat(), slot) % 100 >= 65:
            continue
        visitor = VISITORS[_hash("visitor-pick", day.isoformat(), slot) % len(VISITORS)]
        campus = CAMPUSES[_hash("visitor-campus", day.isoformat(), slot) % len(CAMPUSES)]
        gates = _campus_gates(int(campus["campusId"]))
        gate = gates[_hash("visitor-gate", day.isoformat(), slot) % len(gates)]
        visitor = {
            **visitor,
            "campusId": campus["campusId"],
            "campusName": campus["campusName"],
            "gateId": gate["gateId"],
            "gateName": gate["gateName"],
        }
        events.append(_event(
            visitor,
            _moment(day, 8 * 60 + 30, 150, "visitor-in", day.isoformat(), slot),
            "in",
            gate=gate,
        ))
        events.append(_event(
            visitor,
            _moment(day, 15 * 60, 150, "visitor-out", day.isoformat(), slot),
            "out",
            gate=gate,
        ))


def mock_events(days: int | None = None, end: date | None = None) -> list[dict]:
    """生成确定性演示流水；同一结束日期 + 天数下结果完全一致。"""
    window = max(1, min(int(days or settings.access_sync_days), 366))
    end_day = end or date.today()
    start_day = end_day - timedelta(days=window - 1)
    events: list[dict] = []
    for offset in range(window):
        _day_events(events, start_day + timedelta(days=offset))
    events.sort(key=lambda item: item["eventTime"])
    counters: dict[str, int] = defaultdict(int)
    for event in events:
        token = event["eventTime"][:10].replace("-", "")
        counters[token] += 1
        event["eventNo"] = f"AC{token}{counters[token]:06d}"
    return events


# ------------------------------------------------------------------ 同步
def _parse_dt_value(raw) -> datetime | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, datetime):
        return raw.replace(second=0, microsecond=0)
    if isinstance(raw, (int, float)) or (isinstance(raw, str) and raw.strip().isdigit()):
        value = float(raw)
        if value > 10_000_000_000:
            value /= 1000
        try:
            return datetime.fromtimestamp(value).replace(second=0, microsecond=0)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(raw).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed.replace(second=0, microsecond=0)


def _truthy(raw) -> bool:
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes", "on", "是")
    return bool(raw)


def _direction(raw) -> str | None:
    if raw in (1, "1", "in", "进入", "入", "进", True):
        return "in"
    if raw in (0, "0", "out", "离开", "出", False):
        return "out"
    return None


def _pick(raw: dict, *keys):
    for key in keys:
        if key in raw and raw.get(key) not in (None, ""):
            return raw.get(key)
    return None


def _normalize_remote(raw, idx: int) -> dict | None:
    if not isinstance(raw, dict):
        return None
    user_no = str(_pick(raw, "userNo", "user_no", "userNo", "cardNo", "card_no") or "").strip()
    moment = _parse_dt_value(_pick(raw, "eventTime", "event_time", "time", "timestamp", "occurAt"))
    direction = _direction(_pick(raw, "direction", "inOut", "in_out", "type"))
    if not user_no or moment is None or direction is None:
        return None

    campus_id_raw = _pick(raw, "campusId", "campus_id")
    campus_name = str(_pick(raw, "campusName", "campus_name", "campus") or "").strip()
    try:
        campus_id = int(campus_id_raw) if campus_id_raw not in (None, "") else 0
    except (TypeError, ValueError):
        campus_id = 0
    if campus_id in CAMPUS_NAMES:
        campus_name = campus_name or CAMPUS_NAMES[campus_id]
    elif campus_name in CAMPUS_NAMES.values():
        campus_id = next(key for key, name in CAMPUS_NAMES.items() if name == campus_name)
    elif not campus_name:
        campus_name = "未知校区"

    gate_id_raw = _pick(raw, "gateId", "gate_id", "doorId", "door_id")
    gate_name = str(_pick(raw, "gateName", "gate_name", "doorName", "door_name", "gate") or "").strip()
    try:
        gate_id = int(gate_id_raw) if gate_id_raw not in (None, "") else 0
    except (TypeError, ValueError):
        gate_id = 0
    if gate_id and not gate_name:
        gate_name = GATE_NAMES.get(gate_id, f"门禁点#{gate_id}")
    if not gate_name:
        gate_name = "未知门禁点"

    user_name = str(_pick(raw, "userName", "user_name", "name") or "").strip() or user_no
    role = str(_pick(raw, "role", "identity", "userType", "user_type") or "访客").strip()[:32]
    college = str(
        _pick(raw, "collegeName", "college_name", "college", "orgName", "org_name") or ""
    ).strip()[:128]
    reason = str(_pick(raw, "abnormalReason", "abnormal_reason", "reason", "alertReason") or "").strip()[:64]
    abnormal = _truthy(_pick(raw, "abnormal", "isAbnormal", "is_abnormal", "alert"))
    if moment.hour >= NIGHT_START or moment.hour < NIGHT_END:
        abnormal = True
        reason = reason or NIGHT_REASON

    event_no = str(
        _pick(raw, "eventNo", "event_no", "eventId", "event_id", "recordId", "record_id", "id") or ""
    ).strip()[:64]
    if not event_no:
        event_no = f"HW{moment.strftime('%Y%m%d%H%M%S')}{idx:06d}"
    return {
        "eventNo": event_no,
        "eventTime": moment.isoformat(sep=" ", timespec="seconds"),
        "direction": direction,
        "campusId": campus_id,
        "campusName": campus_name,
        "collegeName": college,
        "gateId": gate_id,
        "gateName": gate_name,
        "userNo": user_no,
        "userName": user_name,
        "role": role,
        "abnormal": abnormal,
        "abnormalReason": reason,
    }


def _extract_items(body) -> tuple[list | None, str | None]:
    data = body
    if isinstance(data, dict):
        for key in ("items", "list", "records"):
            value = data.get(key)
            if isinstance(value, list):
                return value, None
        for key in ("data", "result"):
            value = data.get(key)
            if isinstance(value, list):
                return value, None
            if isinstance(value, dict):
                for inner in ("items", "list", "records"):
                    nested = value.get(inner)
                    if isinstance(nested, list):
                        return nested, None
    if isinstance(data, list):
        return data, None
    return None, "同步接口返回格式不正确（期望流水数组）"


def _fetch_http(start: date, end: date) -> tuple[list | None, str | None]:
    url = str(settings.access_sync_url or "").strip()
    if not url:
        return None, "未配置 ACCESS_SYNC_URL，无法同步门禁流水"
    if not url.lower().startswith(("http://", "https://")):
        return None, "ACCESS_SYNC_URL 必须是 http(s) 地址"
    query = urlencode({"from": start.isoformat(), "to": end.isoformat()})
    full_url = f"{url}{'&' if '?' in url else '?'}{query}"
    headers = {"Accept": "application/json"}
    token = str(settings.access_sync_token or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = httpx.get(
            full_url,
            headers=headers,
            timeout=settings.access_sync_timeout,
            follow_redirects=True,
        )
    except httpx.TimeoutException:
        return None, "门禁数据平台接口超时"
    except httpx.HTTPError as exc:
        return None, f"门禁数据平台接口不可达（{type(exc).__name__}）"
    if resp.status_code >= 400:
        return None, f"门禁数据平台接口返回 HTTP {resp.status_code}"
    try:
        body = resp.json()
    except ValueError:
        return None, "门禁数据平台接口返回的不是 JSON"
    return _extract_items(body)


def sync_events(days: int | None = None) -> dict:
    window = max(1, min(int(days or settings.access_sync_days), 366))
    end_day = date.today()
    start_day = end_day - timedelta(days=window - 1)
    provider = settings.access_sync_provider

    if provider == "mock":
        raw_items: list = mock_events(window, end_day)
        skipped = 0
    else:
        raw_items, error = _fetch_http(start_day, end_day)
        if error:
            return _fail(error, 502)
        if len(raw_items) > settings.access_max_events:
            return _fail(
                f"单次同步流水 {len(raw_items)} 条，超过上限 {settings.access_max_events}",
                413,
            )
        skipped = 0

    records: list[dict] = []
    seen: set[str] = set()
    for idx, raw in enumerate(raw_items):
        record = _normalize_remote(raw, idx)
        if record is None or record["eventNo"] in seen:
            skipped += 1
            continue
        seen.add(record["eventNo"])
        records.append(record)

    if not records:
        return _fail("同步接口未返回有效流水，本地数据保持不变", 502)

    stored = access_store.replace_events(records, provider=provider, days=window)
    logger.info(
        "通行流水同步完成：provider=%s imported=%s skipped=%s", provider, len(records), skipped
    )
    return _ok({
        "provider": provider,
        "total": len(raw_items),
        "imported": len(records),
        "skipped": skipped,
        "abnormal": sum(1 for record in records if record["abnormal"]),
        "days": window,
        "from": start_day.isoformat(),
        "to": end_day.isoformat(),
        "syncedAt": stored["syncedAt"],
    })


# ------------------------------------------------------------------ 筛选
def _parse_day(raw, default: date) -> tuple[date | None, str | None]:
    if raw in (None, ""):
        return default, None
    text = str(raw).strip()
    try:
        return datetime.strptime(text, "%Y-%m-%d").date(), None
    except ValueError:
        return None, "日期格式不正确（示例：2026-10-01）"


def _parse_int(raw, label: str) -> tuple[int | None, str | None]:
    if raw in (None, ""):
        return None, None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None, f"{label} 必须是整数"
    if value < 0:
        return None, f"{label} 不能为负数"
    return value, None


def _parse_bool(raw) -> tuple[bool | None, str | None]:
    if raw in (None, ""):
        return None, None
    if isinstance(raw, bool):
        return raw, None
    if isinstance(raw, (int, float)):
        return bool(raw), None
    text = str(raw).strip().lower()
    if text in ("1", "true", "yes", "是"):
        return True, None
    if text in ("0", "false", "no", "否"):
        return False, None
    return None, "异常筛选只能是 true / false"


def normalize_filters(
    *,
    date_from=None,
    date_to=None,
    campus_id=None,
    college=None,
    gate_id=None,
    role=None,
    abnormal=None,
    direction=None,
    q=None,
) -> tuple[dict | None, dict | None, str | None]:
    today = date.today()
    default_from = today - timedelta(days=max(1, settings.access_sync_days) - 1)
    start, error = _parse_day(date_from, default_from)
    if error:
        return None, None, error
    end, error = _parse_day(date_to, today)
    if error:
        return None, None, error
    assert start is not None and end is not None
    if start > end:
        return None, None, "开始日期不能晚于结束日期"
    span = (end - start).days + 1
    if span > settings.access_max_range_days:
        return None, None, f"单次分析跨度不能超过 {settings.access_max_range_days} 天"

    campus_value, error = _parse_int(campus_id, "校区")
    if error:
        return None, None, error
    gate_value, error = _parse_int(gate_id, "门禁点")
    if error:
        return None, None, error
    abnormal_value, error = _parse_bool(abnormal)
    if error:
        return None, None, error
    direction_value = str(direction or "").strip().lower()
    if direction_value and direction_value not in ("in", "out"):
        return None, None, "direction 只能是 in 或 out"

    keyword = str(q or "").strip()[:64]
    filters = {
        "date_from": datetime.combine(start, time(0, 0, 0)),
        "date_to": datetime.combine(end, time(23, 59, 59)),
        "campus_id": campus_value,
        "college": str(college or "").strip()[:128] or None,
        "gate_id": gate_value,
        "role": str(role or "").strip()[:32] or None,
        "abnormal": abnormal_value,
        "direction": direction_value or None,
        "q": keyword or None,
    }
    window = {"from": start.isoformat(), "to": end.isoformat(), "days": span}
    return filters, window, None


# ------------------------------------------------------------------ 聚合
def _is_night(event: dict) -> bool:
    moment = access_store._as_dt(event.get("eventTime"))
    return bool(moment and (moment.hour >= NIGHT_START or moment.hour < NIGHT_END))


def _person_key(event: dict) -> str:
    return str(event.get("userNo") or event.get("userName") or "-")


def _summary(events: list[dict], window: dict, hourly: list[dict]) -> dict:
    total = len(events)
    entries = sum(1 for event in events if event.get("direction") == "in")
    exits = total - entries
    abnormal = sum(1 for event in events if event.get("abnormal"))
    persons = {_person_key(event) for event in events}
    active_days = len({str(event.get("eventTime") or "")[:10] for event in events})
    peak = max(hourly, key=lambda row: row["total"], default=None)
    return {
        "total": total,
        "entries": entries,
        "exits": exits,
        "persons": len(persons),
        "abnormal": abnormal,
        "abnormalRate": _ratio(abnormal, total),
        "night": sum(1 for event in events if _is_night(event)),
        "avgDaily": round(total / window["days"], 1) if window["days"] else 0,
        "activeDays": active_days,
        "peakHour": None if not peak or not peak["total"] else {
            "hour": peak["hour"],
            "label": peak["label"],
            "total": peak["total"],
        },
        "lastEventAt": max((event.get("eventTime") or "" for event in events), default=None),
    }


def _trend(events: list[dict], window: dict) -> list[dict]:
    start = datetime.strptime(window["from"], "%Y-%m-%d").date()
    end = datetime.strptime(window["to"], "%Y-%m-%d").date()
    days: dict[str, dict] = {}
    people: dict[str, set] = defaultdict(set)
    for event in events:
        token = str(event.get("eventTime") or "")[:10]
        if not token:
            continue
        row = days.setdefault(token, {
            "date": token,
            "total": 0,
            "entries": 0,
            "exits": 0,
            "abnormal": 0,
            "persons": 0,
        })
        row["total"] += 1
        if event.get("direction") == "in":
            row["entries"] += 1
        else:
            row["exits"] += 1
        if event.get("abnormal"):
            row["abnormal"] += 1
        people[token].add(_person_key(event))
    rows: list[dict] = []
    cursor = start
    while cursor <= end:
        token = cursor.isoformat()
        row = days.get(token) or {
            "date": token,
            "total": 0,
            "entries": 0,
            "exits": 0,
            "abnormal": 0,
            "persons": 0,
        }
        row["persons"] = len(people.get(token, set()))
        row["weekend"] = cursor.weekday() >= 5
        rows.append(row)
        cursor += timedelta(days=1)
    return rows


def _hourly(events: list[dict]) -> list[dict]:
    rows = [
        {"hour": hour, "label": f"{hour:02d}:00", "total": 0, "entries": 0, "exits": 0, "abnormal": 0}
        for hour in range(24)
    ]
    for event in events:
        moment = access_store._as_dt(event.get("eventTime"))
        if moment is None:
            continue
        row = rows[moment.hour]
        row["total"] += 1
        if event.get("direction") == "in":
            row["entries"] += 1
        else:
            row["exits"] += 1
        if event.get("abnormal"):
            row["abnormal"] += 1
    return rows


def _group_rows(events: list[dict], key_func, fields_func) -> list[dict]:
    buckets: dict[tuple, dict] = {}
    total = len(events)
    for event in events:
        key = key_func(event)
        bucket = buckets.setdefault(key, {
            "total": 0,
            "entries": 0,
            "exits": 0,
            "abnormal": 0,
            "_persons": set(),
            **fields_func(event),
        })
        bucket["total"] += 1
        if event.get("direction") == "in":
            bucket["entries"] += 1
        else:
            bucket["exits"] += 1
        if event.get("abnormal"):
            bucket["abnormal"] += 1
        bucket["_persons"].add(_person_key(event))
    rows = []
    for bucket in buckets.values():
        bucket["persons"] = len(bucket.pop("_persons"))
        bucket["share"] = _ratio(bucket["total"], total)
        rows.append(bucket)
    rows.sort(key=lambda row: (-row["total"], str(row.get("campusName") or row.get("gateName") or row.get("collegeName") or "")))
    return rows


def _by_campus(events: list[dict]) -> list[dict]:
    return _group_rows(
        events,
        lambda event: (int(event.get("campusId") or 0), event.get("campusName") or "未知校区"),
        lambda event: {
            "campusId": int(event.get("campusId") or 0),
            "campusName": event.get("campusName") or "未知校区",
        },
    )


def _by_college(events: list[dict]) -> list[dict]:
    return _group_rows(
        events,
        lambda event: event.get("collegeName") or "未标注学院",
        lambda event: {"collegeName": event.get("collegeName") or "未标注学院"},
    )


def _by_gate(events: list[dict]) -> list[dict]:
    return _group_rows(
        events,
        lambda event: (
            int(event.get("gateId") or 0),
            event.get("gateName") or "未知门禁点",
            int(event.get("campusId") or 0),
        ),
        lambda event: {
            "gateId": int(event.get("gateId") or 0),
            "gateName": event.get("gateName") or "未知门禁点",
            "campusId": int(event.get("campusId") or 0),
            "campusName": event.get("campusName") or "未知校区",
        },
    )


def _top_persons(events: list[dict], limit: int = DEFAULT_TOP_PERSONS) -> list[dict]:
    buckets: dict[str, dict] = {}
    for event in events:
        key = _person_key(event)
        bucket = buckets.setdefault(key, {
            "userNoMasked": _mask_no(str(event.get("userNo") or "")),
            "userName": event.get("userName") or "",
            "role": event.get("role") or "",
            "collegeName": event.get("collegeName") or "",
            "total": 0,
            "entries": 0,
            "exits": 0,
            "abnormal": 0,
            "lastAt": "",
        })
        bucket["total"] += 1
        if event.get("direction") == "in":
            bucket["entries"] += 1
        else:
            bucket["exits"] += 1
        if event.get("abnormal"):
            bucket["abnormal"] += 1
        bucket["lastAt"] = max(bucket["lastAt"], str(event.get("eventTime") or ""))
    rows = sorted(
        buckets.values(),
        key=lambda row: (-row["total"], -row["abnormal"], row["userName"]),
    )[: max(1, min(int(limit or DEFAULT_TOP_PERSONS), 50))]
    for idx, row in enumerate(rows, start=1):
        row["rank"] = idx
    return rows


def _abnormal_detail(events: list[dict]) -> dict:
    abnormal_events = [event for event in events if event.get("abnormal")]
    reasons: dict[str, int] = defaultdict(int)
    for event in abnormal_events:
        reasons[event.get("abnormalReason") or "未标注"] += 1
    recent = sorted(abnormal_events, key=lambda event: event.get("eventTime") or "", reverse=True)[:10]
    return {
        "total": len(abnormal_events),
        "persons": len({_person_key(event) for event in abnormal_events}),
        "night": sum(1 for event in events if _is_night(event)),
        "nightWindow": f"{NIGHT_START:02d}:00–{NIGHT_END:02d}:00",
        "byReason": [
            {"reason": reason, "count": count}
            for reason, count in sorted(reasons.items(), key=lambda row: (-row[1], row[0]))
        ],
        "recent": [
            {
                "eventTime": event.get("eventTime"),
                "campusName": event.get("campusName") or "",
                "gateName": event.get("gateName") or "",
                "userName": event.get("userName") or "",
                "userNoMasked": _mask_no(str(event.get("userNo") or "")),
                "role": event.get("role") or "",
                "abnormalReason": event.get("abnormalReason") or "未标注",
            }
            for event in recent
        ],
    }


def _options(events: list[dict] | None = None) -> dict:
    campuses = [dict(item) for item in CAMPUSES]
    gates = [dict(item) for item in GATES]
    colleges = list(COLLEGES)
    roles = list(ROLES)
    for event in events or []:
        campus = {
            "campusId": int(event.get("campusId") or 0),
            "campusName": event.get("campusName") or "未知校区",
        }
        if not any(int(item["campusId"]) == campus["campusId"] for item in campuses):
            campuses.append(campus)
        college = event.get("collegeName") or "未标注学院"
        if college not in colleges:
            colleges.append(college)
        gate = {
            "gateId": int(event.get("gateId") or 0),
            "gateName": event.get("gateName") or "未知门禁点",
            "campusId": int(event.get("campusId") or 0),
        }
        if not any(int(item["gateId"]) == gate["gateId"] for item in gates):
            gates.append(gate)
        role = event.get("role") or ""
        if role and role not in roles:
            roles.append(role)
    campuses.sort(key=lambda item: (item["campusId"], item["campusName"]))
    gates.sort(key=lambda item: (item["campusId"], item["gateId"]))
    return {"campuses": campuses, "gates": gates, "colleges": colleges, "roles": roles}


def _sync_meta() -> dict:
    meta = access_store.sync_meta()
    meta["provider"] = meta.get("provider") or settings.access_sync_provider
    meta["configuredProvider"] = settings.access_sync_provider
    meta["urlConfigured"] = bool(str(settings.access_sync_url or "").strip())
    meta["tokenConfigured"] = bool(str(settings.access_sync_token or "").strip())
    meta["syncDays"] = settings.access_sync_days
    meta["maxRangeDays"] = settings.access_max_range_days
    meta["maxEvents"] = settings.access_max_events
    return meta


def _build_overview(filters: dict, window: dict, events: list[dict]) -> dict:
    hourly = _hourly(events)
    by_college = _by_college(events)
    by_campus = _by_campus(events)
    abnormal = _abnormal_detail(events)
    return {
        "window": window,
        "summary": _summary(events, window, hourly),
        "trend": _trend(events, window),
        "hourly": hourly,
        "byCampus": by_campus,
        "byCollege": by_college,
        "byGate": _by_gate(events),
        "topColleges": by_college[:DEFAULT_TOP_COLLEGES],
        "topPersons": _top_persons(events),
        "abnormal": abnormal,
        "options": _options(events),
        "meta": _sync_meta(),
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
    }


def overview(**raw) -> dict:
    filters, window, error = normalize_filters(**raw)
    if error:
        return _fail(error)
    assert filters is not None and window is not None
    events = access_store.list_events(filters)
    return _ok(_build_overview(filters, window, events))


def drilldown(dimension: str, **raw) -> dict:
    kind = str(dimension or "").strip().lower()
    if kind not in ("campus", "college", "gate"):
        return _fail("下钻维度只能是 campus / college / gate")
    filters, window, error = normalize_filters(**raw)
    if error:
        return _fail(error)
    assert filters is not None and window is not None

    if kind == "campus":
        campus_id = filters.get("campus_id")
        if campus_id is None:
            return _fail("校区下钻需要 campusId")
        filters["campus_id"] = int(campus_id)
        events = access_store.list_events(filters)
        title = next(
            (event.get("campusName") for event in events if event.get("campusName")),
            CAMPUS_NAMES.get(int(campus_id), f"校区#{campus_id}"),
        )
    elif kind == "college":
        college = filters.get("college")
        if not college:
            return _fail("学院下钻需要 college")
        filters["college"] = college
        events = access_store.list_events(filters)
        title = college
    else:
        gate_id = filters.get("gate_id")
        if gate_id is None:
            return _fail("门禁点下钻需要 gateId")
        filters["gate_id"] = int(gate_id)
        events = access_store.list_events(filters)
        title = next(
            (event.get("gateName") for event in events if event.get("gateName")),
            GATE_NAMES.get(int(gate_id), f"门禁点#{gate_id}"),
        )

    hourly = _hourly(events)
    by_college = _by_college(events)
    by_campus = _by_campus(events)
    return _ok({
        "dimension": kind,
        "title": title,
        "window": window,
        "summary": _summary(events, window, hourly),
        "trend": _trend(events, window),
        "hourly": hourly,
        "byCampus": by_campus,
        "byCollege": by_college,
        "byGate": _by_gate(events),
        "topPersons": _top_persons(events),
        "abnormal": _abnormal_detail(events),
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
    })


def event_page(**raw) -> dict:
    page = raw.pop("page", 1)
    page_size = raw.pop("page_size", 20)
    filters, window, error = normalize_filters(**raw)
    if error:
        return _fail(error)
    assert filters is not None and window is not None
    result = access_store.query_events(filters, page=page, page_size=page_size)
    for row in result["items"]:
        row["userNoMasked"] = _mask_no(str(row.get("userNo") or ""))
        row["directionLabel"] = "进入" if row.get("direction") == "in" else "离开"
        row["abnormalReason"] = row.get("abnormalReason") or ""
    result["window"] = window
    return _ok(result)


def export_rows(**raw) -> dict:
    filters, window, error = normalize_filters(**raw)
    if error:
        return _fail(error)
    assert filters is not None and window is not None
    events = access_store.list_events(filters)
    events.sort(key=lambda event: (event.get("eventTime") or "", event.get("eventId") or 0), reverse=True)
    total = len(events)
    limit = max(1, int(settings.access_export_limit))
    rows = [
        {
            "time": event.get("eventTime") or "",
            "direction": "进入" if event.get("direction") == "in" else "离开",
            "campus": event.get("campusName") or "",
            "college": event.get("collegeName") or "",
            "gate": event.get("gateName") or "",
            "userName": event.get("userName") or "",
            "userNo": _mask_no(str(event.get("userNo") or "")),
            "role": event.get("role") or "",
            "abnormal": "是" if event.get("abnormal") else "否",
            "reason": event.get("abnormalReason") or "",
        }
        for event in events[:limit]
    ]
    return _ok({
        "rows": rows,
        "total": total,
        "truncated": total > limit,
        "limit": limit,
        "window": window,
    })


# ------------------------------------------------------------------ 报告
def _md_table(headers: list[str], rows: list[list]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(str(cell) for cell in row) + " |" for row in rows)
    return "\n".join(lines)


def _report_markdown(data: dict) -> str:
    summary = data["summary"]
    window = data["window"]
    meta = data["meta"]
    by_campus = data["byCampus"]
    by_college = data["topColleges"]
    top_persons = data["topPersons"]
    abnormal = data["abnormal"]
    trend = data["trend"]
    hourly = data["hourly"]

    lines = [
        "# 校园通行智能分析报告",
        "",
        f"- **统计区间**：{window['from']} 至 {window['to']}（{window['days']} 天）",
        f"- **数据来源**：{meta.get('provider') or '本地同步库'}（最近同步 "
        f"{meta.get('syncedAt') or '尚未同步'}）",
        f"- **生成时间**：{data['generatedAt']}",
        "- **隐私说明**：学工号已脱敏；报告仅基于本地聚合指标生成，未向外部模型发送个人明细。",
        "",
        "## 一、总体概况",
        "",
        _md_table(
            ["指标", "数值"],
            [
                ["通行总量", summary["total"]],
                ["进入 / 离开", f"{summary['entries']} / {summary['exits']}"],
                ["去重人数", summary["persons"]],
                ["日均通行", summary["avgDaily"]],
                ["异常通行", f"{summary['abnormal']}（{summary['abnormalRate'] * 100:.1f}%）"],
                ["深夜出入", summary["night"]],
                [
                    "高峰时段",
                    summary["peakHour"]["label"] if summary["peakHour"] else "无数据",
                ],
            ],
        ),
    ]

    if by_campus:
        lines += ["", "## 二、校区分布", "", _md_table(
            ["校区", "通行量", "进入", "离开", "去重人数", "占比"],
            [
                [
                    row["campusName"], row["total"], row["entries"], row["exits"],
                    row["persons"], f"{row['share'] * 100:.1f}%",
                ]
                for row in by_campus
            ],
        )]

    if by_college:
        lines += ["", "## 三、学院通行 TOP5", "", _md_table(
            ["排名", "学院", "通行量", "去重人数", "占比"],
            [
                [row["rank"] if "rank" in row else idx, row["collegeName"], row["total"], row["persons"],
                 f"{row['share'] * 100:.1f}%"]
                for idx, row in enumerate(by_college, start=1)
            ],
        )]

    if top_persons:
        lines += ["", "## 四、通行频次 TOP10（学工号脱敏）", "", _md_table(
            ["排名", "姓名", "身份", "学院", "通行量", "异常", "最近通行"],
            [
                [
                    row["rank"], row["userName"], row["role"], row["collegeName"],
                    row["total"], row["abnormal"], row["lastAt"],
                ]
                for row in top_persons
            ],
        )]

    lines += ["", "## 五、异常通行", ""]
    if abnormal["total"]:
        lines.append(
            f"异常通行 {abnormal['total']} 次，涉及 {abnormal['persons']} 人；"
            f"深夜时段（{abnormal['nightWindow']}）通行 {abnormal['night']} 次。"
        )
        lines += ["", _md_table(
            ["异常类型", "次数"],
            [[row["reason"], row["count"]] for row in abnormal["byReason"]],
        )]
    else:
        lines.append("所选区间未发现异常通行记录。")

    peak = summary["peakHour"]
    busiest_day = max(trend, key=lambda row: row["total"], default=None)
    lines += ["", "## 六、趋势与高峰", ""]
    if peak:
        lines.append(
            f"- 高峰时段为 **{peak['label']}**，共 {peak['total']} 次，"
            f"占全量 {peak['total'] / summary['total'] * 100:.1f}%。"
        )
    if busiest_day and busiest_day["total"]:
        lines.append(
            f"- 最繁忙日期为 **{busiest_day['date']}**，共 {busiest_day['total']} 次"
            f"（周末：{'是' if busiest_day.get('weekend') else '否'}）。"
        )
    midday = sum(row["total"] for row in hourly if 11 <= row["hour"] <= 13)
    night = sum(row["total"] for row in hourly if row["hour"] >= 23 or row["hour"] < 6)
    lines.append(f"- 午间（11:00–14:00）通行 {midday} 次；深夜（23:00–06:00）通行 {night} 次。")

    lines += ["", "## 七、结论与建议", ""]
    if summary["total"]:
        top_campus = by_campus[0] if by_campus else None
        top_gate = data["byGate"][0] if data["byGate"] else None
        top_college = by_college[0] if by_college else None
        if top_campus:
            lines.append(
                f"1. **空间集中度**：{top_campus['campusName']}通行量最高（{top_campus['total']} 次，"
                f"占 {top_campus['share'] * 100:.1f}%），建议结合课程表与门禁通道容量做潮汐调度。"
            )
        if top_gate:
            lines.append(
                f"2. **门禁压力**：{top_gate['gateName']}为最繁忙门禁点（{top_gate['total']} 次），"
                "高峰时段可增开备用通道并安排引导人员。"
            )
        if top_college:
            lines.append(
                f"3. **学院关注**：{top_college['collegeName']}通行量居首（{top_college['total']} 次），"
                "可作为错峰上下课与安全宣导的重点单位。"
            )
        if abnormal["total"]:
            lines.append(
                f"4. **异常处置**：异常率 {summary['abnormalRate'] * 100:.1f}%，其中深夜通行 "
                f"{summary['night']} 次；建议保卫处对深夜时段与尾随告警记录做二次核验。"
            )
        else:
            lines.append("4. **异常处置**：所选区间异常通行较少，保持现有巡查频次即可。")
        if peak:
            split = max(1, len(trend) // 2)
            first = sum(row["total"] for row in trend[:split])
            second = sum(row["total"] for row in trend[split:])
            if first:
                change = (second - first) / first * 100
                lines.append(
                    f"5. **趋势判断**：后半周期较前半周期{'上升' if change >= 0 else '下降'} "
                    f"{abs(change):.1f}%，{'建议关注增长带来的通道压力' if change >= 5 else '整体通行态势平稳'}。"
                )
    else:
        lines.append("所选区间暂无通行流水，请先在管理页执行「同步门禁数据」。")

    lines += [
        "",
        "---",
        "",
        "> 本报告由通行分析服务基于本地同步库聚合生成，指标口径：进入/离开按方向统计，"
        "去重人数按学工号（访客无法识别时按姓名）计算，异常包含上游标记与深夜时段规则。",
        "",
    ]
    return "\n".join(lines)


def build_report(**raw) -> dict:
    filters, window, error = normalize_filters(**raw)
    if error:
        return _fail(error)
    assert filters is not None and window is not None
    events = access_store.list_events(filters)
    data = _build_overview(filters, window, events)
    content = _report_markdown(data)
    generated_at = datetime.now()
    return _ok({
        "filename": f"通行分析报告-{generated_at.strftime('%Y%m%d-%H%M%S')}.md",
        "content": content,
        "generatedAt": generated_at.isoformat(timespec="seconds"),
        "window": window,
    })
