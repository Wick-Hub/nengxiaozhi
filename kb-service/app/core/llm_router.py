"""LLM 统一网关：模型路由（任务 2.4 / C-01）。

职责：把 ``agent.model_code`` 解析成一份可用的模型连接配置，并按编码缓存
:class:`~app.core.llm_client.LLMClient` 实例。

## 架构（2026-10 变更）：**模型的唯一来源是网关**

能小智**不再有自己的模型台账**（原 `llm_model` 表与「模型管理」页已下线）。
可用模型清单由网关下发（``GET {LLM_BASE_URL}/models``，受业务 Key 的
``AllowedModels`` 白名单过滤），见 :func:`app.services.gateway_admin.list_remote_models`。

因此路由规则简化为两条：

============  ==================================================
``modelCode``  行为
============  ==================================================
空 / ``default``  **用 ``.env`` 的全局连接配置**（``source="env"``：``LLM_BASE_URL``
                  指向网关、``LLM_API_KEY`` 是网关业务 Key）；``LLM_MODEL`` 可省略，
                  省略时自动取网关 ``/v1/models`` 清单的第一个模型
非 default      用**全局连接配置**（网关地址 + 业务 Key），
                 但把 ``model`` 换成该编码（``source="gateway"``）
============  ==================================================

**关键点：模型编码本身就是网关的模型名。** 网关控制面里的 ``alias`` 就是能小智的
``model_code``，所以这里不做任何本地映射——网关改了别名，能小智填什么就用什么，
不一致时由网关返回 404/400，错误信息里带网关错误码（见 ``llm_client``）。

**为什么不再有「库内查不到就回退」这条分支**：清单来自网关，本地没有第二份台账，
也就没有"本地登记但网关不认"这种状态。校验（该编码是否在网关可用）由
:func:`app.services.gateway_admin.remote_model_ids` 提供，供**绑定/保存时**提示，
而不是每次问答都拦——网关自己会给出权威答复。

**降级链不在本任务范围**：``token_usage.call_state``（``2=降级``）与 ``fallback_from``
两列已建好但**未接线**——C-01 的「主模型失败自动切备用」留给后续任务
（且它本质属于 ThinkWatch Core 的职责）。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from app.config import settings
from app.core.llm_client import LLMClient, get_client

logger = logging.getLogger(__name__)

#: ``agent.model_code`` 的保留编码：走 ``.env`` 的全局默认模型
DEFAULT_MODEL_CODE = "default"

#: 路由来源
SOURCE_ENV = "env"
SOURCE_GATEWAY = "gateway"
SOURCE_LOCAL = "local"
SOURCE_LABELS = {
    SOURCE_ENV: "全局默认(.env)",
    SOURCE_GATEWAY: "网关模型",
    SOURCE_LOCAL: "本地模型",
}

#: 本地模式但没有可用模型时的提示（同时写入 ``ModelRoute.error``）
NO_LOCAL_MODEL = (
    "已启用本地模型模式（LLM_PROVIDER=local），但没有可用的本地模型；"
    "请在「集成中心」新增并启用一个本地模型。"
)


@dataclass
class ModelRoute:
    """一次问答真正使用的模型连接配置。``api_key`` 为**明文**，不得外泄到接口。"""

    model_code: str = DEFAULT_MODEL_CODE
    source: str = SOURCE_ENV
    label: str = ""
    api_key: str = ""
    base_url: str = ""
    remote_model: str = ""
    timeout: float = 0.0
    is_local: int = 0
    error: str | None = None

    @property
    def source_label(self) -> str:
        return SOURCE_LABELS.get(self.source, self.source)

    @property
    def available(self) -> bool:
        """是否具备调用条件（mock 或已配 Key）。本地模型允许无 Key。"""
        return bool(self.api_key) or bool(self.is_local)

    def to_dict(self) -> dict:
        """供管理接口返回。**刻意不含 ``api_key``**——只给「有没有配」。"""
        return {
            "modelCode": self.model_code,
            "source": self.source,
            "sourceLabel": self.source_label,
            "label": self.label,
            "baseUrl": self.base_url,
            "remoteModel": self.remote_model,
            "timeout": self.timeout,
            "isLocal": self.is_local,
            "hasApiKey": bool(self.api_key),
            "available": self.available,
            "error": self.error,
        }


def default_remote_model() -> str:
    """默认路由实际下发给网关的模型名。

    ``.env`` 的 ``LLM_MODEL`` **可省略**：省略时自动取网关 ``/v1/models`` 清单的
    第一个模型（由网关决定默认用哪个，落实「大模型配置全在网关」）。
    已显式配置则优先用配置值，兼容既有部署。
    """
    if settings.llm_model:
        return settings.llm_model
    if str(settings.llm_provider or "gateway").lower() == "local":
        from app.services import integration_admin
        route = integration_admin.local_route(None)
        return route["remoteModel"] if route else ""
    # 懒加载，避免与 gateway_admin 形成模块级循环依赖
    from app.services import gateway_admin
    items = gateway_admin.list_remote_models().get("items") or []
    return items[0]["id"] if items else ""


def _from_local(model_code: str | None) -> ModelRoute:
    """本地模式路由：读「集成中心」当前启用的本地模型。"""
    from app.services import integration_admin

    item = integration_admin.local_route(model_code)
    if item is None:
        return ModelRoute(
            model_code=model_code or DEFAULT_MODEL_CODE,
            source=SOURCE_LOCAL,
            label="（未配置本地模型）",
            error=NO_LOCAL_MODEL,
        )
    enabled = int(item.get("status") or 0) == 1
    return ModelRoute(
        model_code=item["modelCode"],
        source=SOURCE_LOCAL,
        label=item["label"],
        api_key=item.get("apiKey") or "",
        base_url=item["baseUrl"],
        remote_model=item["remoteModel"],
        timeout=float(item.get("timeout") or settings.llm_timeout),
        is_local=1 if enabled else 0,
        error=None if enabled else f"本地模型「{item['label']}」已停用",
    )


def _from_env() -> ModelRoute:
    if str(settings.llm_provider or "gateway").lower() == "local":
        return _from_local(None)
    model = default_remote_model()
    return ModelRoute(
        model_code=DEFAULT_MODEL_CODE,
        source=SOURCE_ENV,
        label=model or "（跟随网关默认）",
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        remote_model=model,
        timeout=settings.llm_timeout,
        is_local=0,
    )


def _from_gateway(model_code: str) -> ModelRoute:
    """网关模型：连接参数取全局配置，``model`` 换成该编码。

    模型编码**就是**网关的模型名（alias），因此不做本地映射。
    网关侧改了别名/白名单，能小智这边立刻跟随，不需要二次维护。
    """
    if str(settings.llm_provider or "gateway").lower() == "local":
        return _from_local(model_code)
    return ModelRoute(
        model_code=model_code,
        source=SOURCE_GATEWAY,
        label=model_code,
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        remote_model=model_code,
        timeout=settings.llm_timeout,
        is_local=0,
    )


def resolve(model_code: str | None) -> ModelRoute:
    """解析模型编码 → :class:`ModelRoute`。**任何异常都降级，不抛**。"""
    code = str(model_code or "").strip()
    if not code or code == DEFAULT_MODEL_CODE:
        return _from_env()
    return _from_gateway(code)


# ------------------------------------------------------------------ 客户端缓存
#: 按 ``model_code`` 缓存客户端。键为 ``""`` 表示 ``.env`` 全局默认。
_clients: dict[str, LLMClient] = {}
_route_cache: dict[str, ModelRoute] = {}
_lock = threading.RLock()

#: 测试/运维钩子：非 ``None`` 时 :func:`get_client_for` 直接返回它（**跳过缓存与路由**）
_override: LLMClient | None = None


def get_client_for(model_code: str | None) -> LLMClient:
    """按模型编码取客户端（带缓存）。``.env`` 默认走 ``llm_client.get_client()``。"""
    if _override is not None:
        return _override

    code = str(model_code or "").strip()
    if not code or code == DEFAULT_MODEL_CODE:
        # 本地模式：``get_client`` 每次按当前启用模型构造，切换后立即生效。
        _route_cache[""] = _from_env()
        return get_client()

    with _lock:
        cached = _clients.get(code)
        if cached is not None:
            return cached
        route = resolve(code)
        _route_cache[code] = route
        client = LLMClient(
            api_key=route.api_key or None,
            base_url=route.base_url,
            model=route.remote_model,
            timeout=route.timeout,
            model_code=route.model_code,
            local=bool(route.is_local),
        )
        _clients[code] = client
        return client


def route_of(model_code: str | None) -> ModelRoute:
    """取路由信息（供管理接口展示「实际走哪个模型」）。不构造客户端。"""
    code = str(model_code or "").strip()
    if not code or code == DEFAULT_MODEL_CODE:
        return _from_env()
    with _lock:
        if code in _route_cache:
            return _route_cache[code]
    route = resolve(code)
    with _lock:
        _route_cache[code] = route
    return route


def set_override(client: LLMClient | None) -> None:
    """测试钩子：强制所有模型都走同一个客户端（传 ``None`` 取消）。"""
    global _override
    _override = client


def reset_routes() -> None:
    """清空路由与客户端缓存。

    **改了 `.env`（网关地址 / 业务 Key / 默认模型）后必须调用**，否则会拿着旧的
    base_url/api_key 继续请求。网关侧改了模型清单**不需要**调它——
    清单由 :mod:`app.services.gateway_admin` 单独缓存（30s TTL），
    且模型编码不做本地映射，网关改了立即生效。
    """
    global _override
    with _lock:
        _clients.clear()
        _route_cache.clear()
    _override = None
