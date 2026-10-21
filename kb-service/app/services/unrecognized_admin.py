"""未识别问题闭环服务（任务 3.11 / M-S-07～09）。

职责：

* **聚合**：同一归一化问题（``question_key``）合并为一行，给出频次、首末时间、
  角色 / 渠道 / 来源分布；
* **语义聚类**：对聚合后的代表问题做余弦聚类，把「怎么办校园卡」与
  「校园卡丢了怎么补办」归到同一簇（结果带进程内缓存，池子变化才重算）；
* **业务域分类**：关键词规则给出建议业务域，供运营批量归类；
* **追溯**：按记录回查原会话（用户原话 + AI 回答 + 引用）；
* **反哺**：把问题生成 Markdown 知识文档草稿（``status=0``），走 2.7 审核发布；
  发布后由 :mod:`app.core.index_store` 重建索引即可被检索命中。

存储层只做「读写池表」，本模块不重复扫描 ``chat_message``。
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import math
from datetime import datetime, timedelta

from app.config import settings
from app.core.embedder import get_embedder
from app.db import channel_store, chatlog, kb_repo, unrecognized_store
from app.services import kb_admin

logger = logging.getLogger(__name__)

MAX_IDS_PER_GROUP = 200
MAX_VARIANTS = 8
MAX_CLUSTER_INPUT = 200

_UNSET = object()
_cluster_cache: dict = {"sig": None, "result": None}

#: 业务域规则：命中关键词数最多者胜，同分按声明顺序（先声明优先级更高）
DOMAIN_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "教务教学",
        (
            "选课", "课表", "课程", "成绩", "考试", "补考", "缓考", "学分", "绩点",
            "毕业", "学位", "学籍", "培养方案", "重修", "辅修", "转专业", "休学",
            "复学", "论文", "答辩", "实习", "教务", "教室", "调课",
        ),
    ),
    (
        "学生事务",
        (
            "奖学金", "助学金", "助学贷款", "资助", "评优", "评先", "处分", "申诉",
            "社团", "宿舍", "寝室", "请假", "辅导员", "心理", "困难生", "勤工",
            "保险", "医保", "档案", "证明", "在校证明",
        ),
    ),
    (
        "财务缴费",
        (
            "学费", "住宿费", "缴费", "交费", "发票", "报销", "财务", "收费",
            "退款", "退费", "银行卡", "一卡通", "校园卡", "余额", "充值", "补助",
        ),
    ),
    (
        "后勤保障",
        (
            "食堂", "餐饮", "饭卡", "维修", "报修", "水电", "空调", "热水", "浴室",
            "校车", "班车", "物业", "快递", "超市", "医疗", "医院", "体检", "疫苗",
        ),
    ),
    (
        "网络信息",
        (
            "校园网", "网络", "网费", "wifi", "vpn", "邮箱", "账号", "密码",
            "认证", "上网", "信息化", "系统", "平台", "门户", "统一身份",
        ),
    ),
    (
        "招生就业",
        (
            "招生", "录取", "报到", "迎新", "就业", "招聘", "简历", "三方",
            "派遣", "考研", "保研", "创业", "秋招", "春招",
        ),
    ),
    (
        "图书馆",
        ("图书馆", "借书", "还书", "续借", "数据库", "文献", "查重", "自习", "座位", "馆藏"),
    ),
    (
        "安全保卫",
        ("门禁", "出入", "访客", "户籍", "身份证", "保卫", "消防", "报警", "停车", "车辆"),
    ),
    (
        "党政办公",
        ("公章", "用印", "公文", "会议", "场地", "审批", "一网通办", "组织", "党员", "工会"),
    ),
)

DOMAIN_OTHER = "其他"


def reset_cluster_cache() -> None:
    """清空聚类缓存（测试隔离 / 池重建后调用）。"""
    global _cluster_cache
    _cluster_cache = {"sig": None, "result": None}


def _ok(data) -> dict:
    return {"ok": True, "data": data, "error": None}


def _fail(message: str, status: int = 400) -> dict:
    return {"ok": False, "data": None, "error": message, "status": status}


_DATE_FMT = "%Y-%m-%d"


def _parse_day(value: str | None, *, end: bool = False) -> datetime | None:
    if not value:
        return None
    try:
        day = datetime.strptime(str(value)[:10], _DATE_FMT)
    except ValueError:
        return None
    return day + timedelta(days=1) - timedelta(seconds=1) if end else day


def _day_ok(created: str | None, start: datetime | None, end: datetime | None) -> bool:
    if not created:
        return start is None and end is None
    text = str(created)
    if start is not None and text < start.isoformat(timespec="seconds"):
        return False
    if end is not None and text > end.isoformat(timespec="seconds"):
        return False
    return True


def classify_domain(text: str) -> str:
    """关键词业务域分类：返回中文业务域名，无法判断时返回「其他」。"""
    raw = str(text or "").lower()
    best_name, best_hits = DOMAIN_OTHER, 0
    for name, words in DOMAIN_RULES:
        hits = sum(1 for word in words if word in raw)
        if hits > best_hits:
            best_name, best_hits = name, hits
    return best_name


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _cluster(question_keys: list[str], texts: list[str]) -> dict:
    """对问题组做语义聚类，返回 ``{key: {clusterId, clusterLabel}}`` 与簇摘要。"""
    global _cluster_cache
    threshold = float(settings.unrecognized_cluster_threshold)
    sig = (threshold, tuple(sorted(question_keys)))
    if _cluster_cache.get("sig") == sig:
        return _cluster_cache["result"]

    n = len(question_keys)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[max(ri, rj)] = min(ri, rj)

    try:
        vectors = get_embedder().encode(texts) if n > 1 else []
        for i in range(n):
            for j in range(i + 1, n):
                if _cos(vectors[i], vectors[j]) >= threshold:
                    union(i, j)
    except Exception as exc:  # noqa: BLE001 - 聚类失败降级为「每组独立」
        logger.warning("未识别问题聚类失败，已降级：%s", exc)
        parent = list(range(n))

    components: dict[int, list[int]] = {}
    for i in range(n):
        components.setdefault(find(i), []).append(i)

    mapping: dict[str, dict] = {}
    clusters: list[dict] = []
    for members in components.values():
        keys = sorted(question_keys[i] for i in members)
        rep_idx = max(members, key=lambda i: (len(texts[i]), -i))
        label = texts[rep_idx]
        cluster_id = "K" + hashlib.md5(keys[0].encode("utf-8")).hexdigest()[:8].upper()
        for i in members:
            mapping[question_keys[i]] = {"clusterId": cluster_id, "clusterLabel": label}
        clusters.append(
            {
                "clusterId": cluster_id,
                "clusterLabel": label,
                "questionCount": len(members),
                "questionKeys": keys,
            }
        )

    clusters.sort(key=lambda item: (-item["questionCount"], item["clusterId"]))
    result = (mapping, clusters)
    _cluster_cache = {"sig": sig, "result": result}
    return result


def _state_of(counts: dict[int, int]) -> int:
    if counts.get(unrecognized_store.STATE_PENDING):
        return unrecognized_store.STATE_PENDING
    if counts.get(unrecognized_store.STATE_FED_BACK):
        return unrecognized_store.STATE_FED_BACK
    return unrecognized_store.STATE_IGNORED


def _filter_records(
    records: list[dict],
    *,
    state: int | None = None,
    source: str | None = None,
    dept: str | None = None,
    role: str | None = None,
    channel_code: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    keyword: str | None = None,
) -> list[dict]:
    start = _parse_day(date_from)
    end = _parse_day(date_to, end=True)
    q = (keyword or "").strip().lower()
    out = []
    for rec in records:
        if state is not None and int(rec.get("state") or 0) != int(state):
            continue
        if source and rec.get("source") != source:
            continue
        if dept and rec.get("dept") != dept:
            continue
        if role and rec.get("role") != role:
            continue
        if channel_code and rec.get("channelCode") != channel_code:
            continue
        if not _day_ok(rec.get("createdAt"), start, end):
            continue
        if q and q not in str(rec.get("question") or "").lower():
            continue
        out.append(rec)
    return out


def _aggregate(records: list[dict]) -> list[dict]:
    """按归一化问题聚合；输入按时间倒序，输出同样按最近出现倒序。"""
    channel_names = {
        item["channelCode"]: item["channelName"] for item in channel_store.list_channels()
    }
    grouped: dict[str, dict] = {}
    for rec in records:
        key = rec.get("questionKey") or unrecognized_store.normalize_question(rec.get("question"))
        item = grouped.get(key)
        if item is None:
            item = {
                "questionKey": key,
                "question": rec.get("question") or "",
                "count": 0,
                "ids": [],
                "firstAt": rec.get("createdAt"),
                "lastAt": rec.get("createdAt"),
                "lastSessionId": rec.get("sessionId"),
                "lastMsgId": rec.get("msgId"),
                "roles": set(),
                "channels": set(),
                "sources": set(),
                "depts": set(),
                "confidences": [],
                "variants": [],
                "stateCounts": {},
            }
            grouped[key] = item
        item["count"] += 1
        if len(item["ids"]) < MAX_IDS_PER_GROUP:
            item["ids"].append(rec.get("id"))
        if rec.get("role"):
            item["roles"].add(rec["role"])
        if rec.get("channelCode"):
            item["channels"].add(rec["channelCode"])
        if rec.get("source"):
            item["sources"].add(rec["source"])
        if rec.get("dept"):
            item["depts"].add(rec["dept"])
        if rec.get("confidence") is not None:
            item["confidences"].append(float(rec["confidence"]))
        question = rec.get("question") or ""
        if question and question not in item["variants"] and len(item["variants"]) < MAX_VARIANTS:
            item["variants"].append(question)
        state = int(rec.get("state") or 0)
        item["stateCounts"][state] = item["stateCounts"].get(state, 0) + 1
        created = rec.get("createdAt") or ""
        if created and (not item["firstAt"] or created < item["firstAt"]):
            item["firstAt"] = created

    keys = list(grouped)
    texts = [grouped[key]["question"] for key in keys]
    mapping, clusters = _cluster(keys, texts)

    items = []
    for key, item in grouped.items():
        confs = item.pop("confidences")
        depts = sorted(item.pop("depts"))
        channels = sorted(item.pop("channels"))
        cluster = mapping.get(key) or {}
        domain = classify_domain(item["question"])
        items.append(
            {
                **item,
                "roles": sorted(item["roles"]),
                "channels": channels,
                "channelNames": [channel_names.get(code, code) for code in channels],
                "sources": sorted(item["sources"]),
                "sourceLabels": [
                    unrecognized_store.SOURCE_LABELS.get(src, src)
                    for src in sorted(item["sources"])
                ],
                "dept": depts[0] if depts else "",
                "avgConfidence": round(sum(confs) / len(confs), 4) if confs else None,
                "minConfidence": round(min(confs), 4) if confs else None,
                "state": _state_of(item["stateCounts"]),
                "clusterId": cluster.get("clusterId", ""),
                "clusterLabel": cluster.get("clusterLabel", ""),
                "domain": domain,
                "idsTruncated": item["count"] > len(item["ids"]),
            }
        )
    items.sort(key=lambda x: (x.get("lastAt") or "", x.get("count") or 0), reverse=True)
    return items, clusters


def list_questions(
    *,
    state: int | None = None,
    source: str | None = None,
    dept: str | None = None,
    role: str | None = None,
    channel_code: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    keyword: str | None = None,
    sort: str = "recent",
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """聚合清单（一行 = 一个归一化问题）。"""
    scanned = unrecognized_store.list_records(limit=unrecognized_store.MAX_SCAN)
    filtered = _filter_records(
        scanned,
        state=state,
        source=source,
        dept=dept,
        role=role,
        channel_code=channel_code,
        date_from=date_from,
        date_to=date_to,
        keyword=keyword,
    )
    items, clusters = _aggregate(filtered)
    if sort == "frequent":
        items.sort(key=lambda x: (x.get("count") or 0, x.get("lastAt") or ""), reverse=True)
    total = len(items)
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 200))
    start = (page - 1) * page_size
    return {
        "items": items[start : start + page_size],
        "total": total,
        "recordTotal": len(filtered),
        "page": page,
        "pageSize": page_size,
        "clusters": [c for c in clusters if c["questionCount"] > 1][:20],
        "threshold": settings.unrecognized_confidence_threshold,
        "enabled": settings.unrecognized_enabled,
        "store": settings.store_label,
        "feedbackAvailable": settings.store_label == "mysql",
        "window": {
            "scanned": len(scanned),
            "limit": unrecognized_store.MAX_SCAN,
            "truncated": len(scanned) >= unrecognized_store.MAX_SCAN,
        },
    }


def filter_options() -> dict:
    records = unrecognized_store.list_records(limit=unrecognized_store.MAX_SCAN)
    channels = [
        {"code": item["channelCode"], "name": item["channelName"]}
        for item in channel_store.list_channels()
    ]
    # 渠道被删除后，历史记录仍带着编码；筛选下拉必须能选到它，否则这部分数据
    # 在管理页上永远筛不出来。名称缺失时直接显示编码。
    known = {item["code"] for item in channels}
    for rec in records:
        code = rec.get("channelCode")
        if code and code not in known:
            channels.append({"code": code, "name": code})
            known.add(code)
    channels.sort(key=lambda item: item["code"])
    return {
        "states": [
            {"value": code, "label": label}
            for code, label in unrecognized_store.STATE_LABELS.items()
        ],
        "sources": [
            {"value": code, "label": label}
            for code, label in unrecognized_store.SOURCE_LABELS.items()
        ],
        "domains": [name for name, _ in DOMAIN_RULES] + [DOMAIN_OTHER],
        "depts": sorted({r["dept"] for r in records if r.get("dept")}),
        "roles": sorted({r["role"] for r in records if r.get("role")}),
        "channels": [c for c in channels if c["code"]],
        "threshold": settings.unrecognized_confidence_threshold,
        "enabled": settings.unrecognized_enabled,
        "store": settings.store_label,
        "feedbackAvailable": settings.store_label == "mysql",
    }


def _feedback_info(rec: dict) -> dict | None:
    doc_id = rec.get("newDocId")
    if not doc_id:
        return None
    info = {"docId": doc_id, "chunkId": rec.get("newChunkId")}
    if settings.store_label == "mysql":
        try:
            doc = kb_repo.get_doc(int(doc_id))
        except Exception:  # noqa: BLE001 - 溯源失败不应让详情整体不可用
            doc = None
        if doc:
            info.update(
                {
                    "title": doc["title"],
                    "status": doc["status"],
                    "statusName": doc["statusName"],
                    "publicId": kb_repo.doc_public_id(int(doc_id)),
                }
            )
    return info


def detail(record_id: int) -> dict:
    rec = unrecognized_store.get_record(record_id)
    if rec is None:
        return _fail(f"未识别问题记录不存在：{record_id}", status=404)

    session = None
    question_msg = None
    answer_msg = None
    if rec.get("sessionId"):
        try:
            session = chatlog.load_session(rec["sessionId"])
        except Exception as exc:  # noqa: BLE001 - 会话可能已被清理
            logger.warning("回溯会话失败（%s）：%s", rec["sessionId"], exc)
    if session:
        messages = session.get("messages") or []
        if rec.get("msgId") is not None:
            answer_msg = next(
                (m for m in messages if m.get("msgId") == rec["msgId"]), None
            )
        if answer_msg is None:
            answer_msg = next((m for m in reversed(messages) if m.get("role") == "ai"), None)
        if answer_msg is not None:
            idx = messages.index(answer_msg)
            question_msg = next(
                (m for m in reversed(messages[:idx]) if m.get("role") == "user"), None
            )

    variants = [
        r
        for r in unrecognized_store.list_records(limit=unrecognized_store.MAX_SCAN)
        if (r.get("questionKey") or "") == (rec.get("questionKey") or "")
    ]
    domain = classify_domain(rec.get("question") or "")
    return _ok(
        {
            "record": rec,
            "domain": domain,
            "session": (
                {
                    "sessionId": session.get("sessionId"),
                    "role": session.get("role"),
                    "createdAt": session.get("createdAt"),
                    "messageCount": len(session.get("messages") or []),
                }
                if session
                else None
            ),
            "questionMessage": question_msg,
            "answerMessage": answer_msg,
            "variants": variants[:20],
            "variantCount": len(variants),
            "feedback": _feedback_info(rec),
        }
    )


def batch(ids: list[int] | None, question_keys: list[str] | None, action: str, **kwargs) -> dict:
    """批量处理：忽略 / 重新打开 / 归类（写归属部门）。"""
    target_ids = [int(i) for i in (ids or []) if str(i).strip().isdigit()]
    keys = {str(k) for k in (question_keys or []) if str(k).strip()}
    if keys:
        for rec in unrecognized_store.list_records(limit=unrecognized_store.MAX_SCAN):
            if (rec.get("questionKey") or "") in keys:
                target_ids.append(int(rec["id"]))
    target_ids = sorted(set(target_ids))
    if not target_ids:
        return _fail("请至少选择一条未识别问题")

    handler_id = kwargs.get("handler_id")
    if action == "ignore":
        changed = unrecognized_store.update_records(
            target_ids, state=unrecognized_store.STATE_IGNORED, handler_id=handler_id
        )
    elif action == "reopen":
        changed = unrecognized_store.update_records(
            target_ids, state=unrecognized_store.STATE_PENDING, handler_id=handler_id
        )
    elif action == "classify":
        dept = str(kwargs.get("dept") or "").strip()
        if not dept:
            return _fail("归类需要指定部门")
        changed = unrecognized_store.update_records(
            target_ids, dept=dept, handler_id=handler_id
        )
    else:
        return _fail(f"未知操作：{action}")
    if not changed:
        return _fail("没有记录被更新", status=404)
    return _ok({"action": action, "updated": changed, "ids": target_ids})


_FEEDBACK_TEMPLATE = """# {title}

> 来源：未识别问题反哺（记录 {ids}，共出现 {count} 次）
> 处理状态：待补充标准答案，补齐后提交审核。

## 用户问题

{question}

## 标准答案（待补充）

请在此处补充标准答案、办理条件与入口链接，然后在「知识文档」中提交审核并发布。
"""


def _feedback_records(ids: list[int] | None, question_key: str | None) -> list[dict]:
    if question_key:
        return [
            r
            for r in unrecognized_store.list_records(limit=unrecognized_store.MAX_SCAN)
            if (r.get("questionKey") or "") == question_key
        ]
    out = []
    for rid in ids or []:
        rec = unrecognized_store.get_record(int(rid))
        if rec:
            out.append(rec)
    return out


def feedback(
    *,
    ids: list[int] | None = None,
    question_key: str | None = None,
    title: str | None = None,
    content: str | None = None,
    dept: str | None = None,
    ktype: str = "faq",
    visible_roles: list[str] | None = None,
    handler_id=None,
) -> dict:
    """一键反哺：生成知识文档草稿（不直接发布，必须走 2.7 审核）。

    JSON 模式的知识库是只读静态索引，无法创建草稿，返回 400 并给出指引。
    """
    if settings.store_label != "mysql":
        return _fail(
            "反哺知识库需要 MySQL 知识存储：请设置 KB_STORE=mysql 并启动数据库后重试"
            "（JSON 模式的知识库是只读静态索引）"
        )
    records = _feedback_records(ids, question_key)
    if not records:
        return _fail("未找到可反哺的未识别问题", status=404)
    if any(r.get("newDocId") for r in records):
        return _fail("该问题已反哺过，请勿重复创建草稿", status=409)

    primary = max(records, key=lambda r: int(r.get("id") or 0))
    question = primary.get("question") or ""
    doc_title = (title or question).strip()[:120] or "未识别问题反哺"
    body = (content or "").strip() or _FEEDBACK_TEMPLATE.format(
        title=doc_title,
        ids="、".join(str(r["id"]) for r in records[:20]),
        count=len(records),
        question=question,
    )
    if not body.startswith("#"):
        body = f"# {doc_title}\n\n{body}"
    dept_name = (dept or primary.get("dept") or classify_domain(question) or "未分类").strip()

    result = kb_admin.ingest_uploaded(
        file_buf=body.encode("utf-8"),
        filename=f"未识别问题-{primary['id']}.md",
        dept=dept_name,
        ktype=ktype,
        status=0,  # 草稿：必须经 2.7 审核发布后才会进入检索索引
        title=doc_title,
        visible_roles=visible_roles or ["全部"],
    )
    if not result.get("ok"):
        return _fail(result.get("error") or "创建知识草稿失败")

    doc_id = int(result["docId"])
    chunk_id = None
    try:
        chunks = kb_repo.list_chunks(doc_id)
        chunk_id = chunks[0]["chunkId"] if chunks else None
    except Exception:  # noqa: BLE001 - 切片回填失败不影响反哺本身
        logger.warning("反哺草稿切片回读失败：doc_id=%s", doc_id)

    target_ids = [int(r["id"]) for r in records]
    unrecognized_store.update_records(
        target_ids,
        state=unrecognized_store.STATE_FED_BACK,
        dept=dept_name,
        handler_id=handler_id,
        new_doc_id=doc_id,
        new_chunk_id=chunk_id,
    )
    return _ok(
        {
            "docId": doc_id,
            "publicId": kb_repo.doc_public_id(doc_id),
            "chunkId": chunk_id,
            "title": doc_title,
            "dept": dept_name,
            "ktype": ktype,
            "status": 0,
            "statusName": "草稿",
            "updated": len(target_ids),
            "ids": target_ids,
        }
    )


_EXPORT_HEADER = [
    "问题",
    "频次",
    "首次出现",
    "最近出现",
    "来源",
    "角色",
    "渠道",
    "归属部门",
    "业务域",
    "语义簇",
    "状态",
    "典型会话",
]


def export_csv(**filters) -> tuple[str, int, bool]:
    """按当前筛选导出聚合清单（UTF-8 BOM）；返回 ``(csv, rows, truncated)``。"""
    scanned = unrecognized_store.list_records(limit=unrecognized_store.MAX_SCAN)
    records = _filter_records(
        scanned,
        state=filters.get("state"),
        source=filters.get("source"),
        dept=filters.get("dept"),
        role=filters.get("role"),
        channel_code=filters.get("channel_code"),
        date_from=filters.get("date_from"),
        date_to=filters.get("date_to"),
        keyword=filters.get("keyword"),
    )
    items, _ = _aggregate(records)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_EXPORT_HEADER)
    for item in items:
        writer.writerow(
            [
                item["question"],
                item["count"],
                item.get("firstAt") or "",
                item.get("lastAt") or "",
                "、".join(item.get("sourceLabels") or []),
                "、".join(item.get("roles") or []),
                "、".join(item.get("channelNames") or []),
                item.get("dept") or "",
                item.get("domain") or "",
                item.get("clusterLabel") or "",
                unrecognized_store.STATE_LABELS.get(item["state"], ""),
                item.get("lastSessionId") or "",
            ]
        )
    return "\ufeff" + buf.getvalue(), len(items), len(scanned) >= unrecognized_store.MAX_SCAN
