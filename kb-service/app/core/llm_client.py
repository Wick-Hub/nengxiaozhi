"""LLM 客户端（任务 1.7）。

对齐 kb-engine/src/llm/client.js：
    - OpenAI 兼容 ``/chat/completions``，支持 ``stream`` 与 ``usage``
    - ``LLM_MOCK=1`` 时返回**确定性**模拟回答，便于离线联调与回归
    - 统一记录 ``latency_ms`` / ``first_token_ms`` / ``model``
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.core.replies import DEFAULT_FALLBACK_REPLY

# mock 拒答触发词（与原型一致）
_MOCK_REFUSAL = re.compile(r"火星|量子|彩票|哪个星球")
_CITE_RE = re.compile(r"\[(\d{1,2})\]")

#: 本地地址（校内自建推理服务 C-05 / 本地网关）——这些地址**不得**经过系统代理。
#:
#: 公开名，供其他模块复用（如 :mod:`app.services.gateway_admin` 做同样的代理绕过判定），
#: 避免同一规则在两处各写一份而产生漂移。
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0"})
#: 兼容模块内既有的下划线引用
_LOCAL_HOSTS = LOCAL_HOSTS

DeltaHandler = Callable[[str], None]

#: 上游网关错误码 → 可执行的中文归因（campus-ai-gateway 的 ``{"error":{"code",...}}``）。
#:
#: 为什么要这张表：接入网关后，同一句「HTTP 403」背后可能是白名单、Key 停用、IP 限制等
#: 完全不同的原因，而它们**处置动作完全不同**。把错误码翻成「去做什么」，
#: 运维才不用去翻网关源码。
GATEWAY_ERROR_HINTS: dict[str, str] = {
    # —— 鉴权 / 授权 ——
    "invalid_api_key": "业务 Key 无效或已停用；请核对 .env 的 LLM_API_KEY（或助手的模型绑定）"
                       "是否为网关签发的 sk-campus-*，并确认网关侧 API_KEY_HMAC_SECRET 与签发时一致",
    "api_key_expired": "业务 Key 已过期；请在网关控制台续期或换一把新 Key，再更新 .env 的 LLM_API_KEY",
    "ip_not_allowed": "调用方 IP 不在该 Key 的 IP 白名单内；请把能小智服务器出口 IP 加入网关该 Key 的白名单",
    "model_not_allowed": "该模型不在这个业务 Key 的白名单（AllowedModels）里；"
                         "请核对该模型的 remoteModel 与网关侧白名单是否逐字一致",
    "tenant_disabled": "该业务 Key 所属租户已被停用；请在网关控制台启用该租户",
    # —— 请求体 ——
    "model_required": "请求未携带 model 字段；请检查该模型配置的 remoteModel 是否为空（网关据此选路由）",
    "model_not_configured": "该模型在网关侧没有可用路由；请在网关控制台为它配置渠道（provider）与路由",
    # —— 限流 / 配额（网关 4b87b6a 起都在「租户」维度，不是 Key 维度）——
    "rate_limit_exceeded": "租户维度 RPM 超限；请在网关控制台调高该租户的 rpm_limit，或降低并发",
    "token_limit_exceeded": "租户维度 TPM 超限；请在网关控制台调高该租户的 tpm_limit",
    "concurrency_limit_exceeded": "租户维度并发超限；请降低并发或在网关控制台调高该租户的 max_concurrency",
    "tenant_request_quota_exceeded": "该租户月度请求数已超限；请在网关控制台调高 monthly_request_limit，"
                                     "或等到下个计费周期",
    "tenant_token_quota_exceeded": "该租户 token 配额已超限（日/月）；"
                                   "请在网关控制台调高 daily_token_limit / monthly_token_limit",
    "enforcement_state_unavailable": "网关的限流状态不可用（Redis 异常或计数重建中），"
                                     "为安全起见拒绝放行；请检查网关侧 Redis 后重试",
    # —— 数据面 / 上游 ——
    "channels_circuit_open": "该模型的所有渠道都在熔断状态（连续失败后被自动隔离）；"
                             "请到网关控制台查看渠道健康，或等熔断冷却后重试",
    "upstream_unavailable": "上游渠道不可达；请检查网关到厂商的网络与 base_url 配置",
    "provider_egress_denied": "网关拒绝连向该渠道的 base_url（出站白名单/私网地址拦截）；"
                              "请在网关控制台把渠道地址改成允许的公网域名",
    "upstream_error": "上游返回错误；请到网关控制台查看该请求的路由尝试链（gateway_request_attempt）",
    "connection_failed": "连不上模型服务；请检查 .env 的 LLM_BASE_URL、网关进程状态与网络策略",
}

#: 常见 HTTP 状态码的兜底归因（上游没给错误码时用）
_HTTP_HINTS: dict[int, str] = {
    400: "上游认为请求体不合法（网关侧常见于 model 为空）",
    401: "上游鉴权失败（Key 无效或未携带）",
    403: "上游拒绝访问（网关侧常见于模型不在 Key 白名单）",
    404: "上游接口路径不存在；请检查 apiBase 是否以 /v1 结尾",
    413: "请求体过大",
    429: "上游限流",
    500: "上游内部错误",
    502: "上游网关异常",
    503: "上游服务不可用",
    504: "上游超时",
}


class LLMError(RuntimeError):
    """LLM 调用异常。

    除可读消息外还携带**结构化**的上游信息，供接入 campus-ai-gateway 后对账：
    ``status_code`` / ``code``（网关错误码）/ ``request_id``（可到
    ``gateway_request_log`` 里定位这一次请求）/ ``model_code``（哪个模型出的问题）。

    这些字段都是**附加**的——``str(exc)`` 的行为保持不变，不破坏既有调用方。
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str = "",
        request_id: str = "",
        model_code: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.request_id = request_id
        self.model_code = model_code

    @property
    def hint(self) -> str:
        """可执行的处置建议（优先按网关错误码，其次按 HTTP 状态码）。"""
        if self.code and self.code in GATEWAY_ERROR_HINTS:
            return GATEWAY_ERROR_HINTS[self.code]
        if self.status_code in _HTTP_HINTS:
            return _HTTP_HINTS[self.status_code]
        return ""

    def to_dict(self) -> dict:
        """结构化输出（供 API 层放进 ``error`` 与 SSE ``error`` 事件）。"""
        return {
            "message": self.message,
            "status": self.status_code,
            "code": self.code or "",
            "requestId": self.request_id or "",
            "modelCode": self.model_code or "",
            "hint": self.hint,
        }


def _first_header_value(raw: str) -> str:
    """同名响应头出现多个时只取第一个。

    **实测发现的坑**：campus-ai-gateway 会生成 ``X-Request-Id: req_*``，
    而它把上游（ThinkWatch Core）的响应**原样透传**，上游自己的
    ``X-Request-Id`` 也一并透出，于是响应里有两个同名头。
    ``httpx`` 对重复头用 ``, `` 拼接，``headers.get()`` 会返回
    ``"req_abc..., tw_xyz..."`` 这样的脏值 —— 直接塞进 ``requestId``
    会让对账失败（网关日志里查不到这个拼接串）。

    网关自己产生的错误（401/403/限流）只有一个头，是干净的；
    只有**上游透传的错误**才有这个问题，因此这里统一归一化。
    """
    if not raw:
        return ""
    return raw.split(",")[0].strip()


def parse_upstream_error(
    status_code: int,
    body: str,
    *,
    request_id: str = "",
    model_code: str = "",
    model: str = "",
) -> LLMError:
    """把上游错误响应解析成 :class:`LLMError`。

    兼容三种形态（顺序即优先级）：

    1. **campus-ai-gateway**：``{"error":{"code","message","request_id"}}``
    2. **OpenAI 官方**：``{"error":{"message","type","code"}}``
    3. 非 JSON / 其它：原文截断，保留原始文本便于排查

    ``request_id`` 形参是响应头 ``X-Request-Id`` 的值——网关在**头里也带**，
    所以即使错误体没给也能对上账。**错误体里的 ``request_id`` 优先**
    （它是网关自己写的，最权威）；头值经 :func:`_first_header_value` 归一化。
    """
    code = ""
    message = ""
    rid = _first_header_value(request_id)

    try:
        payload = json.loads(body) if body else {}
    except (json.JSONDecodeError, TypeError):
        payload = None

    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            code = str(err.get("code") or err.get("type") or "").strip()
            message = str(err.get("message") or "").strip()
            rid = str(err.get("request_id") or rid).strip()
        elif isinstance(err, str):
            message = err.strip()
        if not message:
            message = str(payload.get("message") or "").strip()

    if not message:
        # 非 JSON 或结构不认识：保留原文（截断），否则用户看不到任何线索
        message = (body or "").strip()[:300] or "上游返回了空响应体"

    head = f"LLM 调用失败 HTTP {status_code}"
    if model_code and model_code != "default":
        head += f" 模型「{model_code}」"
    elif model:
        head += f" 模型「{model}」"
    if code:
        head += f" [{code}]"
    text = f"{head}：{message}"
    if rid:
        text += f"（request_id={rid}）"

    return LLMError(
        text,
        status_code=status_code,
        code=code,
        request_id=rid,
        model_code=model_code,
    )


@dataclass
class LLMResponse:
    content: str
    usage: dict = field(default_factory=dict)
    latency_ms: int = 0
    first_token_ms: int | None = None
    model: str = ""


def _norm_usage(usage: dict | None) -> dict:
    u = usage or {}
    prompt = u.get("prompt_tokens", 0) or 0
    completion = u.get("completion_tokens", 0) or 0
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": u.get("total_tokens") or (prompt + completion),
    }


class LLMClient:
    """OpenAI 兼容客户端（默认 DeepSeek）。"""

    def __init__(
        self,
        mock: bool | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        model_code: str = "",
        local: bool = False,
    ) -> None:
        self._mock = mock
        #: 校内 OpenAI 兼容服务通常不校验 Key；``local`` 允许无 Key 调用。
        self._local = bool(local)
        self._api_key = api_key if api_key is not None else settings.llm_api_key
        self._base_url = (base_url or settings.llm_base_url).rstrip("/")
        self._model = model or settings.llm_model
        self._temperature = settings.llm_temperature if temperature is None else temperature
        self._max_tokens = max_tokens or settings.llm_max_tokens
        self._timeout = timeout or settings.llm_timeout
        #: 模型编码（任务 2.4）。仅用于把错误信息说清楚「是哪个模型没配 Key」，
        #: 不影响请求内容——``_model`` 才是下发到 ``/chat/completions`` 的字段。
        self._model_code = (model_code or "").strip()

    def _missing_key_message(self) -> str:
        if settings.llm_provider == "local":
            return (
                "已启用本地模型模式（LLM_PROVIDER=local），但没有可用的本地模型。"
                "请在管理后台「集成中心」新增本地模型并设为当前启用，"
                "或把 LLM_PROVIDER 改回 gateway。"
            )
        if self._model_code and self._model_code != "default":
            return (
                f"模型「{self._model_code}」未配置 API Key。"
                "请在管理后台「模型网关」中确认该模型已在网关白名单内且网关业务 Key 已配置，"
                "或把助手切换到其它可用模型；设 LLM_MOCK=1 可用模拟回答体验全链路。"
            )
        return (
            "未配置 LLM_API_KEY。请写入 kb-service/.env（参照 .env.example），"
            "值为网关签发的业务 Key（sk-campus-*）；"
            "设 LLM_MOCK=1 可用模拟回答体验全链路。"
        )

    # ------------------------------------------------------------------ 状态
    @property
    def mock(self) -> bool:
        return settings.llm_mock if self._mock is None else self._mock

    @property
    def model_code(self) -> str:
        return self._model_code

    @property
    def remote_model(self) -> str:
        return self._model

    @property
    def base_url(self) -> str:
        return self._base_url

    def available(self) -> bool:
        """是否具备调用条件（mock / 已配置 key / 本地模型端点）。"""
        return self.mock or bool(self._api_key) or self._local

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _timeout_obj(self) -> httpx.Timeout:
        return httpx.Timeout(connect=10.0, read=self._timeout, write=30.0, pool=10.0)

    def _trust_env(self) -> bool:
        """本地地址**绕过系统代理**，云端地址仍走代理。

        校园网普遍设 ``HTTP_PROXY``/``HTTPS_PROXY``。httpx 默认 ``trust_env=True``，
        会把 ``127.0.0.1`` 的请求也发给代理——表现为**校内本地模型（C-05）
        无论如何都连不上**（日志里是代理的报错，不是模型服务的报错，很难排查）。
        按 host 判断即可：云端地址保留走代理（部分校园网必须经代理出网）。
        """
        host = (urlparse(self._base_url).hostname or "").lower()
        return host not in _LOCAL_HOSTS

    # ------------------------------------------------------------------ 非流式
    def chat(
        self,
        messages: list[dict],
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
    ) -> LLMResponse:
        if self.mock:
            return self._mock_chat(messages)
        if not self._api_key and not self._local:
            raise LLMError(self._missing_key_message())

        t0 = time.monotonic()
        payload = {
            "model": model or self._model,
            "messages": messages,
            "temperature": self._temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self._max_tokens,
        }
        if top_p is not None:
            # topP 为 None 时**不下发该字段**，交给厂商默认值——
            # 各厂商对 topP 的默认并不一致（多为 1.0），硬填反而改变行为。
            payload["top_p"] = top_p
        try:
            resp = httpx.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=self._headers(),
                timeout=self._timeout_obj(),
                trust_env=self._trust_env(),
            )
        except httpx.HTTPError as exc:
            raise LLMError(
                f"无法连接 LLM 服务（{type(exc).__name__}）：{exc}。"
                f"请检查接口地址「{self._base_url}」是否可达"
                "（接入网关时确认网关进程已启动、端口与网络策略已放通）",
                code="connection_failed",
                model_code=self._model_code,
            ) from exc
        latency = int((time.monotonic() - t0) * 1000)
        if resp.status_code != 200:
            raise parse_upstream_error(
                resp.status_code,
                resp.text,
                request_id=resp.headers.get("X-Request-Id", ""),
                model_code=self._model_code,
                model=model or self._model,
            )

        data = resp.json()
        choices = data.get("choices") or []
        content = (choices[0].get("message", {}).get("content") or "") if choices else ""
        return LLMResponse(
            content=content,
            usage=_norm_usage(data.get("usage")),
            latency_ms=latency,
            first_token_ms=None,
            model=data.get("model") or (model or self._model),
        )

    # ------------------------------------------------------------------ 流式
    def chat_stream(
        self,
        messages: list[dict],
        on_delta: DeltaHandler | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
    ) -> LLMResponse:
        if self.mock:
            return self._mock_chat_stream(messages, on_delta)
        if not self._api_key and not self._local:
            raise LLMError(self._missing_key_message())

        t0 = time.monotonic()
        payload = {
            "model": model or self._model,
            "messages": messages,
            "temperature": self._temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self._max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if top_p is not None:
            payload["top_p"] = top_p
        content = ""
        first_token_ms: int | None = None
        usage: dict = {}
        resp_model = model or self._model

        try:
            with httpx.stream(
                "POST",
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=self._headers(),
                timeout=self._timeout_obj(),
                trust_env=self._trust_env(),
            ) as resp:
                if resp.status_code != 200:
                    resp.read()
                    raise parse_upstream_error(
                        resp.status_code,
                        resp.text,
                        request_id=resp.headers.get("X-Request-Id", ""),
                        model_code=self._model_code,
                        model=model or self._model,
                    )
                for raw in resp.iter_lines():
                    line = raw.strip()
                    if not line.startswith("data:"):
                        continue
                    body = line[5:].strip()
                    if not body or body == "[DONE]":
                        continue
                    try:
                        obj = json.loads(body)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("model"):
                        resp_model = obj["model"]
                    choices = obj.get("choices") or []
                    delta = None
                    if choices:
                        delta = (choices[0].get("delta") or {}).get("content")
                    if delta:
                        if first_token_ms is None:
                            first_token_ms = int((time.monotonic() - t0) * 1000)
                        content += delta
                        if on_delta:
                            on_delta(delta)
                    if obj.get("usage"):
                        usage = obj["usage"]
        except httpx.HTTPError as exc:
            raise LLMError(
                f"无法连接 LLM 服务（{type(exc).__name__}）：{exc}。"
                f"请检查接口地址「{self._base_url}」是否可达"
                "（接入网关时确认网关进程已启动、端口与网络策略已放通）",
                code="connection_failed",
                model_code=self._model_code,
            ) from exc

        return LLMResponse(
            content=content,
            usage=_norm_usage(usage),
            latency_ms=int((time.monotonic() - t0) * 1000),
            first_token_ms=first_token_ms,
            model=resp_model,
        )

    # ------------------------------------------------------------------ mock
    def _mock_chat(self, messages: list[dict]) -> LLMResponse:
        """确定性模拟回答（与原型 mockChat 输出一致）。"""
        user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user = m.get("content") or ""
                break

        ids: list[int] = []
        for mm in _CITE_RE.finditer(user):
            n = int(mm.group(1))
            if n not in ids:
                ids.append(n)
        marks = "".join(f"[{i}]" for i in ids[:2])

        if _MOCK_REFUSAL.search(user):
            content = DEFAULT_FALLBACK_REPLY
        else:
            content = (
                f"（模拟回答，未真实调用大模型）根据知识库资料{marks}："
                "这是基于召回切片拼装的占位答案，用于验证生成层全链路。"
                "配置 LLM_API_KEY 后将由大模型基于资料生成真实回答。"
            )
        return LLMResponse(
            content=content,
            usage={"prompt_tokens": 620, "completion_tokens": 46, "total_tokens": 666},
            latency_ms=1,
            first_token_ms=None,
            # 任务 2.4：mock 下也回显模型编码，便于「问答确实走了该模型」可被观测/断言。
            # 默认客户端（未指定 model_code）仍回 "mock"，与批1 行为一致。
            model=self._model_code or "mock",
        )

    def _mock_chat_stream(
        self, messages: list[dict], on_delta: DeltaHandler | None = None
    ) -> LLMResponse:
        r = self._mock_chat(messages)
        if on_delta:
            for i in range(0, len(r.content), 3):
                on_delta(r.content[i : i + 3])
                time.sleep(0.006)
        return r


_client: LLMClient | None = None


def get_client() -> LLMClient:
    global _client
    # 本地模型模式：每次按「当前启用」构造（切换后立即生效，不缓存旧端点）。
    if settings.llm_provider == "local":
        try:
            from app.services.integration_admin import local_route
            route = local_route(None)
        except Exception:  # noqa: BLE001 - 配置读取失败按不可用处理
            route = None
        if route:
            return LLMClient(
                api_key=route.get("apiKey") or None,
                base_url=route["baseUrl"],
                model=route["remoteModel"],
                timeout=route.get("timeout") or None,
                model_code=route["modelCode"],
                local=bool(route.get("isLocal")),
            )
        return LLMClient(api_key="", model="", base_url=settings.llm_base_url)

    if _client is None:
        model = settings.llm_model
        if not model:
            # LLM_MODEL 省略时自动取网关第一个模型（见 llm_router.default_remote_model）
            try:
                from app.core.llm_router import default_remote_model
                model = default_remote_model()
            except Exception:  # noqa: BLE001
                model = ""
        _client = LLMClient(model=model)
    return _client


def reset_client() -> None:
    """仅供测试：清空单例。"""
    global _client
    _client = None
