"""数据中台任务、上报批次与组织/用户镜像存储（任务 4.2 / I-02、M-SYS-02）。

两种后端，对外接口一致：

* **MySQL**：``data_center_job`` / ``data_center_report`` / ``data_center_state``，
  组织与用户直接 upsert 到 ``sys_org`` / ``sys_user``；
* **JSON**：``data/data_center.json``（进程内锁 + 原子写），组织与用户镜像同文件。

组织同步只新增/更新，不删除本地记录：数据中台某次返回缺项时只统计
``missing``，避免一次上游故障把本地管理员或历史用户批量停用。
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import delete, func, insert, select, update

from app.config import settings
from app.db import models as M
from app.db.session import db_session, db_transaction

logger = logging.getLogger(__name__)

DATA_CENTER_PATH: Path = settings.base_dir / "data" / "data_center.json"
SINK_DIR: Path = settings.base_dir / "data" / "data_center_outbox"
_lock = threading.RLock()
JOB_KEEP = 500


def _mysql() -> bool:
    return settings.store_label == "mysql"


def _now() -> datetime:
    return datetime.now()


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _empty_json() -> dict:
    return {
        "jobs": [],
        "reports": {},
        "state": {},
        "orgs": {},
        "users": {},
        "nextJobId": 1,
    }


def _read_json() -> dict:
    if not DATA_CENTER_PATH.is_file():
        data = _empty_json()
    else:
        try:
            data = json.loads(DATA_CENTER_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 文件损坏时重建，避免任务页整体不可用
            logger.warning("数据中台文件损坏，已重建：%s", DATA_CENTER_PATH)
            data = _empty_json()
    for key, value in _empty_json().items():
        data.setdefault(key, value)
    for key in ("jobs",):
        if not isinstance(data.get(key), list):
            data[key] = []
    for key in ("reports", "state", "orgs", "users"):
        if not isinstance(data.get(key), dict):
            data[key] = {}
    return data


def _write_json(data: dict) -> None:
    DATA_CENTER_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA_CENTER_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DATA_CENTER_PATH)


# ================================================================== 任务
def create_job(job_type: str, mode: str, detail: dict | None = None) -> int:
    if _mysql():
        with db_transaction() as conn:
            res = conn.execute(
                insert(M.DataCenterJob).values(
                    job_type=job_type,
                    mode=mode,
                    status="running",
                    started_at=_now(),
                    detail=detail or {},
                    checksum="",
                    error="",
                )
            )
            return int(res.inserted_primary_key[0])
    with _lock:
        data = _read_json()
        job_id = int(data.get("nextJobId") or 1)
        data["nextJobId"] = job_id + 1
        data["jobs"].append(
            {
                "jobId": job_id,
                "jobType": job_type,
                "mode": mode,
                "status": "running",
                "startedAt": _iso(_now()),
                "finishedAt": None,
                "total": 0,
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "failed": 0,
                "checksum": "",
                "detail": detail or {},
                "error": "",
            }
        )
        data["jobs"] = data["jobs"][-JOB_KEEP:]
        _write_json(data)
        return job_id


def finish_job(job_id: int, status: str, **fields) -> None:
    values = {
        "status": status,
        "finished_at": _now(),
        "total": int(fields.get("total") or 0),
        "created_count": int(fields.get("created") or 0),
        "updated_count": int(fields.get("updated") or 0),
        "skipped_count": int(fields.get("skipped") or 0),
        "failed_count": int(fields.get("failed") or 0),
        "checksum": str(fields.get("checksum") or ""),
        "error": str(fields.get("error") or "")[:512],
    }
    if fields.get("detail") is not None:
        values["detail"] = fields["detail"]
    if _mysql():
        with db_transaction() as conn:
            conn.execute(
                update(M.DataCenterJob)
                .where(M.DataCenterJob.job_id == job_id)
                .values(**values)
            )
        return
    with _lock:
        data = _read_json()
        for job in data["jobs"]:
            if int(job.get("jobId") or 0) != int(job_id):
                continue
            job.update(
                {
                    "status": status,
                    "finishedAt": _iso(values["finished_at"]),
                    "total": values["total"],
                    "created": values["created_count"],
                    "updated": values["updated_count"],
                    "skipped": values["skipped_count"],
                    "failed": values["failed_count"],
                    "checksum": values["checksum"],
                    "error": values["error"],
                }
            )
            if fields.get("detail") is not None:
                job["detail"] = fields["detail"]
            break
        _write_json(data)


def _job_row(row) -> dict:
    detail = row.detail if isinstance(row.detail, (dict, list)) else {}
    return {
        "jobId": int(row.job_id),
        "jobType": row.job_type,
        "mode": row.mode,
        "status": row.status,
        "startedAt": _iso(row.started_at),
        "finishedAt": _iso(row.finished_at),
        "total": int(row.total or 0),
        "created": int(row.created_count or 0),
        "updated": int(row.updated_count or 0),
        "skipped": int(row.skipped_count or 0),
        "failed": int(row.failed_count or 0),
        "checksum": row.checksum or "",
        "detail": detail,
        "error": row.error or "",
    }


def list_jobs(limit: int = 30) -> list[dict]:
    limit = max(1, min(200, int(limit or 30)))
    if _mysql():
        with db_session() as conn:
            rows = conn.execute(
                select(M.DataCenterJob)
                .order_by(M.DataCenterJob.job_id.desc())
                .limit(limit)
            ).all()
        return [_job_row(r) for r in rows]
    with _lock:
        data = _read_json()
    return list(reversed(data["jobs"][-limit:]))


def get_job(job_id: int) -> dict | None:
    if _mysql():
        with db_session() as conn:
            row = conn.execute(
                select(M.DataCenterJob).where(M.DataCenterJob.job_id == int(job_id))
            ).first()
        return _job_row(row) if row else None
    with _lock:
        data = _read_json()
    for job in data["jobs"]:
        if int(job.get("jobId") or 0) == int(job_id):
            return job
    return None


# ================================================================== 上报批次
def _report_row(row) -> dict:
    payload = row.payload
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:  # noqa: BLE001
            payload = {}
    return {
        "reportId": int(row.report_id),
        "batchId": row.batch_id,
        "kind": row.kind,
        "date": _iso(row.report_date)[:10] if row.report_date else "",
        "revision": int(row.revision or 1),
        "checksum": row.checksum or "",
        "status": row.status,
        "rows": int(row.rows_count or 0),
        "error": row.error or "",
        "payload": payload if isinstance(payload, dict) else {},
        "createdAt": _iso(row.created_at),
        "updatedAt": _iso(row.updated_at),
    }


def get_report(batch_id: str) -> dict | None:
    if _mysql():
        with db_session() as conn:
            row = conn.execute(
                select(M.DataCenterReport).where(M.DataCenterReport.batch_id == batch_id)
            ).first()
        return _report_row(row) if row else None
    with _lock:
        data = _read_json()
    item = data["reports"].get(batch_id)
    return dict(item) if item else None


def list_reports(limit: int = 20) -> list[dict]:
    limit = max(1, min(100, int(limit or 20)))
    if _mysql():
        with db_session() as conn:
            rows = conn.execute(
                select(M.DataCenterReport)
                .order_by(M.DataCenterReport.report_id.desc())
                .limit(limit)
            ).all()
        return [_report_row(r) for r in rows]
    with _lock:
        data = _read_json()
    items = sorted(
        data["reports"].values(),
        key=lambda r: (r.get("date") or "", r.get("batchId") or ""),
        reverse=True,
    )
    return [dict(r) for r in items[:limit]]


def save_report(
    *,
    batch_id: str,
    kind: str,
    report_date: str,
    checksum: str,
    payload: dict,
    rows_count: int,
    status: str,
    error: str = "",
    revision: int | None = None,
) -> dict:
    """按 batchId 写入/更新批次。``revision=None`` 表示沿用已有值。"""
    now = _now()
    if _mysql():
        with db_transaction() as conn:
            row = conn.execute(
                select(M.DataCenterReport).where(M.DataCenterReport.batch_id == batch_id)
            ).first()
            if row is None:
                conn.execute(
                    insert(M.DataCenterReport).values(
                        batch_id=batch_id,
                        kind=kind,
                        report_date=report_date,
                        revision=int(revision or 1),
                        checksum=checksum,
                        status=status,
                        rows_count=int(rows_count),
                        payload=json.dumps(payload, ensure_ascii=False),
                        error=error[:512],
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                conn.execute(
                    update(M.DataCenterReport)
                    .where(M.DataCenterReport.batch_id == batch_id)
                    .values(
                        revision=int(revision if revision is not None else row.revision),
                        checksum=checksum,
                        status=status,
                        rows_count=int(rows_count),
                        payload=json.dumps(payload, ensure_ascii=False),
                        error=error[:512],
                        updated_at=now,
                    )
                )
        return get_report(batch_id) or {}
    with _lock:
        data = _read_json()
        existing = data["reports"].get(batch_id)
        base = {
            "reportId": int(existing.get("reportId") if existing else len(data["reports"]) + 1),
            "batchId": batch_id,
            "kind": kind,
            "date": report_date,
            "revision": int(revision if revision is not None else (existing or {}).get("revision") or 1),
            "checksum": checksum,
            "status": status,
            "rows": int(rows_count),
            "error": error[:512],
            "payload": payload,
            "createdAt": (existing or {}).get("createdAt") or _iso(now),
            "updatedAt": _iso(now),
        }
        data["reports"][batch_id] = base
        _write_json(data)
        return dict(base)


def mark_report_delivered(batch_id: str) -> None:
    if _mysql():
        with db_transaction() as conn:
            conn.execute(
                update(M.DataCenterReport)
                .where(M.DataCenterReport.batch_id == batch_id)
                .values(status="delivered", error="", updated_at=_now())
            )
        return
    with _lock:
        data = _read_json()
        item = data["reports"].get(batch_id)
        if item:
            item.update({"status": "delivered", "error": "", "updatedAt": _iso(_now())})
            _write_json(data)


def mark_report_failed(batch_id: str, error: str) -> None:
    if _mysql():
        with db_transaction() as conn:
            conn.execute(
                update(M.DataCenterReport)
                .where(M.DataCenterReport.batch_id == batch_id)
                .values(status="failed", error=error[:512], updated_at=_now())
            )
        return
    with _lock:
        data = _read_json()
        item = data["reports"].get(batch_id)
        if item:
            item.update({"status": "failed", "error": error[:512], "updatedAt": _iso(_now())})
            _write_json(data)


# ================================================================== 水位
def get_state(key: str, default: str = "") -> str:
    if _mysql():
        with db_session() as conn:
            row = conn.execute(
                select(M.DataCenterState.state_value).where(
                    M.DataCenterState.state_key == key
                )
            ).first()
        return str(row[0]) if row else default
    with _lock:
        data = _read_json()
    return str(data["state"].get(key, default))


def set_state(key: str, value: str) -> None:
    if _mysql():
        with db_transaction() as conn:
            row = conn.execute(
                select(M.DataCenterState.state_key).where(
                    M.DataCenterState.state_key == key
                )
            ).first()
            if row is None:
                conn.execute(
                    insert(M.DataCenterState).values(
                        state_key=key, state_value=str(value), updated_at=_now()
                    )
                )
            else:
                conn.execute(
                    update(M.DataCenterState)
                    .where(M.DataCenterState.state_key == key)
                    .values(state_value=str(value), updated_at=_now())
                )
        return
    with _lock:
        data = _read_json()
        data["state"][key] = str(value)
        _write_json(data)


def states() -> dict:
    if _mysql():
        with db_session() as conn:
            rows = conn.execute(
                select(M.DataCenterState.state_key, M.DataCenterState.state_value)
            ).all()
        return {str(r[0]): str(r[1]) for r in rows}
    with _lock:
        data = _read_json()
    return dict(data["state"])


# ================================================================== 组织/用户
def _org_view(org: dict) -> dict:
    return {
        "orgCode": org.get("orgCode") or "",
        "orgName": org.get("orgName") or "",
        "parentCode": org.get("parentCode") or "",
        "orgPath": org.get("orgPath") or "",
        "sortNo": int(org.get("sortNo") or 0),
        "status": int(org.get("status") if org.get("status") is not None else 1),
        "updatedAt": org.get("updatedAt") or "",
    }


def _user_view(user: dict) -> dict:
    return {
        "userNo": user.get("userNo") or "",
        "realName": user.get("realName") or "",
        "idType": int(user.get("idType") or 1),
        "orgCode": user.get("orgCode") or "",
        "email": user.get("email") or "",
        "status": int(user.get("status") if user.get("status") is not None else 1),
        "updatedAt": user.get("updatedAt") or "",
    }


def upsert_orgs(items: list[dict]) -> tuple[int, int]:
    """按 ``orgCode`` upsert，返回 ``(created, updated)``。父节点须排在子节点之前。"""
    rows = [_org_view(i) for i in items if i.get("orgCode")]
    if _mysql():
        created = updated = 0
        now = _now()
        with db_transaction() as conn:
            existing = {
                str(r.org_code): {
                    "id": int(r.org_id),
                    "org_name": str(r.org_name or ""),
                    "parent_id": int(r.parent_id or 0),
                    "org_path": str(r.org_path or ""),
                    "sort_no": int(r.sort_no or 0),
                    "status": int(r.status or 0),
                }
                for r in conn.execute(select(M.SysOrg)).all()
            }
            for item in rows:
                code = item["orgCode"]
                parent_id = existing.get(item["parentCode"], {}).get("id", 0)
                if code in existing:
                    current = existing[code]
                    changed = (
                        current["org_name"] != item["orgName"]
                        or current["parent_id"] != parent_id
                        or current["sort_no"] != item["sortNo"]
                        or current["status"] != item["status"]
                        or current["org_path"] != item["orgPath"]
                    )
                    updated += int(changed)
                    conn.execute(
                        update(M.SysOrg)
                        .where(M.SysOrg.org_id == current["id"])
                        .values(
                            parent_id=parent_id,
                            org_name=item["orgName"],
                            org_path=item["orgPath"],
                            sort_no=item["sortNo"],
                            status=item["status"],
                            synced_at=now,
                        )
                    )
                    current.update({"parent_id": parent_id, **{
                        "org_name": item["orgName"],
                        "org_path": item["orgPath"],
                        "sort_no": item["sortNo"],
                        "status": item["status"],
                    }})
                else:
                    res = conn.execute(
                        insert(M.SysOrg).values(
                            parent_id=parent_id,
                            org_code=code,
                            org_name=item["orgName"],
                            org_path=item["orgPath"],
                            sort_no=item["sortNo"],
                            status=item["status"],
                            synced_at=now,
                        )
                    )
                    existing[code] = {
                        "id": int(res.inserted_primary_key[0]),
                        "org_name": item["orgName"],
                        "parent_id": parent_id,
                        "org_path": item["orgPath"],
                        "sort_no": item["sortNo"],
                        "status": item["status"],
                    }
                    created += 1
        return created, updated
    with _lock:
        data = _read_json()
        created = updated = 0
        for item in rows:
            existing = data["orgs"].get(item["orgCode"])
            if existing is None:
                data["orgs"][item["orgCode"]] = {**item, "syncedAt": _iso(_now())}
                created += 1
            else:
                changed = any(
                    existing.get(key) != item[key]
                    for key in ("orgName", "parentCode", "orgPath", "sortNo", "status")
                )
                existing.update({**item, "syncedAt": _iso(_now())})
                updated += int(changed)
        _write_json(data)
        return created, updated


def upsert_users(items: list[dict]) -> tuple[int, int]:
    """按 ``userNo`` upsert，返回 ``(created, updated)``。不覆盖密码与登录时间。"""
    rows = [_user_view(i) for i in items if i.get("userNo")]
    if _mysql():
        created = updated = 0
        now = _now()
        with db_transaction() as conn:
            org_ids = {
                str(r.org_code): int(r.org_id)
                for r in conn.execute(select(M.SysOrg.org_id, M.SysOrg.org_code)).all()
            }
            existing = {str(r.user_no): r for r in conn.execute(select(M.SysUser)).all()}
            for item in rows:
                org_id = org_ids.get(item["orgCode"])
                current = existing.get(item["userNo"])
                if current is None:
                    conn.execute(
                        insert(M.SysUser).values(
                            user_no=item["userNo"],
                            real_name=item["realName"],
                            id_type=item["idType"],
                            org_id=org_id,
                            email=item["email"],
                            user_status=item["status"],
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    created += 1
                else:
                    changed = (
                        str(current.real_name or "") != item["realName"]
                        or int(current.id_type or 0) != item["idType"]
                        or (current.org_id if current.org_id is not None else None) != org_id
                        or str(current.email or "") != item["email"]
                        or int(current.user_status or 0) != item["status"]
                    )
                    conn.execute(
                        update(M.SysUser)
                        .where(M.SysUser.user_id == current.user_id)
                        .values(
                            real_name=item["realName"],
                            id_type=item["idType"],
                            org_id=org_id,
                            email=item["email"],
                            user_status=item["status"],
                            updated_at=now,
                        )
                    )
                    updated += int(changed)
        return created, updated
    with _lock:
        data = _read_json()
        created = updated = 0
        for item in rows:
            existing = data["users"].get(item["userNo"])
            if existing is None:
                data["users"][item["userNo"]] = {**item, "syncedAt": _iso(_now())}
                created += 1
            else:
                changed = any(
                    existing.get(key) != item[key]
                    for key in ("realName", "idType", "orgCode", "email", "status")
                )
                existing.update({**item, "syncedAt": _iso(_now())})
                updated += int(changed)
        _write_json(data)
        return created, updated


def mirror_snapshot() -> dict:
    """返回本地组织/用户镜像的规模与最近同步时间。"""
    if _mysql():
        with db_session() as conn:
            orgs = int(conn.execute(select(func.count()).select_from(M.SysOrg)).scalar() or 0)
            users = int(conn.execute(select(func.count()).select_from(M.SysUser)).scalar() or 0)
            synced = conn.execute(select(func.max(M.SysOrg.synced_at))).scalar()
        return {"orgs": orgs, "users": users, "lastSyncedAt": _iso(synced)}
    with _lock:
        data = _read_json()
    synced = [v.get("syncedAt") for v in data["orgs"].values() if v.get("syncedAt")]
    return {
        "orgs": len(data["orgs"]),
        "users": len(data["users"]),
        "lastSyncedAt": max(synced) if synced else None,
    }


def list_orgs(limit: int = 200) -> list[dict]:
    limit = max(1, min(1000, int(limit or 200)))
    if _mysql():
        with db_session() as conn:
            rows = conn.execute(
                select(M.SysOrg).order_by(M.SysOrg.sort_no, M.SysOrg.org_id).limit(limit)
            ).all()
            all_codes = {
                int(r.org_id): str(r.org_code)
                for r in conn.execute(select(M.SysOrg.org_id, M.SysOrg.org_code)).all()
            }
        return [
            {
                "orgId": int(r.org_id),
                "orgCode": r.org_code,
                "orgName": r.org_name,
                "parentCode": all_codes.get(int(r.parent_id or 0), ""),
                "orgPath": r.org_path,
                "sortNo": int(r.sort_no or 0),
                "status": int(r.status or 0),
                "syncedAt": _iso(r.synced_at),
            }
            for r in rows
        ]
    with _lock:
        data = _read_json()
    items = sorted(
        data["orgs"].values(),
        key=lambda r: (int(r.get("sortNo") or 0), r.get("orgCode") or ""),
    )
    return [dict(r) for r in items[:limit]]


def list_users(limit: int = 200) -> list[dict]:
    limit = max(1, min(2000, int(limit or 200)))
    if _mysql():
        with db_session() as conn:
            rows = conn.execute(
                select(M.SysUser).order_by(M.SysUser.user_no).limit(limit)
            ).all()
            orgs = {
                int(r.org_id): str(r.org_code)
                for r in conn.execute(select(M.SysOrg.org_id, M.SysOrg.org_code)).all()
            }
        return [
            {
                "userId": int(r.user_id),
                "userNo": r.user_no,
                "realName": r.real_name,
                "idType": int(r.id_type or 0),
                "orgCode": orgs.get(int(r.org_id or 0), ""),
                "email": r.email or "",
                "status": int(r.user_status or 0),
                "updatedAt": _iso(r.updated_at),
            }
            for r in rows
        ]
    with _lock:
        data = _read_json()
    items = sorted(data["users"].values(), key=lambda r: r.get("userNo") or "")
    return [dict(r) for r in items[:limit]]


def write_sink(batch_id: str, payload: dict) -> str:
    """mock 模式收件箱：把上报内容落盘，便于验收与联调对账。"""
    SINK_DIR.mkdir(parents=True, exist_ok=True)
    path = SINK_DIR / f"{batch_id}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return str(path)


def reset_store() -> None:
    """测试用：清空任务、批次、水位与镜像。"""
    if _mysql():
        with db_transaction() as conn:
            conn.execute(delete(M.DataCenterJob))
            conn.execute(delete(M.DataCenterReport))
            conn.execute(delete(M.DataCenterState))
        return
    with _lock:
        _write_json(_empty_json())
