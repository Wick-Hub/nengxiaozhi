"""智能体（AI 助手）存储层（任务 2.2 / M-A-01·02·03）。

两种后端，对外接口完全一致：

* **JSON 模式**（``KB_STORE=json``）：``kb-service/data/agents.json``，
  进程内加锁 + 原子写（``.tmp`` → ``replace``）；首次访问自动 seed 默认助手
  ``campus-default``，使无库环境也能跑通「创建 → 配置 → 启停」全流程。
* **MySQL 模式**（``KB_STORE=mysql``）：读写 ``agent`` 表。

**字段口径**：``visible_roles`` 在库里是逗号分隔字符串，对外一律归一为
``list[str]``（与 ``/api/meta`` 的 roles 列表同形），避免前端两套解析逻辑。

**数据层铁律**：``db_session()`` 返回的是 Core ``Connection``，对它执行
``select(Model)`` 不会实体化 ORM 对象——因此所有查询都**显式列出**，
再按位置取值（见 :data:`_COLS`）。
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, insert, select
from sqlalchemy import update as sa_update

from app.config import settings
from app.core import replies
from app.db import models as M
from app.db.session import db_session, db_transaction

logger = logging.getLogger(__name__)

AGENTS_PATH = settings.base_dir / "data" / "agents.json"
_lock = threading.RLock()

#: ``agent.agent_type`` 取值（与 DDL 注释一致）
AGENT_TYPES = ("chat", "db", "guide", "access", "custom")
STATUS_ON = 1
STATUS_OFF = 0
STATUS_NAMES = {1: "已上架", 0: "已下架"}

#: 与 ``app/db/chatlog.py`` 的 seed 常量保持一致，避免同一助手出现两个编码
DEFAULT_AGENT_CODE = "campus-default"
DEFAULT_AGENT_NAME = "能小智·校园百事通"
DEFAULT_DESCRIPTION = "面向全校师生的校园知识问答助手（默认）"
#: 平台默认话术（任务 2.11 起统一由 :mod:`app.core.replies` 定义）
DEFAULT_FALLBACK_REPLY = replies.DEFAULT_FALLBACK_REPLY
DEFAULT_RISK_REPLY = replies.DEFAULT_RISK_REPLY

# ------------------------------------------------------------------ 检索配置（任务 2.3 / M-A-05）
#: 检索配置字段。**「缺省」与「沿用全局」是同一件事**，因此用可辨识的零值表示未配置：
#:
#: * ``topK`` / ``rerankTopn`` 为 ``0`` → 沿用 ``settings.top_k`` / ``settings.rerank_topn``
#: * ``minScore`` 为 ``0.0`` → 不设相似度阈值
#: * ``depts`` / ``ktypes`` 为空数组 → 不限定知识范围
#:
#: 之所以不把全局默认值「物化」进每条记录：那样改 ``.env`` 后已建助手不会跟随，
#: 且会让「未配置助手」与「显式配成同样数值的助手」无法区分。
RETRIEVAL_DEFAULTS: dict = {
    "topK": 0,
    "rerankTopn": 0,
    "minScore": 0.0,
    "depts": [],
    "ktypes": [],
}
#: 对外/校验共用的取值范围
TOP_K_RANGE = (1, 50)
RERANK_TOPN_RANGE = (1, 200)
#: 「知识范围」最多勾选的文件夹项数（防止一次限定过多导致检索恒为空）
MAX_FOLDERS = 50

# ------------------------------------------------------------------ 生成参数（任务 2.4 / M-A-06）
#: 助手级生成参数。**``None`` = 沿用平台默认（``.env``）**，与检索配置的「可辨识零值」同一思路。
#:
#: 这里**不能**用 0 当哨兵值：``temperature=0.0`` 与 ``top_p=0.0`` 都是合法取值。
#: 也**不能**物化全局默认值：那样改 ``.env`` 后已建助手不跟随，且「没配过」与
#: 「显式配成了和默认一样」无法区分。因此这三列在 DDL 里是可空且无默认值。
GEN_PARAMS: tuple[str, ...] = ("temperature", "topP", "maxTokens")
TEMPERATURE_RANGE = (0.0, 2.0)
TOP_P_RANGE = (0.0, 1.0)
MAX_TOKENS_RANGE = (1, 32768)

#: ``agent.model_code`` 的保留编码：表示「走 ``.env`` 全局配置」，``llm_model`` 表不存该行。
DEFAULT_MODEL_CODE = "default"

# ------------------------------------------------------------------ 场景引导问题（任务 3.8 / P-QA-06）
#: 与渠道路由引导问题同一口径（3.4）：最多 6 条、单条 120 字。
#: 空数组 = 该助手不单独推荐，门户回退到渠道路由 / FAQ / 平台示例。
MAX_GUIDE_QUESTIONS = 6
MAX_GUIDE_LENGTH = 120

#: 2.4 之前被**物化**进记录的生成参数默认值。这些数值从未被读取过（2.3 的
#: ``AgentRuntime`` 只解析提示词与检索参数），因此迁移时把「三值恰好等于这组默认」
#: 的记录视为「从未配置」并置空——否则 ``maxTokens=2000`` 会突然变成生效值，
#: 而平台默认是 ``.env`` 的 1024，属于**行为漂移**。
LEGACY_MATERIALIZED: dict = {"temperature": 0.3, "topP": 1.0, "maxTokens": 2000}
#: 迁移标记键（JSON 记录级），保证一次性且幂等
PARAMS_MIGRATED_KEY = "paramsMigrated24"


def _str_list(raw) -> list[str]:
    """归一为去重（保序）的非空字符串列表；空值 → ``[]``。"""
    if isinstance(raw, (list, tuple)):
        items = [str(v).strip() for v in raw]
    elif raw is None or str(raw).strip() == "":
        items = []
    else:
        items = [p.strip() for p in str(raw).split(",")]
    out: list[str] = []
    for it in items:
        if it and it not in out:
            out.append(it)
    return out


def default_retrieval() -> dict:
    """默认检索配置。每次返回**新对象**，避免 ``depts``/``ktypes`` 列表被共享后误改。"""
    return {**RETRIEVAL_DEFAULTS, "depts": [], "ktypes": []}


def normalize_retrieval(raw) -> dict:
    """把任意形态的检索配置归一为完整结构（缺失键补默认值）。

    容错：``None`` / 非 dict / 坏 JSON 字符串 / 超范围数值 一律降级为默认值，
    绝不抛异常——检索配置是「锦上添花」，不该让问答整体失败。
    """
    cfg = default_retrieval()
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except Exception:  # noqa: BLE001
            return cfg
    if not isinstance(raw, dict):
        return cfg

    for key in ("topK", "rerankTopn"):
        try:
            cfg[key] = max(0, int(raw.get(key) or 0))
        except (TypeError, ValueError):
            cfg[key] = 0
    try:
        score = float(raw.get("minScore") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    cfg["minScore"] = min(1.0, max(0.0, score))
    cfg["depts"] = _str_list(raw.get("depts"))
    cfg["ktypes"] = _str_list(raw.get("ktypes"))
    return cfg


def merge_retrieval(base, patch) -> dict:
    """增量合并检索配置：只覆盖 ``patch`` 中**显式出现**的键。

    ``PATCH`` 语义下必须这样处理——否则「只想把 topK 改成 8」会把已经配好的
    ``depts`` 一起冲掉（``normalize_retrieval`` 会用空数组补齐缺失键）。
    """
    merged = normalize_retrieval(base)
    if isinstance(patch, dict):
        for key in RETRIEVAL_DEFAULTS:
            if key in patch:
                merged[key] = patch[key]
    return normalize_retrieval(merged)


def validate_retrieval(raw) -> list[str]:
    """检索配置校验，返回错误信息列表（空 = 通过）。

    放在数据层与 :func:`normalize_retrieval` 相邻，是为了让**默认值、取值范围、
    校验口径**只有一个来源——管理页的滑块上下限与这里共用同一组常量。
    ``0`` / ``[]`` 是合法值，表示「沿用全局 / 不限定」。
    """
    errors: list[str] = []
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except Exception:  # noqa: BLE001
            return ["检索配置必须是 JSON 对象"]
    if not isinstance(raw, dict):
        return ["检索配置必须是 JSON 对象"]

    for key, label, rng in (
        ("topK", "召回条数", TOP_K_RANGE),
        ("rerankTopn", "精排候选数", RERANK_TOPN_RANGE),
    ):
        if key not in raw:
            continue
        try:
            val = int(raw[key] or 0)
        except (TypeError, ValueError):
            errors.append(f"{label}（{key}）必须是整数")
            continue
        if val != 0 and not rng[0] <= val <= rng[1]:
            errors.append(f"{label}（{key}）需为 0（沿用全局）或 {rng[0]}–{rng[1]}")

    if "minScore" in raw:
        try:
            score = float(raw["minScore"] or 0.0)
        except (TypeError, ValueError):
            errors.append("最低相似度（minScore）必须是数值")
        else:
            if not 0.0 <= score <= 1.0:
                errors.append("最低相似度（minScore）需在 0–1 之间（0 表示不设阈值）")

    for key, label in (("depts", "知识范围·部门"), ("ktypes", "知识范围·类型")):
        if key not in raw:
            continue
        val = raw[key]
        if isinstance(val, str):
            val = [p.strip() for p in val.split(",") if p.strip()]
        if not isinstance(val, (list, tuple)):
            errors.append(f"{label}（{key}）必须是字符串数组")
            continue
        items = [str(v).strip() for v in val if str(v).strip()]
        if len(items) > MAX_FOLDERS:
            errors.append(f"{label}最多选择 {MAX_FOLDERS} 项")
        if any(len(it) > 64 for it in items):
            errors.append(f"{label}单个名称最长 64 字符")
    return errors


def validate_gen_params(raw: dict) -> list[str]:
    """生成参数校验，返回错误信息列表（空 = 通过）。

    ``None`` / 缺省 / 空串都是**合法值**，表示「沿用平台默认」——校验只拦
    「传了但不在范围内」的情况。取值范围与 :func:`normalize_gen_params` 共用常量，
    保证校验口径、归一逻辑、前端滑块上下限只有一个来源。
    """
    errors: list[str] = []
    if not isinstance(raw, dict):
        return errors

    if "temperature" in raw and raw["temperature"] not in (None, ""):
        try:
            temp = float(raw["temperature"])
        except (TypeError, ValueError):
            errors.append("temperature 必须是数值")
        else:
            if not TEMPERATURE_RANGE[0] <= temp <= TEMPERATURE_RANGE[1]:
                errors.append(
                    f"temperature 需在 {TEMPERATURE_RANGE[0]}–{TEMPERATURE_RANGE[1]} 之间"
                    "（留空表示沿用平台默认）"
                )
    if "topP" in raw and raw["topP"] not in (None, ""):
        try:
            top_p = float(raw["topP"])
        except (TypeError, ValueError):
            errors.append("topP 必须是数值")
        else:
            if not TOP_P_RANGE[0] < top_p <= TOP_P_RANGE[1]:
                errors.append("topP 需在 (0, 1] 之间（留空表示沿用平台默认）")
    if "maxTokens" in raw and raw["maxTokens"] not in (None, ""):
        try:
            mt = int(raw["maxTokens"])
        except (TypeError, ValueError):
            errors.append("maxTokens 必须是整数")
        else:
            if not MAX_TOKENS_RANGE[0] <= mt <= MAX_TOKENS_RANGE[1]:
                errors.append(
                    f"maxTokens 需在 {MAX_TOKENS_RANGE[0]}–{MAX_TOKENS_RANGE[1]} 之间"
                    "（留空表示沿用平台默认）"
                )
    return errors


def normalize_gen_params(raw) -> dict:
    """把三个生成参数归一为 ``{"temperature": float|None, "topP": ..., "maxTokens": ...}``。

    坏输入一律降级为 ``None``（沿用平台默认），绝不抛异常——生成参数是「锦上添花」，
    不该让问答整体失败。
    """
    src = raw if isinstance(raw, dict) else {}
    return {
        "temperature": _opt_float(src.get("temperature")),
        "topP": _opt_float(src.get("topP")),
        "maxTokens": _opt_int(src.get("maxTokens")),
    }


def _raw_guides(raw) -> list:
    """把 JSON 列 / JSON 字符串 / 换行文本统一成原始列表；坏输入 -> ``[]``。"""
    if isinstance(raw, (str, bytes)):
        text = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else raw
        try:
            raw = json.loads(text)
        except Exception:  # noqa: BLE001 - 也接受管理页「一行一条」的纯文本
            raw = text.splitlines()
    if not isinstance(raw, (list, tuple)):
        return []
    return list(raw)


def normalize_guide_questions(raw) -> list[str]:
    """场景引导问题归一：去空白、去重（保序）、截断到 6 条 / 每条 120 字。

    读路径允许截断（历史脏数据不应让门户报错）；写路径先经
    :func:`validate_guide_questions` 拦截超限，不会静默丢内容。
    """
    out: list[str] = []
    for item in _raw_guides(raw):
        if not isinstance(item, str):
            continue
        text = item.strip()[:MAX_GUIDE_LENGTH]
        if text and text not in out:
            out.append(text)
        if len(out) >= MAX_GUIDE_QUESTIONS:
            break
    return out


def validate_guide_questions(raw) -> list[str]:
    """场景引导问题校验，返回错误信息列表（空 = 通过）；``None``/``[]`` 合法。"""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        items = list(raw)
    elif isinstance(raw, (str, bytes)):
        items = _raw_guides(raw)
    else:
        return ["场景引导问题必须是字符串数组"]

    errors: list[str] = []
    nonempty = 0
    for item in items:
        if not isinstance(item, str):
            errors.append("场景引导问题每一条都必须是字符串")
            continue
        text = item.strip()
        if not text:
            continue
        nonempty += 1
        if len(text) > MAX_GUIDE_LENGTH:
            errors.append(f"引导问题单条最长 {MAX_GUIDE_LENGTH} 字")
    if nonempty > MAX_GUIDE_QUESTIONS:
        errors.append(f"引导问题最多 {MAX_GUIDE_QUESTIONS} 条")
    return errors

#: MySQL 侧显式列（顺序与 :func:`_row_to_dict` 一一对应）
_COLS = (
    M.Agent.agent_id,
    M.Agent.agent_code,
    M.Agent.agent_name,
    M.Agent.agent_type,
    M.Agent.system_prompt,
    M.Agent.model_code,
    M.Agent.temperature,
    M.Agent.top_p,
    M.Agent.max_tokens,
    M.Agent.visible_roles,
    M.Agent.icon_url,
    M.Agent.description,
    M.Agent.stream_enabled,
    M.Agent.risk_reply,
    M.Agent.fallback_reply,
    M.Agent.retrieval_config,
    M.Agent.guide_questions,
    M.Agent.status,
    M.Agent.created_at,
    M.Agent.updated_at,
)
_COL_KEYS = (
    "agentId",
    "agentCode",
    "agentName",
    "agentType",
    "systemPrompt",
    "modelCode",
    "temperature",
    "topP",
    "maxTokens",
    "visibleRoles",
    "iconUrl",
    "description",
    "streamEnabled",
    "riskReply",
    "fallbackReply",
    "retrievalConfig",
    "guideQuestions",
    "status",
    "createdAt",
    "updatedAt",
)


def _mysql() -> bool:
    return settings.store_label == "mysql"


# ------------------------------------------------------------------ 归一化
def split_roles(raw) -> list[str]:
    """``"全部,本科生"`` / ``["全部"]`` → ``["全部","本科生"]``；空值兜底 ``["全部"]``。"""
    if isinstance(raw, (list, tuple)):
        items = [str(r).strip() for r in raw]
    elif raw is None or str(raw).strip() == "":
        items = []
    else:
        items = [p.strip() for p in str(raw).split(",")]
    items = [p for p in items if p]
    return items or ["全部"]


def join_roles(raw) -> str:
    return ",".join(split_roles(raw))


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat(timespec="seconds")


def _as_float(value, default: float = 0.0) -> float:
    return float(value) if value is not None else default


def _as_int(value, default: int = 0) -> int:
    return int(value) if value is not None else default


def _opt_float(value) -> float | None:
    """可空浮点：``None`` / 空串 / 坏值 → ``None``（= 沿用平台默认）。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _opt_int(value) -> int | None:
    """可空整数：``None`` / 空串 / 坏值 → ``None``（= 沿用平台默认）。"""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _row_to_dict(row) -> dict:
    """MySQL 行 → 对外 dict（camelCase）。"""
    out = dict(zip(_COL_KEYS, row))
    # 生成参数（任务 2.4）：NULL 原样保留为 None = 沿用平台默认，**不物化**成具体数值
    out["temperature"] = _opt_float(out.get("temperature"))
    out["topP"] = _opt_float(out.get("topP"))
    out["maxTokens"] = _opt_int(out.get("maxTokens"))
    out["streamEnabled"] = 1 if _as_int(out.get("streamEnabled"), 1) else 0
    out["status"] = _as_int(out.get("status"), STATUS_ON)
    out["visibleRoles"] = split_roles(out.get("visibleRoles"))
    out["systemPrompt"] = out.get("systemPrompt") or ""
    out["iconUrl"] = out.get("iconUrl") or ""
    out["description"] = out.get("description") or ""
    out["riskReply"] = out.get("riskReply") or ""
    out["fallbackReply"] = out.get("fallbackReply") or ""
    out["retrievalConfig"] = normalize_retrieval(out.get("retrievalConfig"))
    out["guideQuestions"] = normalize_guide_questions(out.get("guideQuestions"))
    out["createdAt"] = _iso(out.get("createdAt"))
    out["updatedAt"] = _iso(out.get("updatedAt"))
    out["statusName"] = STATUS_NAMES.get(out["status"], "未知")
    return out


def _json_to_dict(rec: dict) -> dict:
    """JSON 记录 → 对外 dict（与 :func:`_row_to_dict` 同形）。"""
    return {
        "agentId": _as_int(rec.get("agentId")),
        "agentCode": rec.get("agentCode") or "",
        "agentName": rec.get("agentName") or "",
        "agentType": rec.get("agentType") or "chat",
        "systemPrompt": rec.get("systemPrompt") or "",
        "modelCode": rec.get("modelCode") or DEFAULT_MODEL_CODE,
        # 生成参数（任务 2.4）：缺省/None = 沿用平台默认
        "temperature": _opt_float(rec.get("temperature")),
        "topP": _opt_float(rec.get("topP")),
        "maxTokens": _opt_int(rec.get("maxTokens")),
        "visibleRoles": split_roles(rec.get("visibleRoles")),
        "iconUrl": rec.get("iconUrl") or "",
        "description": rec.get("description") or "",
        "streamEnabled": 1 if _as_int(rec.get("streamEnabled"), 1) else 0,
        "riskReply": rec.get("riskReply") or "",
        "fallbackReply": rec.get("fallbackReply") or "",
        "retrievalConfig": normalize_retrieval(rec.get("retrievalConfig")),
        "guideQuestions": normalize_guide_questions(rec.get("guideQuestions")),
        "status": _as_int(rec.get("status"), STATUS_ON),
        "createdAt": rec.get("createdAt"),
        "updatedAt": rec.get("updatedAt"),
        "statusName": STATUS_NAMES.get(_as_int(rec.get("status"), STATUS_ON), "未知"),
    }


# ------------------------------------------------------------------ JSON 后端
def _read_all() -> dict:
    if not AGENTS_PATH.is_file():
        return {"agents": {}, "nextId": 1}
    try:
        data = json.loads(AGENTS_PATH.read_text(encoding="utf-8"))
        if not isinstance(data.get("agents"), dict):
            data["agents"] = {}
        data["nextId"] = _as_int(data.get("nextId"), len(data["agents"]) + 1) or 1
        return data
    except Exception:  # noqa: BLE001 - 文件损坏时重建，避免管理端整体不可用
        logger.warning("助手文件损坏，已重建：%s", AGENTS_PATH)
        return {"agents": {}, "nextId": 1}


def _write_all(data: dict) -> None:
    AGENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = AGENTS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(AGENTS_PATH)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _migrate_json(data: dict) -> bool:
    """一次性迁移：把 2.4 之前**物化**的生成参数默认值置空（返回是否发生写入）。

    存量 JSON 记录里 ``temperature/topP/maxTokens`` 是 ``0.3/1.0/2000``——
    这些数值**从未被读取过**（2.3 的 ``AgentRuntime`` 只解析提示词与检索参数）。
    若不置空，它们会从 2.4 起突然变成生效值，而平台默认是 ``.env`` 的
    ``0.3/1.0/1024``——``maxTokens`` 会从 1024 漂到 2000，属于**行为漂移**。

    判定条件：三值**恰好**等于 :data:`LEGACY_MATERIALIZED` 且记录上无迁移标记。
    加标记是为了防止「用户之后恰好把三项都显式配成这组数值」被再次清空。
    """
    changed = False
    for rec in data.get("agents", {}).values():
        if not isinstance(rec, dict) or rec.get(PARAMS_MIGRATED_KEY):
            continue
        if all(_opt_float(rec.get(k)) == v for k, v in LEGACY_MATERIALIZED.items()):
            for key in GEN_PARAMS:
                rec[key] = None
            changed = True
        rec[PARAMS_MIGRATED_KEY] = True
        changed = True
    return changed


def _seed_json(data: dict) -> bool:
    """无数据时 seed 默认助手（返回是否发生了写入）。"""
    if data["agents"]:
        return False
    aid = int(data["nextId"])
    data["agents"][str(aid)] = {
        "agentId": aid,
        "agentCode": DEFAULT_AGENT_CODE,
        "agentName": DEFAULT_AGENT_NAME,
        "agentType": "chat",
        "systemPrompt": "",
        "modelCode": "default",
        # 生成参数留空（任务 2.4）= 沿用 .env 平台默认，不物化具体数值
        "temperature": None,
        "topP": None,
        "maxTokens": None,
        "visibleRoles": "全部",
        "iconUrl": "",
        "description": DEFAULT_DESCRIPTION,
        "streamEnabled": 1,
        "riskReply": DEFAULT_RISK_REPLY,
        "fallbackReply": DEFAULT_FALLBACK_REPLY,
        "retrievalConfig": default_retrieval(),
        "guideQuestions": [],
        "status": STATUS_ON,
        "createdAt": _now(),
        "updatedAt": _now(),
    }
    data["nextId"] = aid + 1
    return True


def _json_list() -> list[dict]:
    with _lock:
        data = _read_all()
        if _seed_json(data) or _migrate_json(data):
            _write_all(data)
        items = [_json_to_dict(r) for r in data["agents"].values()]
    items.sort(key=lambda a: a["agentId"])
    return items


# ------------------------------------------------------------------ 读
def list_agents(*, status: int | None = None, keyword: str | None = None) -> list[dict]:
    """全部助手（按 agentId 升序），可按上架状态与关键词（名称/编码/描述）过滤。"""
    if _mysql():
        stmt = select(*_COLS).order_by(M.Agent.agent_id)
        if status is not None:
            stmt = stmt.where(M.Agent.status == int(status))
        with db_session() as conn:
            items = [_row_to_dict(r) for r in conn.execute(stmt).all()]
    else:
        items = _json_list()
        if status is not None:
            items = [a for a in items if a["status"] == int(status)]

    if keyword:
        needle = keyword.strip().lower()
        if needle:
            items = [
                a
                for a in items
                if needle in a["agentName"].lower()
                or needle in a["agentCode"].lower()
                or needle in a["description"].lower()
            ]
    return items


def get_agent(agent_id: int) -> dict | None:
    if _mysql():
        with db_session() as conn:
            row = conn.execute(select(*_COLS).where(M.Agent.agent_id == int(agent_id))).first()
        return _row_to_dict(row) if row else None
    for a in _json_list():
        if a["agentId"] == int(agent_id):
            return a
    return None


def get_by_code(agent_code: str) -> dict | None:
    """按编码取（场景预设判断「是否已搭建」用）。"""
    if _mysql():
        with db_session() as conn:
            row = conn.execute(select(*_COLS).where(M.Agent.agent_code == str(agent_code))).first()
        return _row_to_dict(row) if row else None
    for a in _json_list():
        if a["agentCode"] == str(agent_code):
            return a
    return None


def count_channels_using(agent_id: int) -> int:
    """被渠道绑定为主助手的数量（删除前置校验；JSON 模式无渠道表，恒为 0）。"""
    if not _mysql():
        return 0
    with db_session() as conn:
        return int(
            conn.execute(
                select(func.count()).select_from(M.Channel).where(
                    M.Channel.default_agent_id == int(agent_id)
                )
            ).scalar()
            or 0
        )


# ------------------------------------------------------------------ 写
def _payload(record: dict) -> dict:
    """把对外 dict 收敛成可落库的字段集合（不含 id / 时间戳）。"""
    return {
        "agent_code": str(record.get("agentCode") or "").strip(),
        "agent_name": str(record.get("agentName") or "").strip(),
        "agent_type": str(record.get("agentType") or "chat").strip(),
        "system_prompt": record.get("systemPrompt") or "",
        "model_code": str(record.get("modelCode") or DEFAULT_MODEL_CODE).strip()
        or DEFAULT_MODEL_CODE,
        # 生成参数（任务 2.4）：None 落 NULL，表示沿用平台默认
        "temperature": _opt_float(record.get("temperature")),
        "top_p": _opt_float(record.get("topP")),
        "max_tokens": _opt_int(record.get("maxTokens")),
        "visible_roles": join_roles(record.get("visibleRoles")),
        "icon_url": record.get("iconUrl") or "",
        "description": record.get("description") or "",
        "stream_enabled": 1 if int(record.get("streamEnabled", 1)) else 0,
        "risk_reply": record.get("riskReply") or "",
        "fallback_reply": record.get("fallbackReply") or "",
        "retrieval_config": normalize_retrieval(record.get("retrievalConfig")),
        "guide_questions": normalize_guide_questions(record.get("guideQuestions")),
        "status": int(record.get("status", STATUS_ON)),
    }


def create_agent(record: dict) -> dict:
    """新建助手，返回落库后的完整记录（含 agentId）。"""
    payload = _payload(record)
    if _mysql():
        now = datetime.now()
        with db_transaction() as conn:
            result = conn.execute(insert(M.Agent).values(**payload, created_at=now, updated_at=now))
            new_id = int(result.inserted_primary_key[0])
        got = get_agent(new_id)
        assert got is not None  # 刚写入必然可读
        return got

    with _lock:
        data = _read_all()
        _seed_json(data) or _migrate_json(data)
        aid = int(data["nextId"])
        data["nextId"] = aid + 1
        rec = {
            "agentId": aid,
            "agentCode": payload["agent_code"],
            "agentName": payload["agent_name"],
            "agentType": payload["agent_type"],
            "systemPrompt": payload["system_prompt"],
            "modelCode": payload["model_code"],
            "temperature": payload["temperature"],
            "topP": payload["top_p"],
            "maxTokens": payload["max_tokens"],
            "visibleRoles": payload["visible_roles"],
            "iconUrl": payload["icon_url"],
            "description": payload["description"],
            "streamEnabled": payload["stream_enabled"],
            "riskReply": payload["risk_reply"],
            "fallbackReply": payload["fallback_reply"],
            "retrievalConfig": payload["retrieval_config"],
            "guideQuestions": payload["guide_questions"],
            "status": payload["status"],
            "createdAt": _now(),
            "updatedAt": _now(),
        }
        data["agents"][str(aid)] = rec
        _write_all(data)
        return _json_to_dict(rec)


def update_agent(agent_id: int, patch: dict) -> dict | None:
    """按字段增量更新（``patch`` 只含要改的键）；不存在返回 ``None``。"""
    if _mysql():
        current = get_agent(agent_id)
        if current is None:
            return None
        merged = {**current, **patch}
        if "retrievalConfig" in patch:
            merged["retrievalConfig"] = merge_retrieval(
                current.get("retrievalConfig"), patch["retrievalConfig"]
            )
        payload = _payload(merged)
        with db_transaction() as conn:
            conn.execute(
                sa_update(M.Agent)
                .where(M.Agent.agent_id == int(agent_id))
                .values(**payload, updated_at=datetime.now())
            )
        return get_agent(agent_id)

    with _lock:
        data = _read_all()
        _seed_json(data) or _migrate_json(data)
        key = str(int(agent_id))
        rec = data["agents"].get(key)
        if rec is None:
            return None
        merged = {**_json_to_dict(rec), **patch}
        if "retrievalConfig" in patch:
            merged["retrievalConfig"] = merge_retrieval(
                rec.get("retrievalConfig"), patch["retrievalConfig"]
            )
        payload = _payload(merged)
        rec.update(
            {
                "agentCode": payload["agent_code"],
                "agentName": payload["agent_name"],
                "agentType": payload["agent_type"],
                "systemPrompt": payload["system_prompt"],
                "modelCode": payload["model_code"],
                "temperature": payload["temperature"],
                "topP": payload["top_p"],
                "maxTokens": payload["max_tokens"],
                "visibleRoles": payload["visible_roles"],
                "iconUrl": payload["icon_url"],
                "description": payload["description"],
                "streamEnabled": payload["stream_enabled"],
                "riskReply": payload["risk_reply"],
                "fallbackReply": payload["fallback_reply"],
                "retrievalConfig": payload["retrieval_config"],
                "guideQuestions": payload["guide_questions"],
                "status": payload["status"],
                "updatedAt": _now(),
            }
        )
        data["agents"][key] = rec
        _write_all(data)
        return _json_to_dict(rec)


def delete_agent(agent_id: int) -> bool:
    """物理删除；不存在返回 ``False``（是否允许删由服务层判定）。"""
    if _mysql():
        with db_transaction() as conn:
            result = conn.execute(sa_delete(M.Agent).where(M.Agent.agent_id == int(agent_id)))
        return bool(result.rowcount)

    with _lock:
        data = _read_all()
        _seed_json(data) or _migrate_json(data)
        existed = data["agents"].pop(str(int(agent_id)), None) is not None
        if existed:
            _write_all(data)
        return existed


def reset_cache() -> None:
    """测试用：清空 JSON 侧缓存（当前无内存缓存，保留钩子以免调用方改动）。"""
    return None
