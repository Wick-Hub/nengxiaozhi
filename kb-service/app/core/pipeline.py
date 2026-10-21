"""问答编排（对应 kb-engine/src/qa.js）。

流程：加载索引 → 向量化问题 → 角色过滤 + 混合召回 → 精排 → 生成 → 引用/置信度 → 落库。

召回与精排的衔接与原型一致：
    recall_k = RERANK_ENABLED ? max(RERANK_TOPN, top_k) : top_k
    精排仅在启用且召回数 > top_k 时执行

**与原型的一处有意差异**：原型在空召回时返回 ``gen: null``，本实现在空召回时
直接返回标准拒答文本（``refused=True``、置信度 0.15）并落库，前端无需特判。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.config import settings
from app.core import agent_runtime, replies
from app.core.agent_runtime import AgentRuntime
from app.core.embedder import get_embedder
from app.core.generator import REFUSAL_CONFIDENCE, get_generator
from app.core.index_store import get_index
from app.core.llm_client import DeltaHandler
from app.core.reranker import get_reranker
from app.core.retriever import Retrieved, hybrid_search
from app.db import chatlog, unrecognized_store
from app.security import sensitive
from app.session.context import load_context

logger = logging.getLogger(__name__)

#: 平台默认兜底话术（任务 2.11 起与助手配置共用同一来源）
REFUSAL_TEXT = replies.DEFAULT_FALLBACK_REPLY


@dataclass
class Answer:
    text: str
    citations: list[dict] = field(default_factory=list)
    confidence: float = 0.0
    refused: bool = False
    usage: dict | None = None
    session_id: str | None = None
    msg_id: int | None = None
    model: str = ""
    latency_ms: int = 0
    first_token_ms: int | None = None
    retrieved: int = 0
    #: 本次实际使用的模型编码与路由来源（任务 2.4）。``""`` = 走 ``.env`` 全局默认，
    #: 保持与批1 一致的「无额外字段」语义。
    model_code: str = ""
    model_source: str = ""
    #: 本次召回命中的切片（任务 2.3 对话测试面板用；主问答链路不消费）。
    #: 直接复用编排过程中已有的对象，**不额外检索**，因此对性能无影响。
    hits: list[Retrieved] = field(default_factory=list)


def gen_kwargs(rt: AgentRuntime) -> dict:
    """从运行时配置取出**生成层**参数（任务 2.4 / M-A-06）。

    统一在此处归一，避免 ``answer_question`` 与 ``answer_question_stream``
    各写一份「留空取全局默认」的逻辑而漂移。
    ``top_p`` 为 ``None`` 时下游不下发该字段（交给厂商默认）。
    """
    return {
        "model_code": rt.model_code or None,
        "temperature": rt.effective_temperature(),
        "top_p": rt.effective_top_p(),
        "max_tokens": rt.effective_max_tokens(),
    }


def retrieve(
    question: str,
    role: str | None = None,
    top_k: int | None = None,
    mode: str | None = None,
    runtime: AgentRuntime | None = None,
) -> list[Retrieved]:
    """检索：知识范围过滤 → 角色过滤 → 混合召回 → 精排 → 相似度阈值。

    :param runtime: 助手运行时配置（任务 2.3 / M-A-05）。``None`` 时与批1 完全一致。

    **范围过滤必须在 ``hybrid_search`` 之前做**：若放到结果之后再筛，其它部门/类型的
    切片会先占满 ``recall_k`` 名额，把本助手真正该看的知识挤出候选集，表现为
    「配了范围反而检索不到」。
    """
    rt = runtime
    top_k = top_k or (rt.top_k if rt else 0) or settings.top_k
    rerank_topn = (rt.rerank_topn if rt else 0) or settings.rerank_topn

    chunks = get_index().chunks
    if rt and rt.depts:
        wanted = set(rt.depts)
        chunks = [c for c in chunks if c.dept in wanted]
    if rt and rt.ktypes:
        wanted = set(rt.ktypes)
        chunks = [c for c in chunks if c.ktype in wanted]
    if not chunks:
        return []

    vector = get_embedder().encode_one(question)
    recall_k = max(rerank_topn, top_k) if settings.rerank_enabled else top_k
    results = hybrid_search(chunks, vector, question, role=role, top_n=recall_k, mode=mode)

    if settings.rerank_enabled and len(results) > top_k:
        results = get_reranker().rerank(question, results, top_k)

    if rt and rt.min_score > 0:
        # 阈值作用在余弦相似度上：三种检索模式（hybrid/vector/bm25）都会算 cos，
        # 且量纲统一在 0–1，运营人员容易理解；用 rrf 之类的排名分做阈值则不可解释。
        results = [r for r in results if r.cos >= rt.min_score]
    return results


def _refusal_answer(session_id: str | None, reply: str | None = None) -> Answer:
    return Answer(
        text=reply or REFUSAL_TEXT,
        citations=[],
        confidence=REFUSAL_CONFIDENCE,
        refused=True,
        session_id=session_id,
        retrieved=0,
    )


def _persist_refusal(sid: str, question: str, role: str | None, answer: Answer) -> None:
    """空召回时把「问题 + 标准拒答」落库，并回填 msg_id。"""
    chatlog.append_message(sid, "user", question, user_role=role)
    ai_msg = chatlog.append_message(
        sid, "ai", answer.text, confidence=answer.confidence, refused=True, citations=[]
    )
    answer.msg_id = ai_msg.get("msgId")


def _attributed_dept(rt: AgentRuntime | None) -> str | None:
    """助手只限定了单一部门时，把它作为问题池的初始归属（可被人工改判）。"""
    if rt is not None and len(rt.depts) == 1:
        return rt.depts[0]
    return None


def _pool_weak_recall(
    sid: str,
    question: str,
    role: str | None,
    answer: Answer,
    rt: AgentRuntime | None,
    channel_code: str | None,
) -> None:
    """空召回入池（任务 3.11「弱召回」）。

    空召回不经过 ``save_qa``（走 ``append_message``），必须在此显式入池，
    否则池子里只剩「模型拒答」一类，知识缺口永远看不到。
    """
    chatlog.note_unrecognized(
        session_id=sid,
        question=question,
        role=role,
        confidence=answer.confidence,
        source=unrecognized_store.SOURCE_WEAK_RECALL,
        msg_id=answer.msg_id,
        agent_id=rt.agent_id if rt is not None else None,
        channel_code=channel_code,
        dept=_attributed_dept(rt),
    )


# ------------------------------------------------------------------ 安全风控（任务 1.12）
def _blocked_answer(
    sid: str,
    question: str,
    role: str | None,
    hit: str,
    save: bool,
    reply: str | None = None,
) -> Answer:
    """提问命中敏感词：直接返回风控话术，**不检索、不调用大模型**。

    落库走 ``append_message``（而非 ``save_qa``），因此**不写入未识别问题池**——
    风控拦截不是「知识缺口」，不该污染反哺闭环。
    """
    logger.warning("提问命中敏感词「%s」，已拦截（session=%s）", hit, sid)
    answer = Answer(
        text=reply or sensitive.RISK_REPLY,
        citations=[],
        confidence=0.0,
        refused=True,
        session_id=sid,
        retrieved=0,
    )
    if save:
        chatlog.append_message(sid, "user", question, user_role=role)
        ai_msg = chatlog.append_message(
            sid, "ai", answer.text, confidence=0.0, refused=True, citations=[]
        )
        answer.msg_id = ai_msg.get("msgId")
    return answer


def _guard_answer(gen, reply: str | None = None) -> None:
    """回答侧敏感词检测：命中则**就地**替换为风控话术。

    流式路径下前端的 ``finalize`` 以 ``done.answer`` 为准，因此这里替换后
    前端最终展示的是风控话术（已推送的 delta 会被覆盖）。
    """
    hit = sensitive.check_answer(gen.answer)
    if hit:
        logger.warning("回答命中敏感词「%s」，已替换为风控话术", hit)
        gen.answer = reply or sensitive.RISK_REPLY
        gen.citations = []
        gen.refused = True
        gen.confidence = 0.0


def _apply_fallback(gen, reply: str) -> None:
    """模型自行拒答时，统一替换为助手配置的兜底话术。

    生成层的系统提示词写的是**平台默认**拒答句；助手若自定义了 ``fallbackReply``，
    最终展示必须一致。引用同时清空——拒答还带来源会自相矛盾。
    """
    if gen.refused:
        gen.answer = reply
        gen.citations = []


def answer_question(
    question: str,
    role: str | None = None,
    session_id: str | None = None,
    top_k: int | None = None,
    mode: str | None = None,
    save: bool = True,
    agent_id=None,
    runtime: AgentRuntime | None = None,
    channel_code: str | None = None,
) -> Answer:
    """一次完整问答（非流式）。

    :param agent_id: 助手 ID（任务 2.3）。传了但不可用会抛 :class:`AgentUnavailable`，
        由接口层翻成 400——**不做静默回落**，否则「选了财务助手却拿到通用回答」
        这种问题在日志里完全看不出来。
    :param runtime: 已解析好的运行时配置（接口层已校验过时可直接传入，省一次查库）
    :param channel_code: 来源渠道编码（任务 3.11 归因；不影响检索与生成）
    """
    rt = runtime if runtime is not None else agent_runtime.resolve(agent_id, strict=True)
    sid = session_id or chatlog.new_session_id()
    # 话术优先级：助手配置 > 平台默认（空串视为未配置）
    risk_reply = rt.risk_reply or sensitive.RISK_REPLY
    fallback_reply = rt.fallback_reply or REFUSAL_TEXT
    if (hit := sensitive.check_question(question)) is not None:
        return _blocked_answer(sid, question, role, hit, save, risk_reply)
    results = retrieve(question, role, top_k, mode, rt)
    if not results:
        answer = _refusal_answer(sid, fallback_reply)
        if save:
            _persist_refusal(sid, question, role, answer)
            _pool_weak_recall(sid, question, role, answer, rt, channel_code)
        return answer

    # 多轮上下文：默认最近 20 条（≈10 轮），并保证配对从 user 起（任务 1.8）
    history = load_context(sid)
    gen = get_generator().generate(
        question,
        results,
        role=role,
        history=history,
        system_prompt=rt.system_prompt,
        **gen_kwargs(rt),
    )
    _apply_fallback(gen, fallback_reply)
    _guard_answer(gen, risk_reply)
    msg_id: int | None = None
    if save:
        saved = chatlog.save_qa(
            question, role, gen, sid, chatlog.new_trace_id(),
            agent_id=rt.agent_id,
            channel_code=channel_code,
            dept=_attributed_dept(rt),
        )
        msg_id = saved.get("msgId")

    return Answer(
        text=gen.answer,
        citations=gen.citations,
        confidence=gen.confidence,
        refused=gen.refused,
        usage=gen.usage,
        session_id=sid,
        msg_id=msg_id,
        model=gen.model,
        latency_ms=gen.latency_ms,
        first_token_ms=gen.first_token_ms,
        retrieved=len(results),
        # model_code/model_source 是任务 2.4 新增的**诊断元信息**（回答走了哪个模型、来源）。
        # 用 getattr 兜底：生成器替身/自定义实现未提供时不应让整次问答崩掉。
        model_code=getattr(gen, "model_code", ""),
        model_source=getattr(gen, "model_source", ""),
        hits=results,
    )


def answer_question_stream(
    question: str,
    role: str | None = None,
    session_id: str | None = None,
    top_k: int | None = None,
    mode: str | None = None,
    save: bool = True,
    on_delta: DeltaHandler | None = None,
    agent_id=None,
    runtime: AgentRuntime | None = None,
    channel_code: str | None = None,
) -> Answer:
    """一次完整问答（流式：逐 token 回调 on_delta）。"""
    rt = runtime if runtime is not None else agent_runtime.resolve(agent_id, strict=True)
    sid = session_id or chatlog.new_session_id()
    risk_reply = rt.risk_reply or sensitive.RISK_REPLY
    fallback_reply = rt.fallback_reply or REFUSAL_TEXT
    if (hit := sensitive.check_question(question)) is not None:
        return _blocked_answer(sid, question, role, hit, save, risk_reply)
    results = retrieve(question, role, top_k, mode, rt)
    if not results:
        answer = _refusal_answer(sid, fallback_reply)
        if on_delta:
            on_delta(answer.text)
        if save:
            _persist_refusal(sid, question, role, answer)
            _pool_weak_recall(sid, question, role, answer, rt, channel_code)
        return answer

    # 多轮上下文：默认最近 20 条（≈10 轮），并保证配对从 user 起（任务 1.8）
    history = load_context(sid)
    gen = get_generator().generate_stream(
        question,
        results,
        role=role,
        history=history,
        on_delta=on_delta,
        system_prompt=rt.system_prompt,
        **gen_kwargs(rt),
    )
    # 模型拒答的 delta 已经推给前端，finalize 以 done.answer 为准，故这里覆写即可
    _apply_fallback(gen, fallback_reply)
    _guard_answer(gen, risk_reply)
    msg_id: int | None = None
    if save:
        saved = chatlog.save_qa(
            question, role, gen, sid, chatlog.new_trace_id(),
            agent_id=rt.agent_id,
            channel_code=channel_code,
            dept=_attributed_dept(rt),
        )
        msg_id = saved.get("msgId")

    return Answer(
        text=gen.answer,
        citations=gen.citations,
        confidence=gen.confidence,
        refused=gen.refused,
        usage=gen.usage,
        session_id=sid,
        msg_id=msg_id,
        model=gen.model,
        latency_ms=gen.latency_ms,
        first_token_ms=gen.first_token_ms,
        retrieved=len(results),
        # model_code/model_source 是任务 2.4 新增的**诊断元信息**（回答走了哪个模型、来源）。
        # 用 getattr 兜底：生成器替身/自定义实现未提供时不应让整次问答崩掉。
        model_code=getattr(gen, "model_code", ""),
        model_source=getattr(gen, "model_source", ""),
        hits=results,
    )
