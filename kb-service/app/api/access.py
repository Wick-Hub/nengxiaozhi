"""通行查询智能体管理接口（任务 4.4 / P-DQ-04、I-04）。

全部端点仅管理员可用：``/api/admin/access``，RBAC 权限点
``access.view``（看板/下钻/明细）、``access.manage``（同步）、``access.export``
（报告与明细导出）。门禁流水同步到本地分析库后再查询，外部平台不可用时
不影响已同步数据的分析。
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from app.models.schemas import ApiResult
from app.security import oplog
from app.security.deps import Principal, require_admin, require_permission
from app.security.ratelimit import rate_limit
from app.services import access

router = APIRouter(
    prefix="/admin",
    dependencies=[
        Depends(require_admin),
        Depends(require_permission),
        Depends(rate_limit("admin")),
    ],
)

CSV_COLUMNS = [
    ("time", "时间"),
    ("direction", "方向"),
    ("campus", "校区"),
    ("college", "学院"),
    ("gate", "门禁点"),
    ("userName", "姓名"),
    ("userNo", "学工号"),
    ("role", "身份"),
    ("abnormal", "异常"),
    ("reason", "异常原因"),
]


def _unwrap(result: dict) -> ApiResult:
    if not result.get("ok"):
        raise HTTPException(
            status_code=int(result.get("status") or 400),
            detail=result.get("error") or "通行分析失败",
        )
    return ApiResult(ok=True, data=result["data"])


def _filters(
    date_from: str | None,
    date_to: str | None,
    campus_id: int | None,
    college: str | None,
    gate_id: int | None,
    role: str | None,
    abnormal: bool | None,
    direction: str | None,
    q: str | None,
) -> dict:
    return {
        "date_from": date_from,
        "date_to": date_to,
        "campus_id": campus_id,
        "college": college,
        "gate_id": gate_id,
        "role": role,
        "abnormal": abnormal,
        "direction": direction,
        "q": q,
    }


@router.get("/access/overview", response_model=ApiResult, summary="通行分析看板（P-DQ-04）")
def access_overview(
    date_from: str | None = Query(default=None, alias="dateFrom", description="YYYY-MM-DD"),
    date_to: str | None = Query(default=None, alias="dateTo", description="YYYY-MM-DD"),
    campus_id: int | None = Query(default=None, alias="campusId", ge=0),
    college: str | None = Query(default=None, max_length=128),
    gate_id: int | None = Query(default=None, alias="gateId", ge=0),
    role: str | None = Query(default=None, max_length=32),
    abnormal: bool | None = Query(default=None),
    direction: str | None = Query(default=None, pattern="^(in|out)?$"),
    q: str | None = Query(default=None, max_length=64),
) -> ApiResult:
    return _unwrap(access.overview(**_filters(
        date_from, date_to, campus_id, college, gate_id, role, abnormal, direction, q
    )))


@router.get("/access/drilldown", response_model=ApiResult, summary="通行数据层级下钻（P-DQ-04）")
def access_drilldown(
    dimension: str = Query(default="campus", pattern="^(campus|college|gate)$"),
    date_from: str | None = Query(default=None, alias="dateFrom"),
    date_to: str | None = Query(default=None, alias="dateTo"),
    campus_id: int | None = Query(default=None, alias="campusId", ge=0),
    college: str | None = Query(default=None, max_length=128),
    gate_id: int | None = Query(default=None, alias="gateId", ge=0),
    role: str | None = Query(default=None, max_length=32),
    abnormal: bool | None = Query(default=None),
    direction: str | None = Query(default=None, pattern="^(in|out)?$"),
    q: str | None = Query(default=None, max_length=64),
) -> ApiResult:
    return _unwrap(access.drilldown(
        dimension,
        **_filters(date_from, date_to, campus_id, college, gate_id, role, abnormal, direction, q),
    ))


@router.get("/access/events", response_model=ApiResult, summary="通行流水明细（P-DQ-04）")
def access_events(
    date_from: str | None = Query(default=None, alias="dateFrom"),
    date_to: str | None = Query(default=None, alias="dateTo"),
    campus_id: int | None = Query(default=None, alias="campusId", ge=0),
    college: str | None = Query(default=None, max_length=128),
    gate_id: int | None = Query(default=None, alias="gateId", ge=0),
    role: str | None = Query(default=None, max_length=32),
    abnormal: bool | None = Query(default=None),
    direction: str | None = Query(default=None, pattern="^(in|out)?$"),
    q: str | None = Query(default=None, max_length=64),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200, alias="pageSize"),
) -> ApiResult:
    return _unwrap(access.event_page(
        **_filters(date_from, date_to, campus_id, college, gate_id, role, abnormal, direction, q),
        page=page,
        page_size=page_size,
    ))


@router.post("/access/sync", response_model=ApiResult, summary="同步门禁流水到本地分析库（I-04）")
def access_sync(
    request: Request,
    days: int | None = Query(default=None, ge=1, le=366),
    principal: Principal = Depends(require_admin),
) -> ApiResult:
    result = access.sync_events(days)
    if result["ok"]:
        data = result["data"]
        oplog.log_from_request(
            request,
            oplog.ACTION_ACCESS_SYNC,
            "access_event",
            data["provider"],
            user_id=principal.user_id,
            detail={
                "provider": data["provider"],
                "from": data["from"],
                "to": data["to"],
                "imported": data["imported"],
                "skipped": data["skipped"],
            },
        )
    return _unwrap(result)


@router.get("/access/export", summary="通行流水明细导出 CSV（P-DQ-04）")
def access_export(
    request: Request,
    date_from: str | None = Query(default=None, alias="dateFrom"),
    date_to: str | None = Query(default=None, alias="dateTo"),
    campus_id: int | None = Query(default=None, alias="campusId", ge=0),
    college: str | None = Query(default=None, max_length=128),
    gate_id: int | None = Query(default=None, alias="gateId", ge=0),
    role: str | None = Query(default=None, max_length=32),
    abnormal: bool | None = Query(default=None),
    direction: str | None = Query(default=None, pattern="^(in|out)?$"),
    q: str | None = Query(default=None, max_length=64),
) -> Response:
    result = access.export_rows(**_filters(
        date_from, date_to, campus_id, college, gate_id, role, abnormal, direction, q
    ))
    if not result["ok"]:
        raise HTTPException(status_code=int(result.get("status") or 400), detail=result.get("error"))
    data = result["data"]

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([label for _, label in CSV_COLUMNS])
    for row in data["rows"]:
        writer.writerow([row.get(key, "") for key, _ in CSV_COLUMNS])
    body = "\ufeff" + buffer.getvalue()

    oplog.log_from_request(
        request,
        oplog.ACTION_ACCESS_EXPORT,
        "access_event",
        "csv",
        detail={
            "rows": len(data["rows"]),
            "truncated": data["truncated"],
            "window": data["window"],
        },
    )
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    response = Response(content=body, media_type="text/csv; charset=utf-8")
    response.headers["Content-Disposition"] = (
        f"attachment; filename*=UTF-8''{quote(f'通行流水-{stamp}.csv')}"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    if data["truncated"]:
        response.headers["X-Export-Truncated"] = f"true; limit={data['limit']}"
    return response


@router.get("/access/report", summary="通行智能分析报告（P-DQ-04）")
def access_report(
    request: Request,
    download: bool = Query(default=False),
    date_from: str | None = Query(default=None, alias="dateFrom"),
    date_to: str | None = Query(default=None, alias="dateTo"),
    campus_id: int | None = Query(default=None, alias="campusId", ge=0),
    college: str | None = Query(default=None, max_length=128),
    gate_id: int | None = Query(default=None, alias="gateId", ge=0),
    role: str | None = Query(default=None, max_length=32),
    abnormal: bool | None = Query(default=None),
    direction: str | None = Query(default=None, pattern="^(in|out)?$"),
    q: str | None = Query(default=None, max_length=64),
):
    result = access.build_report(**_filters(
        date_from, date_to, campus_id, college, gate_id, role, abnormal, direction, q
    ))
    if not result["ok"]:
        raise HTTPException(status_code=int(result.get("status") or 400), detail=result.get("error"))
    data = result["data"]
    if download:
        oplog.log_from_request(
            request,
            oplog.ACTION_ACCESS_REPORT,
            "access_event",
            "report",
            detail={"window": data["window"]},
        )
        response = Response(
            content=data["content"],
            media_type="text/markdown; charset=utf-8",
        )
        response.headers["Content-Disposition"] = (
            f"attachment; filename*=UTF-8''{quote(data['filename'])}"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response
    return ApiResult(ok=True, data=data)
