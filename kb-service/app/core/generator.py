"""生成层（任务 1.7）。

对齐 kb-engine/src/llm/generator.js：
    - 系统提示词：5 条硬约束（只用资料、标 [编号]、不足则拒答、简体分点、敏感操作提醒）
    - 上下文格式：``[n] 部门/类型 · 标题路径\\n正文``，块间空行分隔
    - 用户消息：``知识资料：...\\n\\n问题：...``，role 非空时追加身份约束
    - 引用解析：**保留正文原始编号 n**（修复原型来源面板错位）
    - 拒答识别（正则）与置信度（首条 cos；拒答 0.15）

底层调用见 ``llm_client.py``（OpenAI 兼容 / mock）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.core import llm_router
from app.core.llm_client import DeltaHandler, LLMClient, get_client
from app.core.replies import DEFAULT_FALLBACK_REPLY
from app.core.retriever import Retrieved

SYSTEM_PROMPT = f"""你是「能小智」校园 AI 助手，为学校师生解答校园事务问题。严格遵守：
1. 只能依据用户消息中给出的知识资料回答，不得使用资料外的信息，不得编造数字、政策或流程；
2. 引用来源用 [编号] 标注（编号对应资料块开头的序号），可多处引用；
3. 资料不足以回答时，只回复："{DEFAULT_FALLBACK_REPLY}" 不要硬答；
4. 用简体中文，简洁分点，控制在 300 字以内；
5. 涉及个人账号、密码、缴费等敏感操作，提醒通过学校官方渠道办理。"""

# 拒答识别（与原型一致）
REFUSAL_PATTERN = re.compile(r"未找到相关信息|无法回答|没有相关资料")
REFUSAL_CONFIDENCE = 0.15

_CITE_RE = re.compile(r"\[(\d{1,2})\]")


@dataclass
class GenerationResult:
    answer: str
    citations: list[dict] = field(default_factory=list)
    refused: bool = False
    confidence: float = 0.0
    usage: dict = field(default_factory=dict)
    latency_ms: int = 0
    first_token_ms: int | None = None
    model: str = ""
    #: 实际使用的模型编码（任务 2.4；``default`` 表示走 ``.env``）
    model_code: str = ""
    #: 路由来源：``env`` / ``db`` / ``fallback``（任务 2.4，便于排查「为什么没走我配的模型」）
    model_source: str = ""


# ------------------------------------------------------------------ 提示词
def build_context(results: list[Retrieved]) -> str:
    """拼接知识资料，块首标注 [序号] 与来源信息。"""
    blocks: list[str] = []
    for i, r in enumerate(results):
        c = r.chunk
        head = f"[{i + 1}] {c.dept}/{c.ktype}"
        if c.title_path:
            head += f" · {c.title_path}"
        blocks.append(f"{head}\n{c.text}")
    return "\n\n".join(blocks)


def build_messages(
    question: str,
    results: list[Retrieved],
    history: list[dict] | None = None,
    role: str | None = None,
    system_prompt: str | None = None,
) -> list[dict]:
    """组装消息列表：system → history → user(资料+问题+身份)。

    :param system_prompt: 助手自定义提示词（任务 2.3 / M-A-04）；为空时回退
        :data:`SYSTEM_PROMPT`，因此**未配置提示词的助手与批1 行为逐字一致**。
    """
    messages: list[dict] = [{"role": "system", "content": system_prompt or SYSTEM_PROMPT}]
    if history:
        messages.extend(history)
    content = f"知识资料：\n{build_context(results)}\n\n问题：{question}"
    if role:
        content += f"\n（提问者身份：{role}，只能依据其可见资料回答）"
    messages.append({"role": "user", "content": content})
    return messages


# ------------------------------------------------------------------ 结果解析
def parse_citations(answer: str, results: list[Retrieved]) -> list[dict]:
    """解析回答中的 [n]，返回引用列表（保留原始编号 n）。

    编号越界（n < 1 或 n > len(results)）忽略；按编号升序、去重。
    """
    used: list[int] = []
    for m in _CITE_RE.finditer(answer):
        n = int(m.group(1))
        if 1 <= n <= len(results) and (n - 1) not in used:
            used.append(n - 1)
    used.sort()

    citations: list[dict] = []
    for i in used:
        c = results[i].chunk
        digits = re.sub(r"\D", "", str(c.doc_id))
        citations.append(
            {
                "n": i + 1,  # 正文中的原始编号（修复来源面板错位）
                "chunkId": c.chunk_id,
                "docId": int(digits) if digits else 0,
                "similarity": round(results[i].cos, 4),
                "titlePath": c.title_path,
                "dept": c.dept,
                "ktype": c.ktype,
            }
        )
    return citations


def is_refusal(answer: str) -> bool:
    return bool(REFUSAL_PATTERN.search(answer))


def confidence(results: list[Retrieved], answer: str) -> float:
    """置信度：拒答 0.15；否则取首条余弦（与原型一致）。"""
    if is_refusal(answer):
        return REFUSAL_CONFIDENCE
    if not results:
        return 0.0
    return round(results[0].cos, 4)


# ------------------------------------------------------------------ 编排
class Generator:
    def __init__(self, client: LLMClient | None = None) -> None:
        self.client = client or get_client()

    def _client_for(self, model_code: str | None) -> LLMClient:
        """按模型编码取客户端（任务 2.4）。

        空 / ``default`` → 复用 ``self.client``（批1 的全局单例，行为不变）；
        其它编码 → 走 :mod:`app.core.llm_router` 的按编码缓存（``set_override`` 可拦截）。
        """
        code = str(model_code or "").strip()
        if not code or code == llm_router.DEFAULT_MODEL_CODE:
            return self.client
        return llm_router.get_client_for(code)

    def _route(self, model_code: str | None) -> "llm_router.ModelRoute":
        """路由信息仅用于**上报**（``modelCode``/``modelSource``），便于排查
        「为什么没走我配的模型」。

        默认路径（空 / ``default``）也照样给出 ``default`` / ``env``——
        若这里回空，「压根没配模型」与「配了但没生效」在响应里长得一模一样，
        恰恰丢掉了排查所需的区分度（字段本身是 2.4 新增，不影响批1 契约）。
        """
        return llm_router.route_of(model_code)

    def _to_result(self, resp, results: list[Retrieved], route=None) -> GenerationResult:
        answer = resp.content.strip()
        return GenerationResult(
            answer=answer,
            citations=parse_citations(answer, results),
            refused=is_refusal(answer),
            confidence=confidence(results, answer),
            usage=resp.usage,
            latency_ms=resp.latency_ms,
            first_token_ms=resp.first_token_ms,
            model=resp.model,
            model_code=(route.model_code if route else ""),
            model_source=(route.source if route else ""),
        )

    def generate(
        self,
        question: str,
        results: list[Retrieved],
        role: str | None = None,
        history: list[dict] | None = None,
        system_prompt: str | None = None,
        model_code: str | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
    ) -> GenerationResult:
        """生成回答。

        ``model_code`` / ``temperature`` / ``top_p`` / ``max_tokens`` 由任务 2.4 引入：
        前者决定用哪个模型（见 :mod:`app.core.llm_router`），后三者按
        **显式请求 > 助手配置 > 全局默认** 由 :class:`~app.core.agent_runtime.AgentRuntime`
        归一后传入；``top_p`` 为 ``None`` 时**不下发该字段**（交给厂商默认）。
        """
        messages = build_messages(question, results, history, role, system_prompt)
        client = self._client_for(model_code)
        route = self._route(model_code)
        resp = client.chat(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
        )
        return self._to_result(resp, results, route)

    def generate_stream(
        self,
        question: str,
        results: list[Retrieved],
        role: str | None = None,
        history: list[dict] | None = None,
        on_delta: DeltaHandler | None = None,
        system_prompt: str | None = None,
        model_code: str | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
    ) -> GenerationResult:
        messages = build_messages(question, results, history, role, system_prompt)
        client = self._client_for(model_code)
        route = self._route(model_code)
        resp = client.chat_stream(
            messages,
            on_delta=on_delta,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
        )
        return self._to_result(resp, results, route)


_generator: Generator | None = None


def get_generator() -> Generator:
    global _generator
    if _generator is None:
        _generator = Generator()
    return _generator


def reset_generator() -> None:
    """仅供测试：清空单例。"""
    global _generator
    _generator = None
