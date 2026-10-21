"""模型网关（campus-ai-gateway）的可观测性与入口信息。

## 设计前提：**不持有网关管理员凭据**

网关的管理面（`/admin/*`）需要管理员凭据；业务面（`/v1/*`）需要业务 Key。
本模块**都用手不到**——它只调用**无需鉴权**的 `/healthz` 来判断可达性，
因此能小智侧不需要新增任何高权限凭据（凭据越多，泄露面越大）。

能拿到的信息因此是有限但够用的：

* 网关**是否在线**（`/healthz` → 200 + ``ok`` 这类简短响应）
* 响应**延迟**（毫秒）
* **控制台入口地址**（`<origin>/admin-ui/`）
* 库内各模型**指向哪个网关**、`remoteModel` 是什么（用于核对白名单）

拿不到的（有意不做）：今日请求数 / token 用量 / 错误数 / 熔断次数——
这些只在 `/admin/status` 里，要管理员凭据。需要时请直接打开网关控制台。

## 「形似网关」怎么判断

用**排除法**：主机名不属于已知厂商直连域名（`:data:`VENDOR_HOSTS`）就视为
自建服务/网关。这比"猜测它是网关"更稳——厂商域名是有限的确定集合。

## 安全

端点地址来自**受信来源**（`.env` 的 `LLM_BASE_URL` 或管理页配置的模型记录，
后者本身就在 `require_admin` 之后），不是终端用户输入，因此这里的 HTTP 探测
不构成 SSRF 面。仍只允许 ``http``/``https``，并统一设短超时，避免管理页被拖住。
"""

from __future__ import annotations

import logging
import time
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.core.llm_client import LOCAL_HOSTS

logger = logging.getLogger(__name__)

#: 已知厂商直连域名 → 中文标签。命中即说明**没走网关**。
VENDOR_HOSTS: dict[str, str] = {
    "api.deepseek.com": "DeepSeek 直连",
    "ark.cn-beijing.volces.com": "火山方舟直连",
    "dashscope.aliyuncs.com": "阿里百炼直连",
    "open.bigmodel.cn": "智谱直连",
    "api.moonshot.cn": "Kimi 直连",
    "api.openai.com": "OpenAI 直连",
}

#: 网关健康检查路径（无需鉴权）
HEALTH_PATH = "/healthz"
#: 网关内嵌控制台路径（React Admin UI）
CONSOLE_PATH = "/admin-ui/"
#: 探测超时（秒）。刻意偏短：管理页不该因为一个不通的地址卡住。
PROBE_TIMEOUT = 3.0

#: 网关错误码 → 可执行的中文处置建议。
#:
#: 这份表是**权威定义**：`app/core/llm_client.py` 与本模块描述的是同一批错误码，
#: 但那边是"调用失败时给用户看"，这里是"管理页给运维看"，措辞侧重不同。
#: 工具 `tools/verify_gateway.py` 直接从这里导入，避免三处各写一份而漂移。
GATEWAY_ERROR_HINTS: dict[str, str] = {
    # —— 鉴权 / 授权 ——
    "invalid_api_key": "业务 Key 无效或已停用；请核对该模型填的是否网关签发的 sk-campus-*，"
                       "并确认网关侧 API_KEY_HMAC_SECRET 与签发时一致",
    "api_key_expired": "业务 Key 已过期；请在网关控制台续期或换一把新 Key",
    "ip_not_allowed": "调用方 IP 不在该 Key 的 IP 白名单内；请把能小智服务器出口 IP 加入网关该 Key 的白名单",
    "model_not_allowed": "该模型不在这个业务 Key 的白名单（AllowedModels）里；"
                         "请核对 remoteModel 与网关侧白名单是否逐字一致",
    "tenant_disabled": "该业务 Key 所属租户已被停用；请在网关控制台启用该租户",
    # —— 请求体 ——
    "model_required": "请求未携带 model 字段；请检查该模型的 remoteModel 是否为空（网关据此选路由）",
    "model_not_configured": "该模型在网关侧没有可用路由；请在网关控制台为它配置渠道与路由",
    # —— 限流 / 配额（网关 4b87b6a 起都在「租户」维度，不是 Key 维度）——
    "rate_limit_exceeded": "租户维度 RPM 超限；请在网关控制台调高该租户的 rpm_limit，或降低并发",
    "token_limit_exceeded": "租户维度 TPM 超限；请在网关控制台调高该租户的 tpm_limit",
    "concurrency_limit_exceeded": "租户维度并发超限；请降低并发或在网关控制台调高该租户的 max_concurrency",
    "tenant_request_quota_exceeded": "该租户月度请求数已超限；请在网关控制台调高 monthly_request_limit，"
                                     "或等到下个计费周期",
    "tenant_token_quota_exceeded": "该租户 token 配额已超限（日/月）；"
                                   "请在网关控制台调高 daily_token_limit / monthly_token_limit",
    "enforcement_state_unavailable": "网关限流状态不可用（Redis 异常或计数重建中），"
                                     "为安全起见拒绝放行；请检查网关侧 Redis 后重试",
    # —— 数据面 / 上游 ——
    "channels_circuit_open": "该模型的所有渠道都在熔断状态；请到网关控制台查看渠道健康，"
                             "或等熔断冷却后重试",
    "upstream_unavailable": "上游渠道不可达；请检查网关到厂商的网络与 base_url 配置",
    "provider_egress_denied": "网关拒绝连向该渠道的 base_url（出站白名单/私网地址拦截）；"
                              "请在网关控制台把渠道地址改成允许的公网域名",
    "upstream_error": "上游返回错误；请到网关控制台查看该请求的路由尝试链（gateway_request_attempt）",
    "connection_failed": "连不上模型服务；请检查 .env 的 LLM_BASE_URL、网关进程状态与网络策略",
}


def origin_of(url: str) -> str:
    """``http://gw:8080/v1`` → ``http://gw:8080``。

    健康检查与控制台都在**源站根**上（不是 ``/v1`` 下），所以这里只取
    scheme + netloc。这样无论配置里带不带 ``/v1`` 都能得到正确地址。
    """
    try:
        parsed = urlparse(str(url or "").strip())
    except Exception:  # noqa: BLE001
        return ""
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def host_of(url: str) -> str:
    try:
        return (urlparse(str(url or "")).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""


def _trust_env_for(url: str) -> bool:
    """本地地址绕过系统代理（与 :mod:`app.core.llm_client` 同一规则）。

    校园网常有 ``HTTP_PROXY``，若把 ``127.0.0.1`` 也转发出去，
    本地网关会探测不到（表现为"明明起着却显示离线"）。
    """
    return host_of(url) not in LOCAL_HOSTS


def is_gateway_like(url: str) -> bool:
    """主机名不属于已知厂商域名 → 视为自建服务/网关。"""
    host = host_of(url)
    return bool(host) and host not in VENDOR_HOSTS


def probe_gateway(origin: str, *, timeout: float = PROBE_TIMEOUT) -> dict:
    """探测一个地址上是否有**网关**在跑。**不抛异常** —— 探测失败是一种结果。

    返回的 ``state`` 是三态，刻意不合并成"通/不通"：

    * ``online`` —— ``/healthz`` 返回 200，确认是网关且健康；
    * ``unknown`` —— 地址**有响应**但不是 200。说明有服务在监听，只是它没有
      标准的网关健康端点（本地推理服务 vLLM/Ollama 等常见如此）。
      **不能报成"离线"**，那会让运维去查一个其实活着的服务。
    * ``offline`` —— 连接失败，该地址上没有任何服务。

    :return: ``{"state", "reachable", "status", "latencyMs", "error"}``
    """
    if not origin:
        return {"state": "offline", "reachable": False, "status": None, "latencyMs": None,
                "error": "接口地址不可用（需要形如 http://主机:端口 的地址）"}
    url = f"{origin}{HEALTH_PATH}"
    t0 = time.monotonic()
    try:
        resp = httpx.get(url, timeout=timeout, trust_env=_trust_env_for(url))
    except httpx.HTTPError as exc:
        return {
            "state": "offline",
            "reachable": False,
            "status": None,
            "latencyMs": int((time.monotonic() - t0) * 1000),
            "error": f"{type(exc).__name__}：{exc}",
        }
    latency = int((time.monotonic() - t0) * 1000)
    if resp.status_code == 200:
        return {"state": "online", "reachable": True, "status": 200,
                "latencyMs": latency, "error": None}
    return {
        "state": "unknown",
        "reachable": True,  # 有响应就算可达，只是无法确认是网关
        "status": resp.status_code,
        "latencyMs": latency,
        "error": f"{HEALTH_PATH} 返回 HTTP {resp.status_code}（该地址有服务，"
                 f"但未提供网关健康端点；本地推理服务常见如此）",
    }


# ------------------------------------------------------------------ 端点收集
#: 远程模型清单的缓存 TTL（秒）。
#:
#: 为什么需要缓存：模型清单会被「助手绑定下拉」「模型网关页」「绑定校验」反复读取，
#: 而它是**跨进程 HTTP 调用**。但也不能永久缓存——网关侧改了白名单要能及时反映。
#: 30 秒是折中：正常操作几乎无感，改配置后最多等半分钟（或点页面上的「刷新」）。
MODELS_TTL = 30.0
#: ``{"at": 时间戳, "items": [...], "error": str|None, "base": str}``
_models_cache: dict = {"at": 0.0, "items": [], "error": None, "base": ""}


def _models_url() -> str:
    base = str(settings.llm_base_url or "").rstrip("/")
    return f"{base}/models" if base else ""


def list_remote_models(*, refresh: bool = False) -> dict:
    """拉取**网关**的模型清单 —— 这是能小智模型来源的唯一权威。

    走 OpenAI 标准的 ``GET {LLM_BASE_URL}/models``，用 `.env` 里的**业务 Key** 鉴权。
    网关只会返回该 Key 的 ``AllowedModels`` 白名单内的模型，
    所以「能小智能用哪些模型」完全由网关决定，能小智侧不做任何本地登记。

    **不抛异常**：网关不可达时返回 ``error`` 并把上次的清单（如有）一并带回，
    让上层决定是「提示」还是「降级」——模型清单拉不到不该让问答整体失败。

    :return: ``{"items":[{"id","ownedBy"}], "error", "cachedAt", "stale", "baseUrl"}``
    """
    import time as _time

    url = _models_url()
    if not url:
        return {"items": [], "error": "未配置 LLM_BASE_URL（网关地址）",
                "cachedAt": 0.0, "stale": True, "baseUrl": ""}

    now = _time.monotonic()
    fresh = (
        not refresh
        and _models_cache["base"] == url
        and (now - _models_cache["at"]) < MODELS_TTL
    )
    if fresh:
        return {
            "items": _models_cache["items"],
            "error": _models_cache["error"],
            "cachedAt": _models_cache["at"],
            "stale": False,
            "baseUrl": url,
        }

    headers = {"Content-Type": "application/json"}
    if settings.llm_api_key:
        headers["Authorization"] = f"Bearer {settings.llm_api_key}"

    try:
        resp = httpx.get(url, headers=headers, timeout=PROBE_TIMEOUT,
                         trust_env=_trust_env_for(url))
    except httpx.HTTPError as exc:
        # 拉取失败 → 保留上次成功的结果（宁可用过期清单，也不要空清单）
        err = f"无法获取网关模型清单（{type(exc).__name__}）：{exc}"
        _models_cache.update({"at": now, "error": err, "base": url})
        logger.warning("%s", err)
        return {"items": _models_cache["items"], "error": err,
                "cachedAt": now, "stale": True, "baseUrl": url}

    if resp.status_code != 200:
        err = (f"网关模型清单返回 HTTP {resp.status_code}"
               f"（401/403 通常是业务 Key 无效或白名单为空）")
        _models_cache.update({"at": now, "error": err, "base": url})
        logger.warning("%s", err)
        return {"items": _models_cache["items"], "error": err,
                "cachedAt": now, "stale": True, "baseUrl": url}

    try:
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        err = f"网关模型清单不是合法 JSON：{exc}"
        _models_cache.update({"at": now, "error": err, "base": url})
        return {"items": [], "error": err, "cachedAt": now, "stale": True, "baseUrl": url}

    raw = payload.get("data") if isinstance(payload, dict) else None
    items: list[dict] = []
    for entry in raw or []:
        if isinstance(entry, dict):
            mid = str(entry.get("id") or "").strip()
            if not mid:
                continue
            items.append({"id": mid, "ownedBy": str(entry.get("owned_by") or "")})
        elif isinstance(entry, str) and entry.strip():
            items.append({"id": entry.strip(), "ownedBy": ""})

    _models_cache.update({"at": now, "items": items, "error": None, "base": url})
    return {"items": items, "error": None, "cachedAt": now, "stale": False, "baseUrl": url}


def remote_model_ids(*, refresh: bool = False) -> set[str]:
    """只要 id 集合（供绑定校验用）；拉取失败时返回空集合。"""
    return {m["id"] for m in list_remote_models(refresh=refresh)["items"]}


def reset_models_cache() -> None:
    """清空清单缓存（配置变更/测试用）。"""
    _models_cache.update({"at": 0.0, "items": [], "error": None, "base": ""})


# ------------------------------------------------------------------ 绑定辅助
def choices() -> dict:
    """助手「绑定模型」下拉的数据源。

    ``default`` 永远在第一项（它是保留编码 = 走 `.env` 全局默认模型），
    其余全部来自网关清单 —— **能小智不维护第二份模型列表**。
    """
    from app.core.llm_router import DEFAULT_MODEL_CODE, SOURCE_ENV

    if str(settings.llm_provider or "gateway").lower() == "local":
        from app.services import integration_admin

        items = integration_admin.local_choices()
        return {
            "items": items,
            "error": None if items else "本地模型模式尚无可用模型，请在「集成中心」配置",
            "stale": False,
            "baseUrl": "",
            "provider": "local",
        }

    items = [
        {
            "modelCode": DEFAULT_MODEL_CODE,
            "modelName": (
                f"跟随网关默认（{settings.llm_model}）"
                if settings.llm_model
                else "跟随网关默认（自动取网关第一个模型）"
            ),
            "source": SOURCE_ENV,
            "available": bool(settings.llm_api_key),
        }
    ]
    models = list_remote_models()
    for m in models["items"]:
        items.append({
            "modelCode": m["id"],
            "modelName": m["id"],
            "source": "gateway",
            "ownedBy": m.get("ownedBy") or "",
            "available": bool(settings.llm_api_key),
        })
    return {
        "items": items,
        "error": models["error"],
        "stale": models.get("stale", False),
        "baseUrl": models.get("baseUrl", ""),
    }


def unknown_binding(model_code: str) -> str | None:
    """``model_code`` 若不在网关清单里，返回**可读的提示**；否则 ``None``。

    刻意只在**保存/绑定**时提示，不在每次问答时拦截：
    - 保存时提示能立刻纠错，体验最好；
    - 问答时不拦是有意的——网关才是权威，它会给带错误码的明确答复
      （如 404/`model_not_allowed`），本地多拦一层反而会掩盖真实原因。
    """
    code = str(model_code or "").strip()
    if not code or code == "default":
        return None

    models = list_remote_models()
    if models["error"]:
        # 清单拉不到时**不判非法** —— 网关暂时不可达不等于模型不存在
        return None
    known = {m["id"] for m in models["items"]}
    if code in known:
        return None
    if not known:
        return (f"网关当前未返回任何可用模型（业务 Key 的白名单可能为空），"
                f"无法确认「{code}」是否可用")
    return (f"模型「{code}」不在网关下发的可用清单中："
            f"{'、'.join(sorted(known))}。"
            f"请先在网关控制台把它加入业务 Key 的 AllowedModels 白名单。")


def collect_endpoints() -> list[dict]:
    """当前配置指向的模型服务端点（现在是**单一来源**：`.env` 的 ``LLM_BASE_URL``）。

    架构变更后能小智不再有自己的模型记录（模型清单由网关下发），
    所以这里只有一个端点。保留列表形状是为了兼容调用方与前端渲染。
    """
    seen: dict[str, dict] = {}

    base = str(settings.llm_base_url or "").strip()
    origin = origin_of(base)
    if base and origin:
        host = host_of(origin)
        seen[origin] = {
            "baseUrl": base,
            "origin": origin,
            "host": host,
            "source": "env",
            "label": "全局配置（.env）",
            "isGateway": is_gateway_like(origin),
            "vendorLabel": VENDOR_HOSTS.get(host, ""),
            "consoleUrl": f"{origin}{CONSOLE_PATH}",
        }

    return list(seen.values())


def gateway_overview(refresh_models: bool = False) -> dict:
    """管理页「模型网关」页签的数据源。

    三块内容：
    1. **网关可达性**（只对形似网关的端点做真实探测；厂商直连不探——它可能因
       未配 Key 返回 401，那不代表"离线"）；
    2. **网关下发的模型清单** —— 这是能小智可用模型的**唯一来源**
       （``GET {LLM_BASE_URL}/models``，受业务 Key 白名单过滤）；
    3. **控制台入口 + 错误码对照**。
    """
    endpoints = collect_endpoints()
    models = list_remote_models(refresh=refresh_models)

    for ep in endpoints:
        if ep["isGateway"]:
            ep["probe"] = probe_gateway(ep["origin"])
            ep["status"] = ep["probe"]["state"]
        else:
            # 厂商直连：不做健康探测，只标注「未走网关」
            ep["probe"] = None
            ep["status"] = "vendor-direct"

    gateways = [e for e in endpoints if e["isGateway"]]
    online = [e for e in gateways if e["status"] == "online"]
    console = online[0]["consoleUrl"] if online else (
        gateways[0]["consoleUrl"] if gateways else ""
    )

    return {
        # 显式声明：本页不持有网关凭据，所以看不到配额/用量类指标
        "credentialFree": True,
        "endpoints": endpoints,
        "gatewayCount": len(gateways),
        "onlineCount": len(online),
        "consoleUrl": console,
        "hint": _hint_for(gateways, online, models),
        # **模型清单的唯一来源**：网关 `GET /models`（受业务 Key 白名单过滤）。
        # 能小智侧不落库、不维护第二份清单。
        "models": models["items"],
        "modelsError": models["error"],
        "modelsStale": models.get("stale", False),
        "modelsBaseUrl": models.get("baseUrl", ""),
        # 错误码对照表（与凭据无关，供运维自查；无需去翻网关源码）
        "errorHints": [
            {"code": code, "hint": text} for code, text in GATEWAY_ERROR_HINTS.items()
        ],
    }


def _hint_for(gateways: list[dict], online: list[dict], models: dict) -> str:
    """给出**当前最该做的那一件事**，而不是罗列所有可能性。"""
    if not gateways:
        return ("当前接口地址指向厂商直连，未经过网关。"
                "本平台只从网关获取模型清单，请把 .env 的 LLM_BASE_URL 改为网关地址。")
    if not online:
        unknowable = [e for e in gateways if e["status"] == "unknown"]
        if unknowable:
            who = "、".join(e["origin"] for e in unknowable)
            return (f"{who} 有服务在监听，但没有网关健康端点（/healthz 非 200）——"
                    "可能是本地推理服务而非 campus-ai-gateway；"
                    "若是网关，请确认进程版本与端口是否正确。")
        return ("检测到网关地址但连不上。请确认网关进程已启动、端口与网络策略已放通；"
                "本页通过无需鉴权的 /healthz 探测。")

    items = models.get("items") or []
    if models.get("error"):
        return (f"网关在线，但**模型清单拉取失败**：{models['error']}。"
                "问答时会因取不到模型而失败，请先修好这一项。")
    if not items:
        return ("网关在线，但该业务 Key 的模型清单**为空** —— "
                "请在网关控制台把模型加入这把 Key 的 AllowedModels 白名单。")
    return (f"已从网关获取 {len(items)} 个可用模型；助手的「绑定模型」从此清单中选择。"
            "若某个模型调用报 403 model_not_allowed，说明它已从网关白名单移除。")
