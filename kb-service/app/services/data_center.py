"""数据中台对接（任务 4.2 / I-02、M-SYS-02）。

交付范围（外部平台未就绪，mock 先行）：

1. **聊天日志 / 统计上报**：按自然日生成 ``nxz.data-center.report.v1`` 批次，
   同一 ``batchId`` + 相同内容重复执行只记 ``skipped``，内容变化才升 revision；
   mock 模式写入 ``data/data_center_outbox`` 作为收件箱，http 模式 POST 到数据中台。
2. **组织架构 / 用户同步**：``mock`` 使用确定性示例数据；http 模式从
   ``{DATA_CENTER_BASE_URL}{DATA_CENTER_ORG_PATH}`` 拉取。按 ``orgCode`` / ``userNo``
   upsert，重复执行不产生重复记录；全量同步只统计缺项，不删除或停用本地账号。
3. **定时任务**：每日统计上报、每日组织增量、每周组织全量。可由进程内调度线程
   触发（``DATA_CENTER_SCHEDULER_ENABLED=1``），也可由外部调度器调用管理接口。

数据标准与隐私：

* 上报字段固定、带 ``schema`` 版本号与校验和，接收方可按 ``batchId`` 幂等落库；
* 默认**不上报问答正文**，只上报元数据与聚合统计；开启
  ``DATA_CENTER_INCLUDE_CONTENT=1`` 时正文先经 :func:`masking.mask_text` 脱敏并截断；
* 组织同步不接收手机号/证件号等敏感字段，敏感属性仍以统一身份认证为准。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

from app.config import settings
from app.db import data_center_store as store
from app.security.masking import mask_text
from app.services import chatlog_admin

logger = logging.getLogger(__name__)

SCHEMA = "nxz.data-center.report.v1"
KINDS = ("chatlog", "stats", "all")
SYNC_MODES = ("incremental", "full")
CONTENT_LIMIT = 500

#: 示例组织与用户（待替换为学校真实数据）
MOCK_ORGS: tuple[dict, ...] = (
    {
        "orgCode": "XUST",
        "orgName": "西安科技大学",
        "parentCode": "",
        "orgPath": "/XUST/",
        "sortNo": 1,
        "status": 1,
        "updatedAt": "2026-09-01T08:00:00",
    },
    {
        "orgCode": "JW",
        "orgName": "教务处",
        "parentCode": "XUST",
        "orgPath": "/XUST/JW/",
        "sortNo": 10,
        "status": 1,
        "updatedAt": "2026-09-01T08:00:00",
    },
    {
        "orgCode": "XSGZB",
        "orgName": "学生工作部",
        "parentCode": "XUST",
        "orgPath": "/XUST/XSGZB/",
        "sortNo": 20,
        "status": 1,
        "updatedAt": "2026-09-01T08:00:00",
    },
    {
        "orgCode": "XXZX",
        "orgName": "信息网络中心",
        "parentCode": "XUST",
        "orgPath": "/XUST/XXZX/",
        "sortNo": 30,
        "status": 1,
        "updatedAt": "2026-09-01T08:00:00",
    },
    {
        "orgCode": "CS",
        "orgName": "计算机科学与技术学院",
        "parentCode": "XUST",
        "orgPath": "/XUST/CS/",
        "sortNo": 40,
        "status": 1,
        "updatedAt": "2026-09-01T08:00:00",
    },
    {
        "orgCode": "LIB",
        "orgName": "图书馆",
        "parentCode": "XUST",
        "orgPath": "/XUST/LIB/",
        "sortNo": 50,
        "status": 1,
        "updatedAt": "2026-09-01T08:00:00",
    },
)

MOCK_USERS: tuple[dict, ...] = (
    {
        "userNo": "20230001",
        "realName": "张三",
        "idType": 1,
        "orgCode": "CS",
        "email": "zhangsan@example.edu.cn",
        "status": 1,
        "updatedAt": "2026-09-01T08:05:00",
    },
    {
        "userNo": "20210002",
        "realName": "李四",
        "idType": 2,
        "orgCode": "CS",
        "email": "lisi@example.edu.cn",
        "status": 1,
        "updatedAt": "2026-09-01T08:05:00",
    },
    {
        "userNo": "T2019003",
        "realName": "王老师",
        "idType": 3,
        "orgCode": "JW",
        "email": "wang@example.edu.cn",
        "status": 1,
        "updatedAt": "2026-09-01T08:05:00",
    },
    {
        "userNo": "R2018004",
        "realName": "赵老师",
        "idType": 4,
        "orgCode": "LIB",
        "email": "zhao@example.edu.cn",
        "status": 1,
        "updatedAt": "2026-09-01T08:05:00",
    },
)


def _fail(status: int, message: str) -> dict:
    return {"ok": False, "data": None, "error": message, "status": status}


def _now() -> datetime:
    return datetime.now()


def _default_day() -> str:
    return (_now().date() - timedelta(days=1)).isoformat()


def _validate_day(raw: str | None) -> str:
    if not raw:
        return _default_day()
    try:
        day = date.fromisoformat(str(raw))
    except ValueError as exc:
        raise ValueError("date 需为 YYYY-MM-DD") from exc
    if day > _now().date():
        raise ValueError("不能上报未来日期")
    return day.isoformat()


def _canonical(payload: dict) -> str:
    body = {k: v for k, v in payload.items() if k not in ("generatedAt", "revision")}
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _checksum(payload: dict) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ 上报
def _records(day: str) -> tuple[list[dict], bool]:
    rows, truncated = chatlog_admin.export_rows("qa", date_from=day, date_to=day)
    if len(rows) > settings.data_center_max_records:
        rows = rows[: settings.data_center_max_records]
        truncated = True
    return rows, truncated


def _chatlog_record(row: dict) -> dict:
    record = {
        "sessionId": row.get("sessionId"),
        "askedAt": row.get("askedAt"),
        "answeredAt": row.get("answeredAt"),
        "role": row.get("role"),
        "agentName": row.get("agentName"),
        "channelName": row.get("channelName"),
        "refused": bool(row.get("refused")),
        "confidence": row.get("confidence"),
        "citationCount": row.get("citationCount"),
        "model": row.get("model"),
        "latencyMs": row.get("latencyMs"),
        "tokens": {
            "prompt": row.get("promptTokens"),
            "completion": row.get("completionTokens"),
            "total": row.get("totalTokens"),
        },
    }
    if settings.data_center_include_content:
        record["question"] = mask_text(str(row.get("question") or ""))[:CONTENT_LIMIT]
        record["answer"] = mask_text(str(row.get("answer") or ""))[:CONTENT_LIMIT]
    return record


def _statistics(rows: list[dict]) -> dict:
    sessions = {r.get("sessionId") for r in rows if r.get("sessionId")}
    refused = sum(1 for r in rows if r.get("refused"))
    latencies = [r["latencyMs"] for r in rows if isinstance(r.get("latencyMs"), (int, float))]
    tokens = [r["totalTokens"] for r in rows if isinstance(r.get("totalTokens"), (int, float))]
    by_role: dict[str, int] = {}
    by_channel: dict[str, int] = {}
    by_agent: dict[str, int] = {}
    for row in rows:
        by_role[str(row.get("role") or "访客")] = by_role.get(str(row.get("role") or "访客"), 0) + 1
        channel = str(row.get("channelName") or "未标注")
        by_channel[channel] = by_channel.get(channel, 0) + 1
        agent = str(row.get("agentName") or "未标注")
        by_agent[agent] = by_agent.get(agent, 0) + 1
    answers = len(rows)
    return {
        "sessions": len(sessions),
        "questions": len(rows),
        "answers": answers,
        "refused": refused,
        "successRate": round((answers - refused) / answers, 4) if answers else None,
        "avgLatencyMs": round(sum(latencies) / len(latencies), 1) if latencies else None,
        "totalTokens": sum(tokens) if tokens else 0,
        "byRole": [{"name": k, "count": v} for k, v in sorted(by_role.items())],
        "byChannel": [{"name": k, "count": v} for k, v in sorted(by_channel.items())],
        "byAgent": [{"name": k, "count": v} for k, v in sorted(by_agent.items())],
    }


def _build_payload(day: str, kind: str, rows: list[dict]) -> dict:
    batch_id = f"DC-{kind.upper()}-{day.replace('-', '')}"
    payload = {
        "schema": SCHEMA,
        "batchId": batch_id,
        "kind": kind,
        "date": day,
        "revision": 1,
        "generatedAt": _now().isoformat(timespec="seconds"),
        "source": {
            "system": "nengxiaozhi",
            "service": "kb-service",
            "version": settings.app_version,
        },
        "privacy": {
            "contentIncluded": bool(settings.data_center_include_content),
            "masking": "idCard/phone/studentNo/email",
        },
        "counts": {"records": len(rows)},
    }
    if kind == "chatlog":
        payload["records"] = [_chatlog_record(r) for r in rows]
    else:
        payload["statistics"] = _statistics(rows)
    return payload


def _deliver(payload: dict) -> str:
    """交付批次；返回投递位置。mock 落收件箱，http POST 到数据中台。"""
    if settings.data_center_provider == "mock":
        return store.write_sink(payload["batchId"], payload)
    url = settings.data_center_base_url.rstrip("/") + settings.data_center_report_path
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {settings.data_center_token}",
            "X-Data-Standard": SCHEMA,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=settings.data_center_timeout) as resp:
            if not 200 <= int(resp.status) < 300:
                raise RuntimeError(f"数据中台返回 HTTP {resp.status}")
            resp.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"数据中台返回 HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"数据中台不可达：{exc.reason}") from exc
    return url


def _deliver_kind(day: str, kind: str, rows: list[dict], force: bool = False) -> dict:
    payload = _build_payload(day, kind, rows)
    checksum = _checksum(payload)
    existing = store.get_report(payload["batchId"])
    if (
        existing
        and existing.get("status") == "delivered"
        and existing.get("checksum") == checksum
        and not force
    ):
        return {
            "kind": kind,
            "batchId": payload["batchId"],
            "revision": int(existing.get("revision") or 1),
            "checksum": checksum,
            "status": "delivered",
            "skipped": True,
            "rows": int(existing.get("rows") or 0),
            "location": "",
        }
    revision = int((existing or {}).get("revision") or 1)
    if existing and existing.get("checksum") != checksum:
        revision += 1
    payload["revision"] = revision
    payload["generatedAt"] = _now().isoformat(timespec="seconds")
    try:
        location = _deliver(payload)
    except Exception as exc:  # noqa: BLE001 - 失败要落批次状态，供下次重试
        store.save_report(
            batch_id=payload["batchId"],
            kind=kind,
            report_date=day,
            checksum=checksum,
            payload=payload,
            rows_count=len(rows),
            status="failed",
            error=str(exc),
            revision=revision,
        )
        return {
            "kind": kind,
            "batchId": payload["batchId"],
            "revision": revision,
            "checksum": checksum,
            "status": "failed",
            "skipped": False,
            "rows": len(rows),
            "error": str(exc),
            "location": "",
        }
    store.save_report(
        batch_id=payload["batchId"],
        kind=kind,
        report_date=day,
        checksum=checksum,
        payload=payload,
        rows_count=len(rows),
        status="delivered",
        revision=revision,
    )
    return {
        "kind": kind,
        "batchId": payload["batchId"],
        "revision": revision,
        "checksum": checksum,
        "status": "delivered",
        "skipped": False,
        "rows": len(rows),
        "location": location,
    }


def preview_report(day: str | None = None, kind: str = "all") -> dict:
    if not settings.data_center_enabled:
        return _fail(503, "数据中台未启用")
    try:
        day = _validate_day(day)
    except ValueError as exc:
        return _fail(400, str(exc))
    if kind not in KINDS:
        return _fail(400, "kind 只能是 chatlog / stats / all")
    rows, truncated = _records(day)
    kinds = ("chatlog", "stats") if kind == "all" else (kind,)
    items = []
    for item_kind in kinds:
        payload = _build_payload(day, item_kind, rows)
        items.append(
            {
                "kind": item_kind,
                "batchId": payload["batchId"],
                "checksum": _checksum(payload),
                "rows": len(rows),
                "counts": payload["counts"],
                "statistics": payload.get("statistics"),
                "sample": (payload.get("records") or [])[:3],
            }
        )
    return {
        "ok": True,
        "data": {
            "date": day,
            "schema": SCHEMA,
            "includeContent": settings.data_center_include_content,
            "truncated": truncated,
            "items": items,
        },
        "error": None,
        "status": 200,
    }


def run_report(day: str | None = None, kind: str = "all", force: bool = False) -> dict:
    if not settings.data_center_enabled:
        return _fail(503, "数据中台未启用")
    try:
        day = _validate_day(day)
    except ValueError as exc:
        return _fail(400, str(exc))
    if kind not in KINDS:
        return _fail(400, "kind 只能是 chatlog / stats / all")
    job_id = store.create_job("report", kind, {"date": day, "kind": kind, "force": bool(force)})
    try:
        rows, truncated = _records(day)
        kinds = ("chatlog", "stats") if kind == "all" else (kind,)
        results = [_deliver_kind(day, k, rows, force=force) for k in kinds]
        failed = [r for r in results if r["status"] != "delivered"]
        delivered = [r for r in results if r["status"] == "delivered" and not r["skipped"]]
        skipped = [r for r in results if r["skipped"]]
        checksum = hashlib.sha256(
            "|".join(f"{r['batchId']}:{r['checksum']}" for r in results).encode("utf-8")
        ).hexdigest()
        detail = {
            "date": day,
            "kind": kind,
            "truncated": truncated,
            "includeContent": settings.data_center_include_content,
            "batches": results,
        }
        store.finish_job(
            job_id,
            "success" if not failed else "failed",
            total=sum(int(r.get("rows") or 0) for r in results),
            created=len(delivered),
            skipped=len(skipped),
            failed=len(failed),
            checksum=checksum,
            detail=detail,
            error="; ".join(str(r.get("error") or "") for r in failed)[:512],
        )
        store.set_state("lastReportAt", _now().isoformat(timespec="seconds"))
        if failed:
            return _fail(502, failed[0].get("error") or "上报失败")
        return {
            "ok": True,
            "data": {"jobId": job_id, **detail},
            "error": None,
            "status": 200,
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("数据中台上报失败")
        store.finish_job(job_id, "failed", error=f"{type(exc).__name__}: {exc}")
        return _fail(500, f"上报失败：{exc}")


# ------------------------------------------------------------------ 同步
def _normalize_orgs(items: list) -> tuple[list[dict], int]:
    out: list[dict] = []
    invalid = 0
    for item in items:
        if not isinstance(item, dict) or not str(item.get("orgCode") or "").strip():
            invalid += 1
            continue
        try:
            out.append(
                {
                    "orgCode": str(item["orgCode"]).strip(),
                    "orgName": str(item.get("orgName") or "").strip(),
                    "parentCode": str(item.get("parentCode") or "").strip(),
                    "orgPath": str(item.get("orgPath") or "").strip(),
                    "sortNo": int(item.get("sortNo") or 0),
                    "status": 1 if int(item.get("status") if item.get("status") is not None else 1) else 0,
                    "updatedAt": str(item.get("updatedAt") or ""),
                }
            )
        except (TypeError, ValueError):
            invalid += 1
    return out, invalid


def _normalize_users(items: list) -> tuple[list[dict], int]:
    out: list[dict] = []
    invalid = 0
    for item in items:
        if not isinstance(item, dict) or not str(item.get("userNo") or "").strip():
            invalid += 1
            continue
        try:
            id_type = int(item.get("idType") or 1)
            out.append(
                {
                    "userNo": str(item["userNo"]).strip(),
                    "realName": str(item.get("realName") or "").strip(),
                    "idType": id_type if id_type in (1, 2, 3, 4, 5) else 1,
                    "orgCode": str(item.get("orgCode") or "").strip(),
                    "email": str(item.get("email") or "").strip(),
                    "status": 1 if int(item.get("status") if item.get("status") is not None else 1) else 0,
                    "updatedAt": str(item.get("updatedAt") or ""),
                }
            )
        except (TypeError, ValueError):
            invalid += 1
    return out, invalid


def _fetch_org(mode: str, since: str) -> tuple[list[dict], list[dict]]:
    if settings.data_center_provider == "mock":
        orgs = [dict(o) for o in MOCK_ORGS]
        users = [dict(u) for u in MOCK_USERS]
        if mode == "incremental":
            orgs = [o for o in orgs if str(o["updatedAt"]) > since]
            users = [u for u in users if str(u["updatedAt"]) > since]
        return orgs, users
    url = settings.data_center_base_url.rstrip("/") + settings.data_center_org_path
    sep = "&" if "?" in url else "?"
    url = f"{url}{sep}mode={mode}"
    if since:
        url += f"&since={urllib.parse.quote(since)}"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {settings.data_center_token}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=settings.data_center_timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"数据中台返回 HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"数据中台不可达：{exc.reason}") from exc
    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("数据中台返回非 JSON") from exc
    data = body.get("data") if isinstance(body, dict) else None
    orgs = body.get("orgs") if isinstance(body, dict) else None
    users = body.get("users") if isinstance(body, dict) else None
    if isinstance(data, dict):
        orgs = orgs if orgs is not None else data.get("orgs")
        users = users if users is not None else data.get("users")
    if not isinstance(orgs, list) or not isinstance(users, list):
        raise RuntimeError("数据中台返回缺少 orgs/users 数组")
    return orgs, users


def run_sync(mode: str = "incremental") -> dict:
    if not settings.data_center_enabled:
        return _fail(503, "数据中台未启用")
    mode = str(mode or "incremental").strip().lower()
    if mode not in SYNC_MODES:
        return _fail(400, "mode 只能是 incremental / full")
    since = "" if mode == "full" else store.get_state("orgCursor", "")
    job_id = store.create_job("org_sync", mode, {"mode": mode, "since": since})
    try:
        raw_orgs, raw_users = _fetch_org(mode, since)
        orgs, bad_orgs = _normalize_orgs(raw_orgs)
        users, bad_users = _normalize_users(raw_users)
        org_created, org_updated = store.upsert_orgs(orgs)
        user_created, user_updated = store.upsert_users(users)
        stamps = [str(o.get("updatedAt") or "") for o in orgs]
        stamps += [str(u.get("updatedAt") or "") for u in users]
        cursor = max([s for s in stamps if s], default=since)
        if cursor and cursor != since:
            store.set_state("orgCursor", cursor)
        mirror = store.mirror_snapshot()
        detail = {
            "mode": mode,
            "since": since,
            "cursor": cursor,
            "orgs": {
                "fetched": len(orgs),
                "created": org_created,
                "updated": org_updated,
            },
            "users": {
                "fetched": len(users),
                "created": user_created,
                "updated": user_updated,
            },
            "invalid": bad_orgs + bad_users,
            "mirror": mirror,
            "missing": {},
        }
        if mode == "full":
            remote_orgs = {o["orgCode"] for o in orgs}
            remote_users = {u["userNo"] for u in users}
            local_orgs = {o.get("orgCode") for o in store.list_orgs(1000)}
            local_users = {u.get("userNo") for u in store.list_users(2000)}
            detail["missing"] = {
                "orgs": len([c for c in local_orgs if c and c not in remote_orgs]),
                "users": len([u for u in local_users if u and u not in remote_users]),
                "policy": "只统计不删除（本地管理员/历史账号不受影响）",
            }
        checksum = hashlib.sha256(
            json.dumps(detail, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        store.finish_job(
            job_id,
            "success",
            total=len(orgs) + len(users),
            created=org_created + user_created,
            updated=org_updated + user_updated,
            failed=bad_orgs + bad_users,
            checksum=checksum,
            detail=detail,
        )
        store.set_state(
            "lastFullAt" if mode == "full" else "lastIncrementalAt",
            _now().isoformat(timespec="seconds"),
        )
        return {
            "ok": True,
            "data": {"jobId": job_id, **detail},
            "error": None,
            "status": 200,
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("数据中台组织同步失败")
        store.finish_job(job_id, "failed", error=f"{type(exc).__name__}: {exc}")
        return _fail(502, f"组织同步失败：{exc}")


# ------------------------------------------------------------------ 调度
def run_due(now: datetime | None = None) -> dict:
    if not settings.data_center_enabled:
        return {"ok": False, "error": "数据中台未启用", "executed": []}
    now = now or _now()
    today = now.date().isoformat()
    executed: list[dict] = []
    if now.hour >= settings.data_center_report_hour and store.get_state("lastReportDate") != today:
        result = run_report(date.fromisoformat(today) - timedelta(days=1), "all")
        executed.append({"job": "report", "ok": result["ok"], "data": result.get("data"),
                         "error": result.get("error")})
        if result["ok"]:
            store.set_state("lastReportDate", today)
    if now.hour >= settings.data_center_incremental_hour and store.get_state("lastIncrementalDate") != today:
        result = run_sync("incremental")
        executed.append({"job": "org_incremental", "ok": result["ok"], "data": result.get("data"),
                         "error": result.get("error")})
        if result["ok"]:
            store.set_state("lastIncrementalDate", today)
    if (
        now.weekday() == settings.data_center_full_weekday
        and now.hour >= settings.data_center_full_hour
        and store.get_state("lastFullDate") != today
    ):
        result = run_sync("full")
        executed.append({"job": "org_full", "ok": result["ok"], "data": result.get("data"),
                         "error": result.get("error")})
        if result["ok"]:
            store.set_state("lastFullDate", today)
    return {"ok": True, "time": now.isoformat(timespec="seconds"), "executed": executed}


_scheduler_thread: threading.Thread | None = None
_scheduler_stop = threading.Event()


def start_scheduler() -> threading.Thread | None:
    global _scheduler_thread
    if not settings.data_center_scheduler_enabled or _scheduler_thread is not None:
        return None
    _scheduler_stop.clear()

    def _loop() -> None:
        logger.info(
            "数据中台调度已启动：间隔 %ss，上报 %02d:00，增量 %02d:00，全量 周%d %02d:00",
            settings.data_center_scheduler_interval,
            settings.data_center_report_hour,
            settings.data_center_incremental_hour,
            settings.data_center_full_weekday,
            settings.data_center_full_hour,
        )
        while not _scheduler_stop.wait(settings.data_center_scheduler_interval):
            try:
                run_due()
            except Exception:  # noqa: BLE001 - 调度线程不能因单次失败退出
                logger.exception("数据中台定时任务执行失败")

    thread = threading.Thread(target=_loop, name="data-center-scheduler", daemon=True)
    thread.start()
    _scheduler_thread = thread
    return thread


def stop_scheduler() -> None:
    global _scheduler_thread
    _scheduler_stop.set()
    _scheduler_thread = None


def status() -> dict:
    states = store.states()
    return {
        "enabled": settings.data_center_enabled,
        "provider": settings.data_center_provider,
        "baseUrl": settings.data_center_base_url,
        "schema": SCHEMA,
        "includeContent": settings.data_center_include_content,
        "maxRecords": settings.data_center_max_records,
        "scheduler": {
            "enabled": settings.data_center_scheduler_enabled,
            "interval": settings.data_center_scheduler_interval,
            "reportHour": settings.data_center_report_hour,
            "incrementalHour": settings.data_center_incremental_hour,
            "fullWeekday": settings.data_center_full_weekday,
            "fullHour": settings.data_center_full_hour,
        },
        "state": states,
        "mirror": store.mirror_snapshot(),
        "jobs": store.list_jobs(20),
        "reports": store.list_reports(20),
    }


def sync_meta() -> dict:
    return {
        "provider": settings.data_center_provider,
        "orgPath": settings.data_center_org_path,
        "reportPath": settings.data_center_report_path,
        "modes": list(SYNC_MODES),
    }
