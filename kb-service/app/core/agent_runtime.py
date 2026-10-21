"""智能体运行时配置解析（任务 2.3 / M-A-04·05·09，任务 2.4 / M-A-06）。

把 ``agent`` 表里「可存储」的配置翻译成检索/生成两层**真正能用**的参数，
并固化优先级：

    显式请求参数  >  助手配置  >  全局默认（``.env`` / ``settings``）

**未配置的助手行为与批1 完全一致**：``retrievalConfig`` 全为 0/空时，
``topK``/``rerankTopn`` 回落到 ``settings``，部门/类型不做限定，
``systemPrompt`` 为空时回退 :data:`app.core.generator.SYSTEM_PROMPT`，
``modelCode`` 为空/``default`` 时用 ``.env`` 的模型，
``temperature``/``topP``/``maxTokens`` 为 ``None`` 时用 ``.env`` 的值。
因此一致性回归（``tools/parity``）不受影响。

**接线范围**：本模块解析 ``system_prompt``、检索参数（2.3）、模型路由与生成参数（2.4），
以及风控/兜底话术（2.11）。话术为空时由管线回落到
:mod:`app.core.replies` 的平台默认值——这是**唯一来源**，
``sensitive.RISK_REPLY`` 与 ``pipeline.REFUSAL_TEXT`` 都从那里取。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config import settings
from app.db import agent_store
from app.db.agent_store import DEFAULT_MODEL_CODE, STATUS_ON, normalize_retrieval

logger = logging.getLogger(__name__)


class AgentUnavailable(RuntimeError):
    """助手不可用（ID 非法 / 不存在 / 已下架）。"""


@dataclass
class AgentRuntime:
    """一次问答真正生效的助手配置。**所有字段均已归一，缺省即沿用全局**。"""

    agent_id: int | None = None
    agent_code: str = ""
    agent_name: str = ""
    system_prompt: str = ""  # 空 → generator.SYSTEM_PROMPT
    stream_enabled: int = 1
    top_k: int = 0  # 0 → settings.top_k
    rerank_topn: int = 0  # 0 → settings.rerank_topn
    min_score: float = 0.0  # 0 → 不设相似度阈值
    depts: tuple[str, ...] = ()
    ktypes: tuple[str, ...] = ()
    # ---- 任务 2.4（M-A-06 大模型配置）----
    #: 模型编码；空 / ``default`` → 用 ``.env`` 全局配置（见 ``core.llm_router``）
    model_code: str = ""
    #: 生成参数；``None`` → 沿用 ``.env``（**不物化**，见 agent_store.GEN_PARAMS）
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    # ---- 任务 2.11（话术接线）----
    #: 命中敏感词时的风控话术；空 → ``replies.DEFAULT_RISK_REPLY``
    risk_reply: str = ""
    #: 空召回 / 模型拒答时的兜底话术；空 → ``replies.DEFAULT_FALLBACK_REPLY``
    fallback_reply: str = ""
    #: 非空表示助手不可用（此时其余字段均为默认值，不应被使用）
    error: str | None = None

    # ---------------------------------------------------------------- 便捷属性
    @property
    def found(self) -> bool:
        return self.agent_id is not None and self.error is None

    def effective_top_k(self, requested: int | None = None) -> int:
        """召回条数：显式请求 > 助手配置 > 全局默认。"""
        return int(requested or self.top_k or settings.top_k)

    def effective_rerank_topn(self, requested: int | None = None) -> int:
        """精排候选数：显式请求 > 助手配置 > 全局默认。"""
        return int(requested or self.rerank_topn or settings.rerank_topn)

    # ---- 生成参数（任务 2.4）：同样三级优先级 ----
    def effective_temperature(self, requested: float | None = None) -> float:
        if requested is not None:
            return float(requested)
        return settings.llm_temperature if self.temperature is None else float(self.temperature)

    def effective_top_p(self, requested: float | None = None) -> float | None:
        """``topP`` 平台默认为 ``None``（不发送该字段，交给厂商默认）。"""
        if requested is not None:
            return float(requested)
        return None if self.top_p is None else float(self.top_p)

    def effective_max_tokens(self, requested: int | None = None) -> int:
        if requested:
            return int(requested)
        return settings.llm_max_tokens if self.max_tokens is None else int(self.max_tokens)

    def param_sources(self) -> dict:
        """每个生成参数的来源（供管理页显示「留空到底等于什么」）。"""
        return {
            "modelCode": self.model_code or DEFAULT_MODEL_CODE,
            "modelSource": "助手配置" if self.model_code and self.model_code != DEFAULT_MODEL_CODE
            else "全局默认",
            "temperature": self.temperature,
            "temperatureSource": "助手配置" if self.temperature is not None else "全局默认",
            "topP": self.top_p,
            "topPSource": "助手配置" if self.top_p is not None else "全局默认",
            "maxTokens": self.max_tokens,
            "maxTokensSource": "助手配置" if self.max_tokens is not None else "全局默认",
        }

    def to_dict(self) -> dict:
        """供管理接口返回（camelCase，与前端契约一致）。"""
        return {
            "agentId": self.agent_id,
            "agentCode": self.agent_code,
            "agentName": self.agent_name,
            "hasSystemPrompt": bool(self.system_prompt),
            "streamEnabled": self.stream_enabled,
            "topK": self.top_k,
            "rerankTopn": self.rerank_topn,
            "minScore": self.min_score,
            "depts": list(self.depts),
            "ktypes": list(self.ktypes),
            "modelCode": self.model_code,
            "temperature": self.temperature,
            "topP": self.top_p,
            "maxTokens": self.max_tokens,
            "riskReply": self.risk_reply,
            "fallbackReply": self.fallback_reply,
            "paramSources": self.param_sources(),
            # 2.11 起全部字段已接线，保留空数组以维持管理端契约
            "pendingFields": [],
        }


#: 无需解析的「空 agent_id」写法（含前端可能传来的空串）
_EMPTY_IDS = {"", "0", "none", "null"}


def _unavailable(message: str, strict: bool) -> AgentRuntime:
    if strict:
        raise AgentUnavailable(message)
    logger.warning("助手不可用，已回落全局默认：%s", message)
    return AgentRuntime(error=message)


def resolve(agent_id=None, *, strict: bool = False) -> AgentRuntime:
    """解析助手运行时配置。

    :param agent_id: 助手 ID（``None`` / 空串 / ``0`` 表示「未指定」，返回空配置）
    :param strict: ``True`` 时助手不可用直接抛 :class:`AgentUnavailable`；
        ``False`` 时返回带 ``error`` 的空配置（供调用方自行决定如何提示）
    """
    if agent_id is None or str(agent_id).strip().lower() in _EMPTY_IDS:
        return AgentRuntime()

    try:
        aid = int(str(agent_id).strip())
    except (TypeError, ValueError):
        return _unavailable(f"助手 ID 非法：{agent_id}", strict)

    rec = agent_store.get_agent(aid)
    if rec is None:
        return _unavailable(f"助手不存在：{aid}", strict)
    if int(rec.get("status", STATUS_ON)) != STATUS_ON:
        return _unavailable(f"助手已下架：{aid}（{rec.get('agentName') or ''}）", strict)

    cfg = normalize_retrieval(rec.get("retrievalConfig"))
    return AgentRuntime(
        agent_id=aid,
        agent_code=rec.get("agentCode") or "",
        agent_name=rec.get("agentName") or "",
        system_prompt=(rec.get("systemPrompt") or "").strip(),
        stream_enabled=1 if int(rec.get("streamEnabled", 1)) else 0,
        top_k=int(cfg["topK"]),
        rerank_topn=int(cfg["rerankTopn"]),
        min_score=float(cfg["minScore"]),
        depts=tuple(cfg["depts"]),
        ktypes=tuple(cfg["ktypes"]),
        # 生成参数（任务 2.4）：None 原样保留 = 沿用 .env，不在此处物化
        model_code=(rec.get("modelCode") or "").strip(),
        temperature=rec.get("temperature"),
        top_p=rec.get("topP"),
        max_tokens=rec.get("maxTokens"),
        risk_reply=(rec.get("riskReply") or "").strip(),
        fallback_reply=(rec.get("fallbackReply") or "").strip(),
    )
