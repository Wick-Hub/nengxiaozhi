"""操作审计日志（任务 1.12）：写 ``operation_log``。

- ``KB_STORE=mysql`` 时落库；JSON 模式降级为结构化日志输出（不阻断业务）。
- ``detail`` 经 :func:`app.security.masking.mask_detail` **脱敏**后写入，
  避免学号/手机号等敏感值进审计表。
- 写失败只告警，**绝不影响主流程**——审计不能拖垮业务。
"""

from __future__ import annotations

import logging
from datetime import datetime, time

from fastapi import Request
from sqlalchemy import desc, func, insert, select

from app.config import settings
from app.db import models as M
from app.db.session import db_transaction
from app.security import masking
from app.security.ratelimit import client_key

logger = logging.getLogger(__name__)

#: 常用动作码（与 DDL 注释的 KB_PUBLISH/AGENT_UPDATE 风格一致）
ACTION_DOC_UPLOAD = "KB_UPLOAD"
ACTION_DOC_STATUS = "KB_STATUS"
ACTION_DOC_DELETE = "KB_DELETE"
ACTION_DOC_DOWNLOAD = "KB_DOWNLOAD"  # 任务 1.11：原文下载/预览取件
ACTION_SYNC = "KB_SYNC"
ACTION_LOG_EXPORT = "LOG_EXPORT"  # 任务 2.1：会话日志导出
ACTION_STATS_EXPORT = "STATS_EXPORT"  # 任务 3.10：统计报表导出（M-T-07）
ACTION_AGENT_CREATE = "AGENT_CREATE"  # 任务 2.2：助手创建（含场景一键搭建）
ACTION_AGENT_UPDATE = "AGENT_UPDATE"  # 任务 2.2：助手基础配置更新
ACTION_AGENT_STATUS = "AGENT_STATUS"  # 任务 2.2：助手上架/下架
ACTION_AGENT_DELETE = "AGENT_DELETE"  # 任务 2.2：助手删除
ACTION_AGENT_RETRIEVAL = "AGENT_RETRIEVAL"  # 任务 2.3：助手检索配置变更（M-A-05）
ACTION_CHANNEL_CREATE = "CHANNEL_CREATE"  # 任务 3.1：渠道新建（M-C-01）
ACTION_CHANNEL_UPDATE = "CHANNEL_UPDATE"  # 任务 3.1：渠道配置/绑定更新（M-C-01）
ACTION_CHANNEL_DELETE = "CHANNEL_DELETE"  # 任务 3.1：渠道删除（M-C-01）
ACTION_CHANNEL_COPY = "CHANNEL_COPY"  # 任务 3.2：渠道配置一键复制（M-C-04）
ACTION_APP_CREATE = "APP_CREATE"  # 任务 3.3：广场应用新增（M-C-05）
ACTION_APP_UPDATE = "APP_UPDATE"  # 任务 3.3：广场应用更新/启停/授权（M-C-06）
ACTION_APP_DELETE = "APP_DELETE"  # 任务 3.3：广场应用删除（M-C-05）
ACTION_MODEL_CREATE = "MODEL_CREATE"  # 任务 2.4：大模型新增（M-A-06/M-SYS-06）
ACTION_MODEL_UPDATE = "MODEL_UPDATE"  # 任务 2.4：大模型配置更新
ACTION_MODEL_DELETE = "MODEL_DELETE"  # 任务 2.4：大模型删除
ACTION_CATEGORY_CREATE = "CATEGORY_CREATE"  # 任务 2.5：知识分类新增（M-K-01）
ACTION_CATEGORY_UPDATE = "CATEGORY_UPDATE"  # 任务 2.5：知识分类重命名/排序（M-K-02）
ACTION_CATEGORY_DELETE = "CATEGORY_DELETE"  # 任务 2.5：知识分类删除（M-K-02）
ACTION_PERM_GRANT = "PERM_GRANT"  # 任务 2.6：知识库部门授权（M-K-03）
ACTION_PERM_UPDATE = "PERM_UPDATE"  # 任务 2.6：授权操作位更新（M-K-03）
ACTION_PERM_REVOKE = "PERM_REVOKE"  # 任务 2.6：授权撤销（M-K-03）
ACTION_POINT_CREATE = "POINT_CREATE"  # 任务 2.8：知识点新建切片（M-K-10）
ACTION_POINT_UPDATE = "POINT_UPDATE"  # 任务 2.8：知识点编辑/再加工（M-K-10）
ACTION_POINT_DELETE = "POINT_DELETE"  # 任务 2.8：知识点删除无效切片（M-K-10）
ACTION_POINT_EXPORT = "POINT_EXPORT"  # 任务 2.8：知识点批量导出（M-K-10）
ACTION_SENSITIVE_CREATE = "SENSITIVE_CREATE"  # 任务 2.10：敏感词/关键词新增（M-SYS-07）
ACTION_SENSITIVE_UPDATE = "SENSITIVE_UPDATE"  # 任务 2.10：词条更新/启停（M-SYS-07）
ACTION_SENSITIVE_DELETE = "SENSITIVE_DELETE"  # 任务 2.10：词条删除（M-SYS-07）
ACTION_ROLE_CREATE = "ROLE_CREATE"  # 任务 2.9：管理角色新增（M-SYS-01）
ACTION_ROLE_UPDATE = "ROLE_UPDATE"  # 任务 2.9：角色权限矩阵更新（M-SYS-01）
ACTION_ROLE_DELETE = "ROLE_DELETE"  # 任务 2.9：管理角色删除（M-SYS-01）
ACTION_ADMIN_CREATE = "ADMIN_CREATE"  # 任务 2.9：管理员账号新增（M-SYS-03）
ACTION_ADMIN_UPDATE = "ADMIN_UPDATE"  # 任务 2.9：管理员账号更新（M-SYS-03）
ACTION_ADMIN_DELETE = "ADMIN_DELETE"  # 任务 2.9：移除管理员角色（M-SYS-03）
ACTION_ACCESS_DENIED = "ACCESS_DENIED"  # 任务 2.9：越权请求拦截留痕
ACTION_UNRECOGNIZED_BATCH = "UNRECOGNIZED_BATCH"  # 任务 3.11：未识别问题批量处理
ACTION_UNRECOGNIZED_EXPORT = "UNRECOGNIZED_EXPORT"  # 任务 3.11：未识别问题导出
ACTION_UNRECOGNIZED_FEEDBACK = "UNRECOGNIZED_FEEDBACK"  # 任务 3.11：反哺知识草稿
ACTION_FEEDBACK_EXPORT = "FEEDBACK_EXPORT"  # 任务 4.9：用户反馈导出
ACTION_FEEDBACK_HANDLE = "FEEDBACK_HANDLE"  # 任务 4.9：用户反馈处理
ACTION_GOV_CREATE = "GOV_ITEM_CREATE"  # 任务 4.5：导办事项新增（M-G-01）
ACTION_GOV_UPDATE = "GOV_ITEM_UPDATE"  # 任务 4.5：导办事项维护/启停/生效时段
ACTION_GOV_DELETE = "GOV_ITEM_DELETE"  # 任务 4.5：导办事项删除
ACTION_GOV_SYNC = "GOV_SYNC"  # 任务 4.5：一网通办事项同步（I-03）
ACTION_ACCESS_SYNC = "ACCESS_SYNC"  # 任务 4.4：门禁流水同步（I-04）
ACTION_ACCESS_EXPORT = "ACCESS_EXPORT"  # 任务 4.4：通行流水导出
ACTION_ACCESS_REPORT = "ACCESS_REPORT"  # 任务 4.4：通行分析报告导出
ACTION_SUPPORT_CLAIM = "SUPPORT_CLAIM"  # 任务 4.6：坐席接入
ACTION_SUPPORT_REPLY = "SUPPORT_REPLY"  # 任务 4.6：人工回复
ACTION_SUPPORT_CLOSE = "SUPPORT_CLOSE"  # 任务 4.6：结束会话
ACTION_SUPPORT_ASSIGN = "SUPPORT_ASSIGN"  # 任务 4.6：转交分组/坐席
ACTION_DATA_CENTER_REPORT = "DATA_CENTER_REPORT"  # 任务 4.2：聊天日志/统计上报
ACTION_DATA_CENTER_SYNC = "DATA_CENTER_SYNC"  # 任务 4.2：组织/用户同步
ACTION_INTEGRATION_SAVE = "INTEGRATION_SAVE"  # 任务 4.8：集成配置新增/更新
ACTION_INTEGRATION_DELETE = "INTEGRATION_DELETE"  # 任务 4.8：集成配置删除
ACTION_INTEGRATION_ACTIVATE = "INTEGRATION_ACTIVATE"  # 任务 4.8：本地模型切换
ACTION_INTEGRATION_TEST = "INTEGRATION_TEST"  # 任务 4.8：连通性测试
ACTION_INTEGRATION_SEED = "INTEGRATION_SEED"  # 任务 4.8：写入示例集成

#: 操作日志筛选下拉里展示的动作码（与前端展示文案对应）
ACTION_LABELS: dict[str, str] = {
    ACTION_DOC_UPLOAD: "知识上传",
    ACTION_DOC_STATUS: "文档状态",
    ACTION_DOC_DELETE: "文档删除",
    ACTION_DOC_DOWNLOAD: "原文下载",
    ACTION_SYNC: "知识同步",
    ACTION_LOG_EXPORT: "日志导出",
    ACTION_AGENT_CREATE: "助手创建",
    ACTION_AGENT_UPDATE: "助手更新",
    ACTION_AGENT_STATUS: "助手上/下架",
    ACTION_AGENT_DELETE: "助手删除",
    ACTION_AGENT_RETRIEVAL: "检索配置",
    ACTION_CHANNEL_CREATE: "渠道新增",
    ACTION_CHANNEL_UPDATE: "渠道更新",
    ACTION_CHANNEL_DELETE: "渠道删除",
    ACTION_CHANNEL_COPY: "渠道复制",
    ACTION_APP_CREATE: "应用新增",
    ACTION_APP_UPDATE: "应用更新",
    ACTION_APP_DELETE: "应用删除",
    ACTION_CATEGORY_CREATE: "分类新增",
    ACTION_CATEGORY_UPDATE: "分类更新",
    ACTION_CATEGORY_DELETE: "分类删除",
    ACTION_PERM_GRANT: "授权新增",
    ACTION_PERM_UPDATE: "授权更新",
    ACTION_PERM_REVOKE: "授权撤销",
    ACTION_POINT_CREATE: "知识点新增",
    ACTION_POINT_UPDATE: "知识点更新",
    ACTION_POINT_DELETE: "知识点删除",
    ACTION_POINT_EXPORT: "知识点导出",
    ACTION_SENSITIVE_CREATE: "词条新增",
    ACTION_SENSITIVE_UPDATE: "词条更新",
    ACTION_SENSITIVE_DELETE: "词条删除",
    ACTION_ROLE_CREATE: "角色新增",
    ACTION_ROLE_UPDATE: "角色更新",
    ACTION_ROLE_DELETE: "角色删除",
    ACTION_ADMIN_CREATE: "管理员新增",
    ACTION_ADMIN_UPDATE: "管理员更新",
    ACTION_ADMIN_DELETE: "管理员移除",
    ACTION_ACCESS_DENIED: "越权拦截",
    ACTION_UNRECOGNIZED_BATCH: "未识别问题处理",
    ACTION_UNRECOGNIZED_EXPORT: "未识别问题导出",
    ACTION_UNRECOGNIZED_FEEDBACK: "反哺知识草稿",
    ACTION_FEEDBACK_EXPORT: "反馈导出",
    ACTION_FEEDBACK_HANDLE: "反馈处理",
    ACTION_GOV_CREATE: "事项新增",
    ACTION_GOV_UPDATE: "事项维护",
    ACTION_GOV_DELETE: "事项删除",
    ACTION_GOV_SYNC: "事项同步",
    ACTION_ACCESS_SYNC: "门禁同步",
    ACTION_ACCESS_EXPORT: "通行导出",
    ACTION_ACCESS_REPORT: "通行报告",
    ACTION_SUPPORT_CLAIM: "客服接入",
    ACTION_SUPPORT_REPLY: "客服回复",
    ACTION_SUPPORT_CLOSE: "客服结束",
    ACTION_SUPPORT_ASSIGN: "客服转交",
    ACTION_DATA_CENTER_REPORT: "中台上报",
    ACTION_DATA_CENTER_SYNC: "中台同步",
    ACTION_INTEGRATION_SAVE: "集成配置",
    ACTION_INTEGRATION_DELETE: "集成删除",
    ACTION_INTEGRATION_ACTIVATE: "本地模型切换",
    ACTION_INTEGRATION_TEST: "集成测试",
    ACTION_INTEGRATION_SEED: "集成示例",
}


def client_ip(request: Request | None) -> str:
    """取客户端 IP（复用限流的取值口径：``X-Forwarded-For`` 首段 → 对端 IP）。"""
    return client_key(request) if request is not None else ""


def log_operation(
    action: str,
    target_type: str,
    target_id: str | int,
    *,
    user_id: int | None = None,
    detail: dict | None = None,
    ip_addr: str = "",
) -> bool:
    """写一条操作日志。返回是否落库成功（JSON 模式恒为 ``False``）。"""
    payload = {
        "user_id": user_id,
        "action": action,
        "target_type": target_type,
        "target_id": str(target_id),
        "detail": masking.mask_detail(detail),
        "ip_addr": ip_addr or "",
    }

    if settings.store_label != "mysql":
        logger.info("[OPLOG] %s", payload)
        return False

    try:
        with db_transaction() as conn:
            conn.execute(insert(M.OperationLog).values(**payload))
        return True
    except Exception as exc:  # noqa: BLE001 - 审计失败不得影响业务
        logger.warning("操作日志写入失败（已忽略，不影响业务）：%s", exc)
        return False


def log_from_request(
    request: Request | None,
    action: str,
    target_type: str,
    target_id: str | int,
    *,
    user_id: int | None = None,
    detail: dict | None = None,
) -> bool:
    """便捷包装：自动带上请求来源 IP。"""
    return log_operation(
        action,
        target_type,
        target_id,
        user_id=user_id,
        detail=detail,
        ip_addr=client_ip(request),
    )


def _parse_day(raw: str | None, *, end: bool = False) -> datetime | None:
    """``YYYY-MM-DD`` → 当天 00:00 / 23:59:59；无法解析视为不筛选。"""
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        if len(text) == 10:
            day = datetime.strptime(text, "%Y-%m-%d").date()
            return datetime.combine(day, time(23, 59, 59) if end else time(0, 0, 0))
        return datetime.fromisoformat(text)
    except ValueError:
        logger.warning("操作日志：忽略无法解析的时间参数 %r", raw)
        return None


def query_operations(
    *,
    user_id: int | None = None,
    action: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """按操作人 / 动作 / 时间筛选并分页（任务 2.10 / M-SYS-08）。

    JSON 模式不落审计表，返回空列表（调用方按「无记录」渲染，不报错）。
    """
    page = max(1, int(page or 1))
    page_size = min(200, max(1, int(page_size or 20)))
    empty = {
        "items": [],
        "total": 0,
        "page": page,
        "pageSize": page_size,
        "actions": [{"action": k, "label": v} for k, v in ACTION_LABELS.items()],
        "operators": [],
    }
    if settings.store_label != "mysql":
        return empty

    conds = []
    if user_id is not None:
        conds.append(M.OperationLog.user_id == user_id)
    if action:
        conds.append(M.OperationLog.action == action)
    df = _parse_day(date_from)
    dt = _parse_day(date_to, end=True)
    if df is not None:
        conds.append(M.OperationLog.created_at >= df)
    if dt is not None:
        conds.append(M.OperationLog.created_at <= dt)

    try:
        with db_transaction() as conn:
            total = int(
                conn.execute(
                    select(func.count()).select_from(M.OperationLog).where(*conds)
                ).scalar()
                or 0
            )
            rows = conn.execute(
                select(
                    M.OperationLog.id,
                    M.OperationLog.user_id,
                    M.OperationLog.action,
                    M.OperationLog.target_type,
                    M.OperationLog.target_id,
                    M.OperationLog.detail,
                    M.OperationLog.ip_addr,
                    M.OperationLog.created_at,
                    M.SysUser.user_no,
                    M.SysUser.real_name,
                )
                .outerjoin(M.SysUser, M.SysUser.user_id == M.OperationLog.user_id)
                .where(*conds)
                .order_by(desc(M.OperationLog.id))
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()

            operator_rows = conn.execute(
                select(M.SysUser.user_id, M.SysUser.user_no, M.SysUser.real_name)
                .where(
                    M.SysUser.user_id.in_(
                        select(M.OperationLog.user_id)
                        .where(M.OperationLog.user_id.is_not(None))
                        .distinct()
                    )
                )
                .order_by(M.SysUser.user_id)
                .limit(100)
            ).all()
    except Exception as exc:  # noqa: BLE001 - 旧库可能缺表，管理页给空态而不是 500
        logger.warning("操作日志查询失败：%s", exc)
        return empty

    return {
        "items": [
            {
                "id": r[0],
                "userId": r[1],
                "action": r[2],
                "targetType": r[3],
                "targetId": r[4],
                "detail": r[5],
                "ipAddr": r[6],
                "createdAt": r[7].isoformat() if r[7] else None,
                "operatorNo": r[8],
                "operatorName": r[9] or r[8],
            }
            for r in rows
        ],
        "total": total,
        "page": page,
        "pageSize": page_size,
        "actions": [{"action": k, "label": v} for k, v in ACTION_LABELS.items()],
        "operators": [
            {"userId": r[0], "userNo": r[1], "realName": r[2] or r[1]} for r in operator_rows
        ],
    }


def count_operations() -> int:
    from sqlalchemy import func

    if settings.store_label != "mysql":
        return 0
    with db_transaction() as conn:
        return conn.execute(select(func.count()).select_from(M.OperationLog)).scalar() or 0
