"""统计与运营分析（任务 3.10 / M-T-01～09）。

**口径**

* 与 2.1 会话日志共用 :func:`app.services.chatlog_admin._collect`：
  同一时间 / 渠道 / 助手 / 角色筛选下，统计「提问/回复」与日志命中数一致。
* 提问 = 成功配对的一问一答；回复 = AI 消息；未答 = AI 消息 ``refused=1``；
  成功率 = (回复 − 未答) / 回复；访问 = 会话数；人次 = 去重用户/访客（JSON 无身份，
  按会话计并显式标注）。
* 两种后端都受会话日志的 ``SCAN_LIMIT`` 窗口约束，响应里用 ``window`` 暴露，
  **不伪造全量数据**。

**维度交付状态**

* 渠道 / 助手 / 热门知识 / 分类 / 设备：真实数据（设备自 3.10 起采集）。
* 咨询情绪：``chat_message.sentiment`` 已留列，待 4.6 打标；当前返回空态。
* 应用使用排行：访问埋点未接入，按任务单要求先返回接口与空态。
"""

from __future__ import annotations

import csv
import io
import logging
from collections import defaultdict
from datetime import datetime

from sqlalchemy import distinct, func, select

from app.config import settings
from app.db import models as M
from app.db.kb_repo import ktype_label
from app.db.session import db_session
from app.services import chatlog_admin

logger = logging.getLogger(__name__)

TREND_DAYS = 90
DEFAULT_TOP = 10
MAX_TOP = 50
DEVICE_LABELS = {
    "pc": "电脑端",
    "mobile": "移动端",
    "tablet": "平板",
    "other": "其他",
    "": "未采集",
}
SENTIMENT_LABELS = {1: "负面", 0: "正常"}


# ------------------------------------------------------------------ 工具
def _chunks(seq: list, size: int = 500):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _ratio(part: int, total: int) -> float | None:
    return round(part / total, 4) if total else None


def _day(pair: dict) -> str:
    raw = pair.get("answeredAt") or pair.get("askedAt") or ""
    return str(raw)[:10]


def _top_args(limit: int | None) -> int:
    try:
        n = int(limit or DEFAULT_TOP)
    except (TypeError, ValueError):
        n = DEFAULT_TOP
    return max(1, min(MAX_TOP, n))


def _option_maps() -> tuple[dict, str, dict, str]:
    opts = chatlog_admin.filter_options()
    agent_names = {
        a["agentId"]: a.get("agentName") for a in opts.get("agents", []) if a.get("agentId") is not None
    }
    channel_names = {
        c["channelId"]: c.get("channelName")
        for c in opts.get("channels", [])
        if c.get("channelId") is not None
    }
    default_agent = next(
        (a.get("agentName") for a in opts.get("agents", []) if a.get("agentId") is None),
        "默认助手",
    )
    default_channel = next(
        (c.get("channelName") for c in opts.get("channels", []) if c.get("channelId") is None),
        "默认渠道",
    )
    return agent_names, default_agent, channel_names, default_channel


def _collect(
    *,
    agent_id: int | None,
    channel_id: int | None,
    role: str | None,
    date_from: str | None,
    date_to: str | None,
) -> tuple[list[dict], int]:
    _, pairs, scanned = chatlog_admin._collect(
        agent_id=agent_id,
        channel_id=channel_id,
        role=role,
        date_from=date_from,
        date_to=date_to,
    )
    return pairs, scanned


def _session_ids(pairs: list[dict]) -> list[str]:
    return sorted({str(p["sessionId"]) for p in pairs if p.get("sessionId")})


def _person_count(session_ids: list[str]) -> int:
    """人次：MySQL 按用户/访客去重；JSON 无身份，退化为会话数。"""
    if not session_ids:
        return 0
    if settings.store_label != "mysql":
        return len(session_ids)
    total = 0
    try:
        with db_session() as conn:
            for part in _chunks(session_ids):
                who = func.coalesce(
                    M.ChatSession.user_id, M.ChatSession.visitor_id, M.ChatSession.session_id
                )
                total += int(
                    conn.execute(
                        select(func.count(distinct(who))).where(
                            M.ChatSession.session_id.in_(part)
                        )
                    ).scalar()
                    or 0
                )
    except Exception as exc:  # noqa: BLE001 - 人次是辅助指标，失败不拖垮整页
        logger.warning("统计人次失败（按会话数回退）：%s", exc)
        return len(session_ids)
    return total or len(session_ids)


def _summary(pairs: list[dict], session_ids: list[str]) -> dict:
    replies = len(pairs)
    unanswered = sum(1 for p in pairs if p.get("refused"))
    answered = replies - unanswered
    return {
        "questions": replies,
        "replies": replies,
        "unanswered": unanswered,
        "answered": answered,
        "sessions": len(session_ids),
        "visits": len(session_ids),
        "persons": _person_count(session_ids),
        "successRate": _ratio(answered, replies),
    }


def _trend(pairs: list[dict]) -> tuple[list[dict], bool]:
    days: dict[str, dict] = {}
    sessions_by_day: dict[str, set] = defaultdict(set)
    for p in pairs:
        day = _day(p)
        if not day:
            continue
        row = days.setdefault(
            day, {"date": day, "questions": 0, "replies": 0, "unanswered": 0, "sessions": 0}
        )
        row["questions"] += 1
        row["replies"] += 1
        if p.get("refused"):
            row["unanswered"] += 1
        if p.get("sessionId"):
            sessions_by_day[day].add(p["sessionId"])
    for day, row in days.items():
        row["sessions"] = len(sessions_by_day[day])
    rows = sorted(days.values(), key=lambda r: r["date"])
    truncated = len(rows) > TREND_DAYS
    if truncated:
        rows = rows[-TREND_DAYS:]
    return rows, truncated


def _group(pairs: list[dict], key_fn) -> list[dict]:
    """按 ``key_fn(pair) -> (key, name)`` 聚合，保留原始 key 供前端筛选。"""
    buckets: dict = {}
    session_sets: dict = defaultdict(set)
    for p in pairs:
        key, name = key_fn(p)
        row = buckets.get(key)
        if row is None:
            row = buckets[key] = {"key": key, "name": name, "replies": 0, "unanswered": 0}
        row["replies"] += 1
        if p.get("refused"):
            row["unanswered"] += 1
        if p.get("sessionId"):
            session_sets[key].add(p["sessionId"])
    total = len(pairs)
    rows = []
    for key, row in buckets.items():
        rows.append(
            {
                **row,
                "questions": row["replies"],
                "answered": row["replies"] - row["unanswered"],
                "sessions": len(session_sets[key]),
                "successRate": _ratio(row["replies"] - row["unanswered"], row["replies"]),
                "share": _ratio(row["replies"], total),
            }
        )
    rows.sort(key=lambda r: (-r["replies"], str(r["name"])))
    return rows


def _channel_rows(pairs: list[dict]) -> list[dict]:
    _, _, channel_names, default_channel = _option_maps()

    def key_fn(p):
        cid = p.get("channelId")
        name = p.get("channelName") or channel_names.get(cid)
        if not name:
            name = default_channel if cid is None else f"渠道 #{cid}"
        return cid, name

    rows = _group(pairs, key_fn)
    for row in rows:
        row["channelId"] = row.pop("key")
    return rows


def _agent_rows(pairs: list[dict]) -> list[dict]:
    agent_names, default_agent, _, _ = _option_maps()

    def key_fn(p):
        aid = p.get("agentId")
        name = p.get("agentName") or agent_names.get(aid)
        if not name:
            name = default_agent if aid is None else f"助手 #{aid}"
        return aid, name

    rows = _group(pairs, key_fn)
    for row in rows:
        row["agentId"] = row.pop("key")
    return rows


def _window(scanned: int) -> dict:
    return {
        "scanned": scanned,
        "scanLimit": chatlog_admin.SCAN_LIMIT,
        "truncated": scanned >= chatlog_admin.SCAN_LIMIT,
        "store": settings.store_label,
    }


# ------------------------------------------------------------------ 概览
def overview(
    *,
    agent_id: int | None = None,
    channel_id: int | None = None,
    role: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    pairs, scanned = _collect(
        agent_id=agent_id,
        channel_id=channel_id,
        role=role,
        date_from=date_from,
        date_to=date_to,
    )
    sessions = _session_ids(pairs)
    trend, trend_truncated = _trend(pairs)
    notes: list[str] = []
    window = _window(scanned)
    if window["truncated"]:
        notes.append(
            f"已扫描最近 {window['scanLimit']} 条消息，统计窗口与会话日志一致；请用时间或渠道收窄。"
        )
    if settings.store_label != "mysql":
        notes.append("JSON 模式无渠道/助手/设备的历史落库，相应维度按默认或未采集归并。")
    if trend_truncated:
        notes.append(f"趋势最多展示最近 {TREND_DAYS} 天。")
    return {
        "summary": _summary(pairs, sessions),
        "trend": trend,
        "channels": _channel_rows(pairs),
        "agents": _agent_rows(pairs),
        "window": window,
        "notes": notes,
        "store": settings.store_label,
    }


# ------------------------------------------------------------------ 热门知识
def _json_doc_map() -> dict[int, dict]:
    try:
        from app.core.index_store import get_index

        docs = get_index().docs or []
    except Exception as exc:  # noqa: BLE001 - 索引不可用时退回引用自带字段
        logger.warning("统计读取索引失败（退回引用字段）：%s", exc)
        return {}
    out: dict[int, dict] = {}
    for d in docs:
        digits = "".join(ch for ch in str(d.get("docId") or "") if ch.isdigit())
        if digits:
            out[int(digits)] = d
    return out


def _json_knowledge(pairs: list[dict]) -> tuple[list[dict], list[dict], int]:
    doc_map = _json_doc_map()
    docs: dict[int, dict] = {}
    categories: dict[tuple[str, str], dict] = {}
    citations = 0
    for p in pairs:
        for c in p.get("citations") or []:
            try:
                doc_id = int(c.get("docId") or 0)
            except (TypeError, ValueError):
                doc_id = 0
            meta = doc_map.get(doc_id, {})
            title = meta.get("title") or c.get("titlePath") or f"文档 #{doc_id}"
            dept = meta.get("dept") or c.get("dept") or "未分类"
            ktype = ktype_label(meta.get("ktype") or c.get("ktype")) or "未分类"
            row = docs.setdefault(
                doc_id,
                {"docId": doc_id, "docTitle": title, "dept": dept, "ktype": ktype,
                 "citations": 0, "sessions": set()},
            )
            row["citations"] += 1
            if p.get("sessionId"):
                row["sessions"].add(p["sessionId"])
            cat = categories.setdefault(
                (dept, ktype), {"dept": dept, "ktype": ktype, "citations": 0, "sessions": set()}
            )
            cat["citations"] += 1
            if p.get("sessionId"):
                cat["sessions"].add(p["sessionId"])
            citations += 1
    return _finish_knowledge(docs, categories, citations)


def _mysql_knowledge(pairs: list[dict]) -> tuple[list[dict], list[dict], int]:
    msg_ids = sorted({int(p["msgId"]) for p in pairs if p.get("msgId") is not None})
    if not msg_ids:
        return [], [], 0
    doc_rows: dict[int, dict] = {}
    cat_rows: dict[tuple[str, str], dict] = {}
    citations = 0
    try:
        with db_session() as conn:
            rows = conn.execute(
                select(
                    M.AnswerCitation.doc_id,
                    M.KbDoc.doc_title,
                    M.SysOrg.org_name,
                    M.KbDoc.ktype,
                    func.count(M.AnswerCitation.id),
                    func.count(distinct(M.ChatMessage.session_id)),
                )
                .join(M.KbDoc, M.KbDoc.doc_id == M.AnswerCitation.doc_id)
                .outerjoin(M.SysOrg, M.SysOrg.org_id == M.KbDoc.dept_id)
                .join(M.ChatMessage, M.ChatMessage.msg_id == M.AnswerCitation.msg_id)
                .where(M.AnswerCitation.msg_id.in_(msg_ids))
                .group_by(
                    M.AnswerCitation.doc_id,
                    M.KbDoc.doc_title,
                    M.SysOrg.org_name,
                    M.KbDoc.ktype,
                )
            ).all()
            for r in rows:
                doc_id = int(r[0])
                dept = r[2] or "未分类"
                ktype = ktype_label(r[3]) or "未分类"
                n = int(r[4] or 0)
                doc_rows[doc_id] = {
                    "docId": doc_id,
                    "docTitle": r[1] or f"文档 #{doc_id}",
                    "dept": dept,
                    "ktype": ktype,
                    "citations": n,
                    "sessions": int(r[5] or 0),
                }
                citations += n

            cat_rows_raw = conn.execute(
                select(
                    M.SysOrg.org_name,
                    M.KbDoc.ktype,
                    func.count(M.AnswerCitation.id),
                    func.count(distinct(M.ChatMessage.session_id)),
                )
                .join(M.KbDoc, M.KbDoc.doc_id == M.AnswerCitation.doc_id)
                .outerjoin(M.SysOrg, M.SysOrg.org_id == M.KbDoc.dept_id)
                .join(M.ChatMessage, M.ChatMessage.msg_id == M.AnswerCitation.msg_id)
                .where(M.AnswerCitation.msg_id.in_(msg_ids))
                .group_by(M.SysOrg.org_name, M.KbDoc.ktype)
            ).all()
            for r in cat_rows_raw:
                dept = r[0] or "未分类"
                ktype = ktype_label(r[1]) or "未分类"
                cat_rows[(dept, ktype)] = {
                    "dept": dept,
                    "ktype": ktype,
                    "citations": int(r[2] or 0),
                    "sessions": int(r[3] or 0),
                }
    except Exception as exc:  # noqa: BLE001 - 空态优于整页失败
        logger.warning("统计热门知识失败：%s", exc)
        return [], [], 0
    return _finish_knowledge(doc_rows, cat_rows, citations)


def _finish_knowledge(
    docs: dict[int, dict], cats: dict[tuple[str, str], dict], citations: int
) -> tuple[list[dict], list[dict], int]:
    doc_list = []
    for row in docs.values():
        sessions = row.get("sessions")
        doc_list.append(
            {
                **row,
                "sessions": len(sessions) if isinstance(sessions, set) else int(sessions or 0),
                "share": _ratio(row["citations"], citations),
            }
        )
    doc_list.sort(key=lambda r: (-r["citations"], str(r["docTitle"])))
    cat_list = []
    for row in cats.values():
        sessions = row.get("sessions")
        cat_list.append(
            {
                **row,
                "label": f"{row['dept']} · {row['ktype']}",
                "sessions": len(sessions) if isinstance(sessions, set) else int(sessions or 0),
                "share": _ratio(row["citations"], citations),
            }
        )
    cat_list.sort(key=lambda r: (-r["citations"], r["label"]))
    return doc_list, cat_list, citations


# ------------------------------------------------------------------ 设备 / 情绪 / 应用
def _device_rows(session_ids: list[str]) -> tuple[list[dict], bool]:
    if not session_ids:
        return [], False
    counts: dict[str, int] = defaultdict(int)
    try:
        if settings.store_label == "mysql":
            with db_session() as conn:
                for part in _chunks(session_ids):
                    for r in conn.execute(
                        select(M.ChatSession.device, func.count())
                        .where(M.ChatSession.session_id.in_(part))
                        .group_by(M.ChatSession.device)
                    ).all():
                        counts[str(r[0] or "")] += int(r[1] or 0)
        else:
            from app.db import chatlog as chatlog_store

            wanted = set(session_ids)
            for sess in chatlog_store.read_all_sessions():
                sid = sess.get("sessionId")
                if sid in wanted:
                    counts[str(sess.get("device") or "")] += 1
    except Exception as exc:  # noqa: BLE001
        logger.warning("统计访问设备失败：%s", exc)
        return [], False
    total = sum(counts.values())
    rows = [
        {
            "key": key or "unknown",
            "label": DEVICE_LABELS.get(key, key or "未采集"),
            "count": n,
            "share": _ratio(n, total),
        }
        for key, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        if n > 0
    ]
    tracked = sum(n for key, n in counts.items() if key)
    return rows, tracked > 0


def _sentiment_rows(pairs: list[dict]) -> tuple[list[dict], bool]:
    if settings.store_label != "mysql":
        return [], False
    msg_ids = sorted({int(p["msgId"]) for p in pairs if p.get("msgId") is not None})
    if not msg_ids:
        return [], False
    counts: dict[int, int] = defaultdict(int)
    try:
        with db_session() as conn:
            for part in _chunks(msg_ids):
                for r in conn.execute(
                    select(M.ChatMessage.sentiment, func.count())
                    .where(
                        M.ChatMessage.msg_id.in_(part),
                        M.ChatMessage.sentiment.is_not(None),
                    )
                    .group_by(M.ChatMessage.sentiment)
                ).all():
                    counts[int(r[0])] += int(r[1] or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("统计咨询情绪失败：%s", exc)
        return [], False
    total = sum(counts.values())
    rows = [
        {
            "key": str(key),
            "label": SENTIMENT_LABELS.get(key, f"标签 {key}"),
            "count": n,
            "share": _ratio(n, total),
        }
        for key, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return rows, bool(rows)


def rankings(
    *,
    agent_id: int | None = None,
    channel_id: int | None = None,
    role: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int | None = DEFAULT_TOP,
) -> dict:
    pairs, scanned = _collect(
        agent_id=agent_id,
        channel_id=channel_id,
        role=role,
        date_from=date_from,
        date_to=date_to,
    )
    top = _top_args(limit)
    if settings.store_label == "mysql":
        docs, cats, citations = _mysql_knowledge(pairs)
    else:
        docs, cats, citations = _json_knowledge(pairs)
    devices, device_tracked = _device_rows(_session_ids(pairs))
    sentiments, sentiment_ready = _sentiment_rows(pairs)

    notes: dict[str, str] = {}
    if citations == 0:
        notes["knowledge"] = "当前筛选条件下没有引用记录，无法排名。"
    if not device_tracked:
        notes["devices"] = "暂无设备数据：设备自 3.10 起随新会话采集，历史会话显示为未采集。"
    if not sentiment_ready:
        notes["sentiments"] = "暂无情绪数据：等待 4.6 情绪打标接入。"
    notes["apps"] = "暂无应用访问数据：3.10 已预留统计接口，应用访问埋点尚未接入。"
    return {
        "knowledge": docs[:top],
        "knowledgeTotal": len(docs),
        "categories": cats[:top],
        "categoryTotal": len(cats),
        "citations": citations,
        "devices": devices,
        "sentiments": sentiments,
        "apps": [],
        "notes": notes,
        "window": _window(scanned),
        "store": settings.store_label,
    }


# ------------------------------------------------------------------ 导出
EXPORT_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "trend": [("日期", "date"), ("提问", "questions"), ("回复", "replies"),
              ("未答", "unanswered"), ("会话", "sessions")],
    "channels": [("渠道", "name"), ("提问", "questions"), ("回复", "replies"),
                 ("未答", "unanswered"), ("会话", "sessions"),
                 ("应答成功率", "successRate"), ("占比", "share")],
    "agents": [("助手", "name"), ("提问", "questions"), ("回复", "replies"),
               ("未答", "unanswered"), ("会话", "sessions"),
               ("应答成功率", "successRate"), ("占比", "share")],
    "knowledge": [("文档ID", "docId"), ("文档", "docTitle"), ("部门", "dept"),
                  ("类型", "ktype"), ("引用次数", "citations"),
                  ("会话数", "sessions"), ("占比", "share")],
    "categories": [("分类", "label"), ("引用次数", "citations"),
                   ("会话数", "sessions"), ("占比", "share")],
    "devices": [("设备", "label"), ("会话数", "count"), ("占比", "share")],
    "sentiments": [("情绪", "label"), ("消息数", "count"), ("占比", "share")],
    "apps": [("应用", "name"), ("访问次数", "count")],
    "summary": [("指标", "label"), ("数值", "value")],
}

SUMMARY_LABELS = [
    ("提问", "questions"),
    ("回复", "replies"),
    ("未答", "unanswered"),
    ("已答", "answered"),
    ("访问", "visits"),
    ("人次", "persons"),
    ("成功率", "successRate"),
]


def export_csv(
    kind: str,
    *,
    agent_id: int | None = None,
    channel_id: int | None = None,
    role: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> tuple[str, int]:
    """返回 ``(CSV 文本, 行数)``；kind 非法时抛 ``ValueError``。"""
    if kind not in EXPORT_COLUMNS:
        raise ValueError(f"不支持的导出类型：{kind}")
    filters = dict(
        agent_id=agent_id, channel_id=channel_id, role=role,
        date_from=date_from, date_to=date_to,
    )
    if kind == "summary":
        pairs, _ = _collect(**filters)
        summary = _summary(pairs, _session_ids(pairs))
        rows = [{"label": label, "value": summary.get(key)} for label, key in SUMMARY_LABELS]
    elif kind in {"knowledge", "categories", "devices", "sentiments"}:
        data = rankings(**filters, limit=MAX_TOP)
        key = kind
        rows = data.get(key) or []
    else:
        data = overview(**filters)
        rows = data.get(kind) or []

    columns = EXPORT_COLUMNS[kind]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([name for name, _ in columns])
    for row in rows:
        writer.writerow(["" if row.get(key) is None else row.get(key) for _, key in columns])
    return "\ufeff" + buf.getvalue(), len(rows)
