"""问答分享与用户反馈服务（任务 4.9 / P-QA-07·08）。

* **分享**：按「会话 + AI 消息」在服务端取问题/回答快照，生成不可枚举的
  短链；公开页与卡片共用同一份快照，原会话清理后链接仍可打开。
* **反馈**：点赞/点踩挂在 AI 消息上，点踩必须选择原因；同一条消息再次反馈
  覆盖原记录，管理端不重复处理。
* **后台**：按评价 / 处理状态 / 时间 / 关键词筛选，CSV 导出与处理留痕。

安全边界：分享只暴露创建者主动选择的那一条问答，回答 HTML 由
:func:`app.core.richtext.render_html` 服务端渲染（不信任客户端 Markdown），
公开接口不返回 ``chunkId`` 等内部定位信息。
"""

from __future__ import annotations

import csv
import io
import logging
import re
from datetime import datetime
from urllib.parse import quote

from app.config import settings
from app.core import ai_label
from app.core.richtext import render_html
from app.db import agent_store, chatlog, share_store

logger = logging.getLogger(__name__)

MAX_EXCERPT = 240
EXPORT_LIMIT = share_store.EXPORT_LIMIT

_WS = re.compile(r"\s+")
_MARK = re.compile(r"[#>*_`~\[\]()!-]")


def _ok(data) -> dict:
    return {"ok": True, "data": data}


def _fail(status: int, error: str) -> dict:
    return {"ok": False, "status": status, "error": error}


def _snapshot(session: dict, msg_id: int) -> dict | None:
    """从会话中取「AI 回答 + 其前一条用户问题」；消息不是 AI 时返回 None。"""
    messages = session.get("messages") or []
    index = None
    for i, msg in enumerate(messages):
        if int(msg.get("msgId") or -1) == int(msg_id):
            index = i
            break
    if index is None:
        return None
    answer_msg = messages[index]
    if answer_msg.get("role") != "ai":
        return None
    question = ""
    for msg in reversed(messages[:index]):
        if msg.get("role") == "user":
            question = msg.get("content") or ""
            break
    return {
        "question": question,
        "answer": answer_msg.get("content") or "",
        "citations": list(answer_msg.get("citations") or []),
    }


def _excerpt(text: str, limit: int = MAX_EXCERPT) -> str:
    plain = _MARK.sub(" ", _WS.sub(" ", str(text or ""))).strip()
    return plain[:limit]


def _public_citations(citations: list) -> list[dict]:
    out = []
    for i, raw in enumerate(citations or []):
        if not isinstance(raw, dict):
            continue
        out.append(
            {
                "n": int(raw.get("n") or i + 1),
                "title": raw.get("titlePath") or raw.get("docTitle") or "",
                "dept": raw.get("dept") or "",
                "ktype": raw.get("ktype") or "",
                "similarity": (
                    float(raw["similarity"]) if raw.get("similarity") is not None else None
                ),
            }
        )
    return out


def _agent_name(session: dict, fallback: str = "") -> str:
    raw = session.get("agentId")
    if raw:
        try:
            agent = agent_store.get_agent(int(raw))
            if agent:
                return agent.get("agentName") or fallback
        except Exception as exc:  # noqa: BLE001 - 名称只是展示字段
            logger.debug("读取助手名称失败：%s", exc)
    return fallback


# ------------------------------------------------------------------ 分享
def create_share(
    session_id: str,
    msg_id: int,
    *,
    base_url: str,
    agent_name: str = "",
    channel_code: str = "",
) -> dict:
    if not session_id or msg_id is None:
        return _fail(400, "缺少会话或消息标识")
    session = chatlog.load_session(session_id)
    if not session:
        return _fail(404, "会话不存在或已失效，请刷新页面后重试")
    snapshot = _snapshot(session, int(msg_id))
    if not snapshot:
        return _fail(404, "该消息不是 AI 回答或已不存在")
    if not snapshot["answer"].strip():
        return _fail(400, "该回答为空，无法分享")

    record = share_store.create_share(
        session_id=session_id,
        msg_id=int(msg_id),
        question=snapshot["question"],
        answer=snapshot["answer"],
        citations=snapshot["citations"],
        agent_name=_agent_name(session, agent_name or "")[:128],
        channel_code=(channel_code or session.get("channelCode") or "")[:64],
        role=session.get("role") or "",
    )
    base = (settings.public_base_url or base_url or "").rstrip("/")
    return _ok(
        {
            "shareId": record["shareId"],
            "url": f"{base}/share/{record['shareId']}",
            "reused": bool(record.get("reused")),
            "question": record["question"],
        }
    )


def get_public_share(share_id: str, *, count_view: bool = True) -> dict | None:
    record = share_store.get_share(share_id)
    if not record:
        return None
    if count_view:
        share_store.increment_views(share_id)
        record["views"] = int(record.get("views") or 0) + 1
    counts = share_store.share_feedback_counts(share_id)
    return {
        "shareId": record["shareId"],
        "question": record["question"],
        "answer": record["answer"],
        "answerHtml": render_html(record["answer"]),
        "citations": _public_citations(record["citations"]),
        "agentName": record["agentName"],
        "role": record["role"],
        "channelCode": record["channelCode"],
        "views": record["views"],
        "createdAt": record["createdAt"],
        "feedback": counts,
        "aiGenerated": True,
        "aiLabel": ai_label.build_label(
            session_id=record["sessionId"], msg_id=record.get("msgId")
        ),
    }


# ------------------------------------------------------------------ 反馈
def submit_feedback(
    *,
    rating: int,
    share_id: str = "",
    session_id: str = "",
    msg_id: int | None = None,
    reason_code: str = "",
    reason: str = "",
    channel_code: str = "",
) -> dict:
    """点赞/点踩。点踩必须给出原因；问题与回答一律服务端取快照。"""
    if int(rating) not in (share_store.RATING_LIKE, share_store.RATING_DISLIKE):
        return _fail(400, "评价只能是点赞或点踩")
    if int(rating) == share_store.RATING_DISLIKE:
        if not reason_code:
            return _fail(400, "点踩需选择原因")
        if reason_code not in share_store.REASON_CODES:
            return _fail(400, "不支持的反馈原因")
    else:
        reason_code = ""
        reason = ""

    question = ""
    answer = ""
    role = ""
    channel = channel_code
    share_ref = ""

    if share_id:
        share = share_store.get_share(share_id)
        if not share:
            return _fail(404, "分享不存在或已失效")
        share_ref = share_id
        session_id = session_id or share["sessionId"]
        msg_id = msg_id if msg_id is not None else share.get("msgId")
        question = share["question"]
        answer = share["answer"]
        role = share["role"]
        channel = channel or share["channelCode"]
    elif session_id and msg_id is not None:
        session = chatlog.load_session(session_id)
        if not session:
            return _fail(404, "会话不存在或已失效")
        snapshot = _snapshot(session, int(msg_id))
        if not snapshot:
            return _fail(404, "该消息不是 AI 回答或已不存在")
        question = snapshot["question"]
        answer = snapshot["answer"]
        role = session.get("role") or ""
        channel = channel or session.get("channelCode") or ""
    else:
        return _fail(400, "缺少分享或消息标识")

    item = share_store.upsert_feedback(
        rating=int(rating),
        share_id=share_ref,
        session_id=session_id or "",
        msg_id=int(msg_id) if msg_id is not None else None,
        reason_code=reason_code,
        reason=reason,
        question=question,
        answer_excerpt=_excerpt(answer),
        role=role,
        channel_code=channel,
    )
    return _ok(
        {
            "feedbackId": item.get("feedbackId"),
            "rating": int(rating),
            "ratingLabel": share_store.RATING_LABELS[int(rating)],
            "message": "感谢反馈" if int(rating) > 0 else "已记录，我们会尽快改进",
        }
    )


# ------------------------------------------------------------------ 后台
def filter_options() -> dict:
    return share_store.filter_options() | {
        "reasonLabels": dict(share_store.REASON_CODES)
    }


def list_feedback(
    *,
    rating: int | None = None,
    handled: int | None = None,
    keyword: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    return share_store.list_feedback(
        rating=rating,
        handled=handled,
        keyword=keyword,
        date_from=date_from,
        date_to=date_to,
        page=page,
        page_size=page_size,
    )


def get_feedback(feedback_id: int) -> dict | None:
    return share_store.get_feedback(feedback_id)


def handle_feedback(feedback_id: int, *, handled: bool, note: str = "") -> dict:
    item = share_store.update_feedback(feedback_id, handled=handled, note=note)
    if item is None:
        return _fail(404, "反馈不存在")
    return _ok(item)


CSV_COLUMNS = [
    ("反馈ID", lambda r: r.get("feedbackId")),
    ("反馈时间", lambda r: r.get("createdAt")),
    ("评价", lambda r: r.get("ratingLabel")),
    ("原因", lambda r: r.get("reasonLabel")),
    ("补充说明", lambda r: r.get("reason")),
    ("问题", lambda r: r.get("question")),
    ("回答摘要", lambda r: r.get("answerExcerpt")),
    ("角色", lambda r: r.get("role")),
    ("渠道", lambda r: r.get("channelCode")),
    ("处理状态", lambda r: "已处理" if r.get("handled") else "待处理"),
    ("处理备注", lambda r: r.get("handleNote")),
    ("处理时间", lambda r: r.get("handledAt")),
    ("分享ID", lambda r: r.get("shareId")),
    ("会话ID", lambda r: r.get("sessionId")),
    ("消息ID", lambda r: r.get("msgId")),
]


def export_csv(
    *,
    rating: int | None = None,
    handled: int | None = None,
    keyword: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> tuple[bytes, int, bool]:
    result = share_store.list_feedback(
        rating=rating,
        handled=handled,
        keyword=keyword,
        date_from=date_from,
        date_to=date_to,
        page=1,
        page_size=EXPORT_LIMIT,
    )
    rows = result["items"]
    truncated = int(result["total"]) > len(rows)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([title for title, _ in CSV_COLUMNS])
    for row in rows:
        writer.writerow([fn(row) for _, fn in CSV_COLUMNS])
    return ("\ufeff" + buffer.getvalue()).encode("utf-8"), len(rows), truncated


def export_filename() -> str:
    return f"问答反馈-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"


def quoted_filename(name: str) -> str:
    return f"attachment; filename*=UTF-8''{quote(name)}"
