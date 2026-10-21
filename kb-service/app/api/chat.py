"""问答接口：非流式 + SSE 流式（任务 1.7 / 1.15）。

契约与 kb-engine/src/web/server.js 对齐（前端 kb-service/static/app.js 直接消费）：

    POST /api/chat          -> {ok, data: ChatData, error, errorDetail?}
    POST /api/chat/stream   -> text/event-stream
        delta : {"type":"delta","session_id":..,"text":<增量>}
        done  : {"type":"done","answer":..,"refused":..,"confidence":..,"citations":[..],
                 "retrieved":..,"sessionId":..,"msgId":..,"model":..,"latencyMs":..,
                 "blocks":[..],"answerHtml":..}
        error : {"type":"error","session_id":..,"error":<消息>,"errorDetail":{...}}

``blocks`` / ``answerHtml``（任务 1.9）为**追加字段**，老前端忽略即可；
``delta`` 阶段前端按增量自行渲染，``done`` 时以服务端渲染结果为准。

``errorDetail``（任务 2.4 延伸 · LLM 网关接入）同样是**追加字段**，
形如 ``{"message","status","code","requestId","modelCode","hint"}``：
``code`` 是上游网关错误码（如 ``model_not_allowed``），``requestId`` 可用于
到网关的 ``gateway_request_log`` 里定位这一次请求，``hint`` 是中文处置建议。
**LLM 调用失败统一返回 502**（不透传上游状态码——网关的 401/403 说的是
「网关不认我们的 Key」，对前端而言不是「需要登录」）。

与原型一致的两点行为：
- LLM 未配置时**在进入 SSE 之前**返回 503（原型 ``llmAvailable()`` 检查），
  前端据 ``resp.ok`` 直接展示错误原因，不会进入流式解析。
- ``done`` 同时带 ``sessionId``（前端持久化用）与 ``session_id``（服务内部一致性）。

流式实现：同步编排在后台线程执行，``on_delta`` 回调把增量投入队列，
生成器从队列取出并转成 SSE 事件——避免阻塞事件循环。
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from collections.abc import Iterator

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from app.config import settings
from app.core import agent_runtime, ai_label
from app.core.agent_runtime import AgentRuntime
from app.core.index_store import get_index
from app.core.llm_client import LLMError
from app.core.pipeline import answer_question, answer_question_stream
from app.core.richtext import parse_blocks, render_html
from app.db import chatlog
from app.models.schemas import ApiResult, ChatData, ChatRequest
from app.security.deps import Principal, get_current_user
from app.security.ratelimit import rate_limit
from app.services import data_query, gov_admin, support

logger = logging.getLogger(__name__)

# 限流（任务 1.12）：按「客户端 + 作用域」滑动窗口计数，超限 429
router = APIRouter(dependencies=[Depends(rate_limit("chat"))])

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def _effective_role(payload_role: str | None, principal: Principal) -> str | None:
    """角色注入（任务 1.13）：带有效 token 时以 token 角色为准，防止前端伪造身份；
    无 token 时沿用请求体角色（与原型的自选身份一致）。
    """
    return principal.role if principal.authenticated else payload_role


def llm_ready() -> bool:
    """是否具备调用大模型的条件（mock 模式视为就绪）。"""
    return bool(settings.llm_mock or settings.llm_api_key)


def _llm_unavailable() -> JSONResponse:
    msg = "未配置 LLM_API_KEY。请写入 kb-service/.env（参照 .env.example），或设 LLM_MOCK=1 体验模拟回答。"
    logger.warning("问答拒绝：%s", msg)
    return JSONResponse(
        status_code=503,
        content={
            "ok": False,
            "data": None,
            "error": msg,
            "errorDetail": {
                "message": msg,
                "status": None,
                "code": "llm_unconfigured",
                "requestId": "",
                "modelCode": "",
                "hint": "在管理后台「模型网关」绑定助手到网关模型，或在 .env 配置 LLM_API_KEY",
            },
        },
    )


def _llm_failure(exc: LLMError) -> JSONResponse:
    """把 LLM / 网关异常转成**结构化**错误响应（任务 2.4 延伸）。

    状态码统一用 **502**，而**不**透传上游状态码：网关的 401/403 说的是
    「网关不认我们这把 Key」，对能小智的客户端而言并不是「你需要登录」，
    透传会把前端引到完全错误的处置方向。真实上游状态码放在
    ``errorDetail.status`` 里，运维照样看得到，且 ``requestId`` 可对账。

    这是接入 campus-ai-gateway 后最关键的一层：没有它，网关白名单配错
    只会表现为一个 500 加一坨原始响应文本。
    """
    detail = exc.to_dict()
    logger.error("问答失败（上游）：%s", exc.message)
    return JSONResponse(
        status_code=502,
        content={
            "ok": False,
            "data": None,
            "error": _llm_user_message(exc),
            "errorDetail": detail,
        },
    )


def _llm_user_message(exc: LLMError) -> str:
    """给最终用户看的消息：说清「哪里失败 + 怎么办」。

    用户不关心 trace，但需要知道是不是自己操作错了；``hint`` 把网关错误码
    翻成了可执行动作。``requestId`` 一并带上，用户报障时可直接引用。
    """
    text = "大模型服务调用失败"
    if exc.status_code:
        text += f"（上游 HTTP {exc.status_code}"
        text += f"，{exc.code}）" if exc.code else "）"
    elif exc.code:
        text += f"（{exc.code}）"
    if exc.hint:
        text += f"：{exc.hint}"
    if exc.request_id:
        text += f"［request_id={exc.request_id}］"
    return text


def _index_unavailable() -> JSONResponse | None:
    """索引不可用（如 MySQL 未启动）时返回 503。

    否则空索引会走「空召回 → 标准拒答」路径，把**服务故障**伪装成
    「知识库里没有这条答案」，用户与运维都无从察觉。
    """
    idx = get_index()
    if idx.ok:
        return None
    msg = f"知识库不可用（存储模式 {settings.store_label}）：{idx.error}"
    logger.error("问答拒绝：%s", msg)
    return JSONResponse(status_code=503, content={"ok": False, "data": None, "error": msg})


def _device_of(request: Request) -> str:
    """User-Agent → 设备大类（任务 3.10 / M-T-03）。

    只区分统计需要的四类，不做指纹识别；无法判断时记 ``other``。
    """
    ua = (request.headers.get("user-agent") or "").lower()
    if not ua:
        return "other"
    if any(k in ua for k in ("ipad", "tablet", "kindle", "playbook")) or (
        "android" in ua and "mobile" not in ua
    ):
        return "tablet"
    if any(k in ua for k in ("mobile", "iphone", "android", "phone")):
        return "mobile"
    return "pc"


def _guide_items(question: str, role: str | None) -> list[dict]:
    """导办事项推荐（任务 4.5 / P-DB-01）。

    推荐失败绝不能影响问答本身：数据源异常时返回空列表并记日志。
    """
    try:
        return gov_admin.recommend(question, role)
    except Exception:  # noqa: BLE001 - 推荐是增强能力，失败降级为空
        logger.warning("导办事项推荐失败", exc_info=True)
        return []


def _data_cards(question: str, principal: Principal, role: str | None) -> list[dict]:
    """问数卡片（任务 4.3 / P-DQ-01·02·03）。

    识别到课表/成绩/一卡通意图时返回结构化卡片；未识别、无权限或数据源
    异常都只降级为空/拒绝卡片，绝不影响问答主链路。
    """
    try:
        return data_query.chat_cards(question, principal, role)
    except Exception:  # noqa: BLE001 - 问数是增强能力，失败降级为空
        logger.warning("问数卡片生成失败", exc_info=True)
        return []


def _handoff(question: str) -> dict:
    """转人工提示（任务 4.6 / P-B-09）：客服关闭时不提示。"""
    if not settings.support_enabled:
        return {"handoffSuggested": False, "handoffKeywords": [], "supportEnabled": False}
    hint = support.handoff_hint(question)
    return {
        "handoffSuggested": bool(hint.get("suggested")),
        "handoffKeywords": list(hint.get("keywords") or []),
        "supportEnabled": True,
    }


def _resolve_agent(payload: ChatRequest) -> AgentRuntime | JSONResponse:
    """解析助手配置（任务 2.3）。

    ``agentId`` 未传 → 空配置（沿用全局，与批1 行为一致）；
    传了但不存在/已下架 → **400 而不是静默回落**，否则前端选了某个助手却拿到
    通用提示词的回答，问题在日志里完全不可见。
    """
    rt = agent_runtime.resolve(payload.agent_id)
    if rt.error:
        logger.warning("问答拒绝：%s", rt.error)
        return JSONResponse(status_code=400, content={"ok": False, "data": None, "error": rt.error})
    return rt


@router.post("/chat", response_model=ApiResult, summary="非流式问答")
def chat(
    payload: ChatRequest,
    request: Request,
    response: Response,
    principal: Principal = Depends(get_current_user),
) -> ApiResult:
    if not llm_ready():
        return _llm_unavailable()  # type: ignore[return-value]
    if (resp := _index_unavailable()) is not None:
        return resp  # type: ignore[return-value]
    rt = _resolve_agent(payload)
    if isinstance(rt, JSONResponse):
        return rt  # type: ignore[return-value]

    role = _effective_role(payload.role, principal)
    try:
        ans = answer_question(
            payload.question,
            role=role,
            session_id=payload.session_id,
            top_k=payload.top_k,
            mode=payload.mode,
            runtime=rt,
            channel_code=payload.channel_code,
        )
    except LLMError as exc:
        # 上游（直连厂商或网关）失败：必须给出可读且可对账的错误，
        # 而不是让它冒泡成一个没有任何线索的 500。
        return _llm_failure(exc)  # type: ignore[return-value]
    # 任务 3.7：响应头隐式标识（produceId 精确到消息级）
    response.headers.update(ai_label.headers(ans.session_id))
    # 任务 3.10：记录首次访问设备（失败不影响问答）
    chatlog.set_session_device(ans.session_id, _device_of(request))
    return ApiResult(
        ok=True,
        data=ChatData(
            sessionId=ans.session_id,
            msgId=ans.msg_id,
            answer=ans.text,
            refused=ans.refused,
            confidence=ans.confidence,
            citations=ans.citations,  # type: ignore[arg-type]
            retrieved=ans.retrieved,
            model=ans.model or None,
            # 任务 2.4：让「这次回答到底走的哪个模型」可被观测（默认路径仍为空，行为不变）
            modelCode=ans.model_code or None,
            modelSource=ans.model_source or None,
            latencyMs=ans.latency_ms,
            blocks=parse_blocks(ans.text),
            answerHtml=render_html(ans.text),
            aiGenerated=True,
            aiLabel=ai_label.build_label(
                session_id=ans.session_id, msg_id=ans.msg_id, model=ans.model
            ),
            guideItems=_guide_items(payload.question, role),
            dataCards=_data_cards(payload.question, principal, role),
            **_handoff(payload.question),
        ),
    )


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@router.post("/chat/stream", summary="流式问答（SSE）")
def chat_stream(
    payload: ChatRequest,
    request: Request,
    principal: Principal = Depends(get_current_user),
):
    if not llm_ready():
        return _llm_unavailable()
    if (resp := _index_unavailable()) is not None:
        return resp
    rt = _resolve_agent(payload)
    if isinstance(rt, JSONResponse):
        return rt

    session_id = payload.session_id or chatlog.new_session_id()
    role = _effective_role(payload.role, principal)
    device = _device_of(request)

    def event_stream() -> Iterator[str]:
        events: queue.Queue = queue.Queue()

        def worker() -> None:
            try:
                ans = answer_question_stream(
                    payload.question,
                    role=role,
                    session_id=session_id,
                    top_k=payload.top_k,
                    mode=payload.mode,
                    on_delta=lambda text: events.put(("delta", text)),
                    runtime=rt,
                    channel_code=payload.channel_code,
                )
                chatlog.set_session_device(ans.session_id, device)
                events.put(("done", ans))
            except LLMError as exc:
                # 上游（直连厂商或网关）失败：把网关错误码与 request_id 一并下发，
                # 否则前端只能显示一坨原始响应文本，运维也无从对账。
                logger.error("流式问答失败（上游）：%s", exc.message)
                events.put(("error", {"error": _llm_user_message(exc), "errorDetail": exc.to_dict()}))
            except Exception as exc:  # noqa: BLE001 - 需把异常转成 SSE error 事件
                logger.exception("流式问答失败")
                events.put(("error", {"error": str(exc)}))

        threading.Thread(target=worker, daemon=True).start()

        while True:
            kind, data = events.get()
            if kind == "delta":
                yield _sse({"type": "delta", "session_id": session_id, "text": data})
            elif kind == "done":
                yield _sse(
                    {
                        "type": "done",
                        "answer": data.text,
                        "refused": data.refused,
                        "confidence": data.confidence,
                        "citations": data.citations,
                        "retrieved": data.retrieved,
                        "sessionId": data.session_id,
                        "session_id": data.session_id,
                        "msgId": data.msg_id,
                        "model": data.model,
                        # 任务 2.4：模型编码与路由来源（默认路径为空串，与批1 契约兼容）
                        "modelCode": data.model_code,
                        "modelSource": data.model_source,
                        "latencyMs": data.latency_ms,
                        # 任务 1.9：富文本结构化数据 + 安全 HTML
                        # （delta 阶段前端自行增量渲染，done 时以服务端结果为准）
                        "blocks": parse_blocks(data.text),
                        "answerHtml": render_html(data.text),
                        # 任务 3.7：流式结束事件同样携带 AI 标识元数据
                        "aiGenerated": True,
                        "aiLabel": ai_label.build_label(
                            session_id=data.session_id, msg_id=data.msg_id, model=data.model
                        ),
                        # 任务 4.5：导办事项卡片（无匹配时为空数组）
                        "guideItems": _guide_items(payload.question, role),
                        # 任务 4.3：问数卡片（未识别/无权限时为空或拒绝卡片）
                        "dataCards": _data_cards(payload.question, principal, role),
                        # 任务 4.6：转人工建议（客服关闭时 supportEnabled=false）
                        **_handoff(payload.question),
                    }
                )
                return
            else:
                yield _sse({"type": "error", "session_id": session_id, **data})
                return

    headers = {**_SSE_HEADERS, **ai_label.headers(session_id)}
    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=headers)
