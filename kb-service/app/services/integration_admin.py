"""集成中心服务（任务 4.8 / C-05、M-A-07/10）。

三类配置统一管理：

* **本地大模型**：OpenAI 兼容端点（``/models`` + ``/chat/completions``），
  可设一个「当前启用」；``LLM_PROVIDER=local`` 时问答直连该端点。
* **MCP 服务**：HTTP / SSE / stdio 三种传输的配置；本版本对 HTTP 执行
  ``initialize`` + ``tools/list`` 真实探测，其余传输只保存配置并给出说明。
* **第三方智能体**：Dify / Coze / 自定义 Webhook，测试时真实发一条
  ``请回复：pong`` 的消息并回显回答。

测试适配器有 **mock / live** 两种来源（``INTEGRATION_*_PROVIDER``）：
外部资源未就绪时用 mock 全链路验收；接口就绪后切 live，不改代码。

密钥一律经 :mod:`app.security.crypto` 加密落库，公开结构只给 ``hasKey`` /
``hasToken`` / ``hasHeaders`` 与 Header **名称**，不返回任何明文或前缀。
"""

from __future__ import annotations

import json
import re
import time
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.core.llm_client import LOCAL_HOSTS
from app.db import integration_store as store
from app.security.crypto import CryptoError, decrypt_field, encrypt_field

MAX_CODE = 64
MAX_NAME = 128
MAX_URL = 512
MAX_REMARK = 255
MAX_SECRET = 512
CODE_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
HEADER_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
MCP_TRANSPORTS = ("http", "sse", "stdio")
AGENT_KINDS = ("dify", "coze", "webhook")
AGENT_LABELS = {"dify": "Dify 应用", "coze": "Coze 智能体", "webhook": "自定义 Webhook"}

#: 示例数据统一标记（交付要求：样例必须可识别、可替换）
SAMPLE_REMARK = "示例数据（待替换为学校真实配置）"


def _ok(data) -> dict:
    return {"ok": True, "data": data, "error": None}


def _fail(message: str, status: int = 400) -> dict:
    return {"ok": False, "data": None, "error": message, "status": status}


# ------------------------------------------------------------------ 校验
def _clean_code(raw) -> tuple[str | None, str | None]:
    code = str(raw or "").strip().lower()
    if not code:
        return None, "编码不能为空"
    if not CODE_RE.match(code):
        return None, "编码需以小写字母开头，仅含小写字母/数字/下划线/短横线（2–64 位）"
    if len(code) > MAX_CODE:
        return None, f"编码不能超过 {MAX_CODE} 个字符"
    return code, None


def _clean_text(raw, label: str, *, required: bool = True, limit: int = MAX_NAME):
    text = str(raw if raw is not None else "").strip()
    if not text:
        return ("", None) if not required else (None, f"{label}不能为空")
    if len(text) > limit:
        return None, f"{label}不能超过 {limit} 个字符"
    return text, None


def _clean_url(raw, label: str, *, required: bool = True) -> tuple[str | None, str | None]:
    text = str(raw if raw is not None else "").strip()
    if not text:
        return ("", None) if not required else (None, f"{label}不能为空")
    if len(text) > MAX_URL:
        return None, f"{label}不能超过 {MAX_URL} 个字符"
    if re.search(r"[\s\u0000-\u001f\u007f]", text):
        return None, f"{label}不能包含空白或控制字符"
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None, f"{label}需以 http(s):// 开头并包含主机名"
    return text.rstrip("/"), None


def _clean_status(raw, default: int = 1) -> tuple[int | None, str | None]:
    if raw is None:
        return default, None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None, "状态必须是 0 或 1"
    if value not in (0, 1):
        return None, "状态必须是 0 或 1"
    return value, None


def _clean_args(raw) -> tuple[list[str] | None, str | None]:
    if raw is None:
        return [], None
    if not isinstance(raw, list) or len(raw) > 20:
        return None, "参数需为字符串数组（最多 20 项）"
    args: list[str] = []
    for entry in raw:
        text = str(entry)
        if "\x00" in text or len(text) > 200:
            return None, "单个参数不能超过 200 个字符且不能包含空字符"
        args.append(text)
    return args, None


def _clean_headers(raw) -> tuple[dict | None, str | None]:
    """返回 ``(headers, error)``；``None`` 表示「未提供，保持原值」。"""
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        return None, "Headers 需为对象（键值均为字符串）"
    if len(raw) > 30:
        return None, "Headers 最多 30 项"
    result: dict[str, str] = {}
    for key, value in raw.items():
        name = str(key or "").strip()
        text = str(value if value is not None else "")
        if not HEADER_RE.match(name):
            return None, f"Header 名称不合法：{name or '(空)'}"
        if re.search(r"[\r\n\u0000]", text) or len(text) > 1024:
            return None, f"Header「{name}」的值不能包含换行且不能超过 1024 字符"
        result[name] = text
    return result, None


def _secret_of(item: dict) -> str:
    try:
        return decrypt_field(item.get("_secret")) or ""
    except CryptoError:
        return ""


def _header_names(item: dict) -> list[str]:
    raw = _secret_of(item)
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return sorted(str(k) for k in data) if isinstance(data, dict) else []
    except Exception:  # noqa: BLE001
        return []


def _store_headers(headers: dict | None) -> bytes | None:
    if headers is None:
        return None
    if not headers:
        return None
    return encrypt_field(json.dumps(headers, ensure_ascii=False))


# ------------------------------------------------------------------ 公开结构
def public_local(item: dict) -> dict:
    return {
        key: item.get(key)
        for key in (
            "localId", "code", "name", "baseUrl", "remoteModel", "timeout",
            "status", "active", "remark", "lastTestAt", "lastTestState",
            "lastTestError", "lastLatencyMs", "createdAt", "updatedAt",
        )
    } | {"hasKey": bool(item.get("_secret"))}


def public_mcp(item: dict) -> dict:
    tools = item.get("tools") if isinstance(item.get("tools"), list) else []
    return {
        key: item.get(key)
        for key in (
            "serverId", "code", "name", "transport", "endpoint", "command", "args",
            "status", "remark", "lastTestAt", "lastTestState", "lastTestError",
            "lastLatencyMs", "createdAt", "updatedAt",
        )
    } | {
        "hasHeaders": bool(item.get("_secret")),
        "headerNames": _header_names(item),
        "tools": tools,
        "toolCount": len(tools),
    }


def public_agent(item: dict) -> dict:
    return {
        key: item.get(key)
        for key in (
            "appId", "code", "name", "kind", "baseUrl", "remoteAppId", "status",
            "remark", "lastTestAt", "lastTestState", "lastTestError", "lastLatencyMs",
            "lastReply", "createdAt", "updatedAt",
        )
    } | {"hasToken": bool(item.get("_secret")), "kindLabel": AGENT_LABELS.get(item.get("kind") or "", "")}


# ------------------------------------------------------------------ 本地模型 CRUD
def list_local_models() -> list[dict]:
    return [public_local(i) for i in store.list_local_models()]


def _validate_local(payload: dict, existing: dict | None) -> tuple[dict | None, str | None]:
    data = dict(existing or {})
    for key in ("code", "name", "baseUrl", "remoteModel", "timeout", "status", "remark"):
        if key in payload:
            data[key] = payload[key]

    code, err = _clean_code(data.get("code"))
    if err:
        return None, err
    name, err = _clean_text(data.get("name"), "名称")
    if err:
        return None, err
    url, err = _clean_url(data.get("baseUrl"), "接口地址")
    if err:
        return None, err
    model, err = _clean_text(data.get("remoteModel"), "模型名", limit=MAX_NAME)
    if err:
        return None, err
    try:
        timeout = float(data.get("timeout") if data.get("timeout") is not None else 30)
    except (TypeError, ValueError):
        return None, "超时必须是数字"
    if not 1 <= timeout <= 600:
        return None, "超时需在 1–600 秒之间"
    status, err = _clean_status(data.get("status"), int(data.get("status", 1)))
    if err:
        return None, err
    remark, err = _clean_text(data.get("remark"), "备注", required=False, limit=MAX_REMARK)
    if err:
        return None, err
    return {
        "code": code, "name": name, "baseUrl": url, "remoteModel": model,
        "timeout": timeout, "status": status, "remark": remark or "",
    }, None


def create_local_model(payload: dict) -> dict:
    fields, err = _validate_local(payload, None)
    if err:
        return _fail(err)
    if store.find_local_model_by_code(fields["code"]):
        return _fail("该编码已被使用", 409)
    raw_key = payload.get("apiKey")
    if raw_key is not None and len(str(raw_key)) > MAX_SECRET:
        return _fail(f"API Key 不能超过 {MAX_SECRET} 个字符")
    fields["_secret"] = encrypt_field(str(raw_key)) if raw_key else None
    fields["active"] = 0
    item = store.create_local_model(fields)
    if payload.get("active"):
        store.activate_local_model(item["localId"])
        item = store.get_local_model(item["localId"]) or item
    return _ok(public_local(item))


def update_local_model(local_id: int, payload: dict) -> dict:
    existing = store.get_local_model(local_id)
    if existing is None:
        return _fail("本地模型不存在", 404)
    fields, err = _validate_local(payload, existing)
    if err:
        return _fail(err)
    if fields["code"] != existing["code"] and store.find_local_model_by_code(fields["code"]):
        return _fail("该编码已被使用", 409)
    if "apiKey" in payload:
        raw_key = payload.get("apiKey")
        if raw_key is not None and len(str(raw_key)) > MAX_SECRET:
            return _fail(f"API Key 不能超过 {MAX_SECRET} 个字符")
        fields["_secret"] = encrypt_field(str(raw_key)) if raw_key else None
    item = store.update_local_model(local_id, fields) or existing
    return _ok(public_local(item))


def delete_local_model(local_id: int) -> dict:
    if not store.delete_local_model(local_id):
        return _fail("本地模型不存在", 404)
    return _ok({"deleted": True, "localId": int(local_id)})


def activate_local_model(local_id: int) -> dict:
    if not store.activate_local_model(local_id):
        return _fail("本地模型不存在", 404)
    return _ok({"activated": True, "localId": int(local_id)})


# ------------------------------------------------------------------ MCP CRUD
def list_mcp_servers() -> list[dict]:
    return [public_mcp(i) for i in store.list_mcp_servers()]


def _validate_mcp(payload: dict, existing: dict | None) -> tuple[dict | None, str | None]:
    data = dict(existing or {})
    for key in ("code", "name", "transport", "endpoint", "command", "args", "status", "remark"):
        if key in payload:
            data[key] = payload[key]

    code, err = _clean_code(data.get("code"))
    if err:
        return None, err
    name, err = _clean_text(data.get("name"), "名称")
    if err:
        return None, err
    transport = str(data.get("transport") or "http").strip().lower()
    if transport not in MCP_TRANSPORTS:
        return None, "传输方式仅支持 http / sse / stdio"
    endpoint, err = _clean_url(data.get("endpoint"), "Endpoint", required=transport != "stdio")
    if err:
        return None, err
    command = str(data.get("command") or "").strip()
    if transport == "stdio":
        if not command:
            return None, "stdio 传输需填写启动命令"
        if len(command) > 255 or re.search(r"[\s;|&`$<>]", command):
            return None, "启动命令不能包含空白或 shell 元字符（参数请填在 args）"
    args, err = _clean_args(data.get("args"))
    if err:
        return None, err
    status, err = _clean_status(data.get("status"), int(data.get("status", 1)))
    if err:
        return None, err
    remark, err = _clean_text(data.get("remark"), "备注", required=False, limit=MAX_REMARK)
    if err:
        return None, err
    return {
        "code": code, "name": name, "transport": transport,
        "endpoint": endpoint or "" if transport != "stdio" else "",
        "command": command if transport == "stdio" else "",
        "args": args if transport == "stdio" else [],
        "status": status, "remark": remark or "",
    }, None


def create_mcp_server(payload: dict) -> dict:
    fields, err = _validate_mcp(payload, None)
    if err:
        return _fail(err)
    if any(i["code"] == fields["code"] for i in store.list_mcp_servers()):
        return _fail("该编码已被使用", 409)
    headers, err = _clean_headers(payload.get("headers"))
    if err:
        return _fail(err)
    fields["_secret"] = _store_headers(headers)
    item = store.create_mcp_server(fields)
    return _ok(public_mcp(item))


def update_mcp_server(server_id: int, payload: dict) -> dict:
    existing = store.get_mcp_server(server_id)
    if existing is None:
        return _fail("MCP 服务不存在", 404)
    fields, err = _validate_mcp(payload, existing)
    if err:
        return _fail(err)
    if fields["code"] != existing["code"] and any(
        i["code"] == fields["code"] for i in store.list_mcp_servers()
    ):
        return _fail("该编码已被使用", 409)
    if "headers" in payload:
        headers, err = _clean_headers(payload.get("headers"))
        if err:
            return _fail(err)
        fields["_secret"] = _store_headers(headers)
    item = store.update_mcp_server(server_id, fields) or existing
    return _ok(public_mcp(item))


def delete_mcp_server(server_id: int) -> dict:
    if not store.delete_mcp_server(server_id):
        return _fail("MCP 服务不存在", 404)
    return _ok({"deleted": True, "serverId": int(server_id)})


# ------------------------------------------------------------------ 第三方智能体 CRUD
def list_agent_apps() -> list[dict]:
    return [public_agent(i) for i in store.list_agent_apps()]


def _validate_agent(payload: dict, existing: dict | None) -> tuple[dict | None, str | None]:
    data = dict(existing or {})
    for key in ("code", "name", "kind", "baseUrl", "remoteAppId", "status", "remark"):
        if key in payload:
            data[key] = payload[key]

    code, err = _clean_code(data.get("code"))
    if err:
        return None, err
    name, err = _clean_text(data.get("name"), "名称")
    if err:
        return None, err
    kind = str(data.get("kind") or "dify").strip().lower()
    if kind not in AGENT_KINDS:
        return None, "类型仅支持 dify / coze / webhook"
    url, err = _clean_url(data.get("baseUrl"), "接口地址")
    if err:
        return None, err
    remote_id, err = _clean_text(
        data.get("remoteAppId"), "应用 ID", required=kind == "coze", limit=MAX_NAME
    )
    if err:
        return None, err
    status, err = _clean_status(data.get("status"), int(data.get("status", 1)))
    if err:
        return None, err
    remark, err = _clean_text(data.get("remark"), "备注", required=False, limit=MAX_REMARK)
    if err:
        return None, err
    return {
        "code": code, "name": name, "kind": kind, "baseUrl": url,
        "remoteAppId": remote_id or "", "status": status, "remark": remark or "",
    }, None


def create_agent_app(payload: dict) -> dict:
    fields, err = _validate_agent(payload, None)
    if err:
        return _fail(err)
    if any(i["code"] == fields["code"] for i in store.list_agent_apps()):
        return _fail("该编码已被使用", 409)
    raw_token = payload.get("token")
    if raw_token is not None and len(str(raw_token)) > MAX_SECRET:
        return _fail(f"Token 不能超过 {MAX_SECRET} 个字符")
    fields["_secret"] = encrypt_field(str(raw_token)) if raw_token else None
    item = store.create_agent_app(fields)
    return _ok(public_agent(item))


def update_agent_app(app_id: int, payload: dict) -> dict:
    existing = store.get_agent_app(app_id)
    if existing is None:
        return _fail("第三方智能体不存在", 404)
    fields, err = _validate_agent(payload, existing)
    if err:
        return _fail(err)
    if fields["code"] != existing["code"] and any(
        i["code"] == fields["code"] for i in store.list_agent_apps()
    ):
        return _fail("该编码已被使用", 409)
    if "token" in payload:
        raw_token = payload.get("token")
        if raw_token is not None and len(str(raw_token)) > MAX_SECRET:
            return _fail(f"Token 不能超过 {MAX_SECRET} 个字符")
        fields["_secret"] = encrypt_field(str(raw_token)) if raw_token else None
    item = store.update_agent_app(app_id, fields) or existing
    return _ok(public_agent(item))


def delete_agent_app(app_id: int) -> dict:
    if not store.delete_agent_app(app_id):
        return _fail("第三方智能体不存在", 404)
    return _ok({"deleted": True, "appId": int(app_id)})


# ------------------------------------------------------------------ 测试适配器
def _trust_env(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host not in LOCAL_HOSTS


def _latency(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _error_text(resp: httpx.Response) -> str:
    """尽量提取上游错误信息，但只取 message/error，不整段回显请求头。"""
    try:
        payload = resp.json()
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict) and error.get("message"):
                return f"HTTP {resp.status_code}：{error['message']}"
            if isinstance(error, str) and error:
                return f"HTTP {resp.status_code}：{error}"
            if payload.get("message"):
                return f"HTTP {resp.status_code}：{payload['message']}"
    except Exception:  # noqa: BLE001
        pass
    return f"HTTP {resp.status_code}"


def _auth_headers(token: str, extra: dict | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    headers.update(extra or {})
    return headers


def _mock_local(item: dict) -> dict:
    return {
        "state": "ok",
        "mock": True,
        "model": item["remoteModel"],
        "models": [item["remoteModel"]],
        "reply": "pong（模拟回复）",
        "latencyMs": 12,
    }


def test_local_model(local_id: int) -> dict:
    item = store.get_local_model(local_id)
    if item is None:
        return _fail("本地模型不存在", 404)
    start = time.monotonic()
    if settings.integration_test_provider != "live":
        result = _mock_local(item)
        store.record_local_test(local_id, "ok", result["latencyMs"])
        return _ok({**result, "item": public_local(store.get_local_model(local_id) or item)})

    key = _secret_of(item)
    base = str(item["baseUrl"]).rstrip("/")
    timeout = min(float(item.get("timeout") or 30), 60)
    models: list[str] = []
    try:
        with httpx.Client(timeout=timeout, trust_env=_trust_env(base),
                          headers=_auth_headers(key)) as client:
            try:
                listed = client.get(f"{base}/models")
                if listed.status_code == 200:
                    payload = listed.json()
                    rows = payload.get("data") if isinstance(payload, dict) else None
                    models = [
                        str(r.get("id")) for r in rows or []
                        if isinstance(r, dict) and r.get("id")
                    ][:50]
            except (httpx.HTTPError, ValueError):
                models = []

            resp = client.post(
                f"{base}/chat/completions",
                json={
                    "model": item["remoteModel"],
                    "messages": [{"role": "user", "content": "请回复：pong"}],
                    "max_tokens": 16,
                    "stream": False,
                },
            )
        if resp.status_code != 200:
            error = _error_text(resp)
            store.record_local_test(local_id, "error", _latency(start), error)
            return _ok({
                "state": "error", "mock": False, "error": error,
                "latencyMs": _latency(start), "models": models,
                "item": public_local(store.get_local_model(local_id) or item),
            })
        payload = resp.json()
        choices = payload.get("choices") if isinstance(payload, dict) else None
        reply = ""
        if choices:
            reply = str((choices[0].get("message") or {}).get("content") or "")[:500]
        result = {
            "state": "ok",
            "mock": False,
            "model": str(payload.get("model") or item["remoteModel"]),
            "models": models or [item["remoteModel"]],
            "reply": reply or "(模型返回了空内容)",
            "latencyMs": _latency(start),
        }
        store.record_local_test(local_id, "ok", result["latencyMs"])
        return _ok({**result, "item": public_local(store.get_local_model(local_id) or item)})
    except Exception as exc:  # noqa: BLE001 - 管理端探测失败是一种结果
        error = f"{type(exc).__name__}：{exc}"[:512]
        store.record_local_test(local_id, "error", _latency(start), error)
        return _ok({
            "state": "error", "mock": False, "error": error,
            "latencyMs": _latency(start), "models": [],
            "item": public_local(store.get_local_model(local_id) or item),
        })


def _mock_tools(item: dict) -> list[dict]:
    suffix = item["code"].replace("-", "_")
    return [
        {"name": f"{suffix}_search_kb", "description": "检索校园知识库（模拟工具）"},
        {"name": f"{suffix}_get_schedule", "description": "查询当前用户课表（模拟工具）"},
        {"name": f"{suffix}_get_balance", "description": "查询一卡通余额（模拟工具）"},
    ]


def _parse_rpc(resp: httpx.Response) -> dict:
    content_type = resp.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        for line in resp.text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                try:
                    return json.loads(line[5:].strip())
                except ValueError:
                    continue
        raise ValueError("SSE 响应中没有可解析的 data 事件")
    payload = resp.json()
    if not isinstance(payload, dict):
        raise ValueError("MCP 响应不是 JSON 对象")
    return payload


def test_mcp_server(server_id: int) -> dict:
    item = store.get_mcp_server(server_id)
    if item is None:
        return _fail("MCP 服务不存在", 404)
    start = time.monotonic()
    transport = str(item.get("transport") or "http")

    if settings.integration_mcp_provider != "live":
        tools = _mock_tools(item)
        store.record_mcp_test(server_id, "ok", 15, tools=tools)
        return _ok({
            "state": "ok", "mock": True, "toolCount": len(tools), "tools": tools,
            "server": {"name": f"{item['name']}（模拟）", "version": "mock"},
            "latencyMs": 15,
            "item": public_mcp(store.get_mcp_server(server_id) or item),
        })

    if transport != "http":
        message = ("stdio 需由部署进程托管，本版本只保存配置；"
                   "SSE 旧传输请改为 HTTP 后再测试。") if transport == "stdio" else (
                   "当前版本仅对 HTTP 传输执行 tools/list 探测；配置已保存。")
        store.record_mcp_test(server_id, "config-only", _latency(start), message)
        return _ok({
            "state": "config-only", "mock": False, "message": message,
            "toolCount": 0, "tools": [], "latencyMs": _latency(start),
            "item": public_mcp(store.get_mcp_server(server_id) or item),
        })

    headers = _auth_headers("", {"Accept": "application/json, text/event-stream"})
    try:
        custom = json.loads(_secret_of(item) or "{}")
        if isinstance(custom, dict):
            headers.update({str(k): str(v) for k, v in custom.items()})
    except Exception:  # noqa: BLE001 - 解不开的自定义头忽略，仍可测试
        pass

    timeout = min(float(settings.integration_test_timeout or 20), 60)
    url = item["endpoint"]
    session_id = ""
    try:
        with httpx.Client(timeout=timeout, trust_env=_trust_env(url), headers=headers) as client:
            init = client.post(url, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "nengxiaozhi-admin", "version": "1.0"},
                },
            })
            session_id = init.headers.get("mcp-session-id", "")
            if init.status_code != 200:
                raise RuntimeError(_error_text(init))
            init_payload = _parse_rpc(init)
            if init_payload.get("error"):
                raise RuntimeError(str(init_payload["error"]))
            server_info = (init_payload.get("result") or {}).get("serverInfo") or {}
            if session_id:
                client.headers["Mcp-Session-Id"] = session_id
            client.post(url, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
            listed = client.post(url, json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}
            })
            if listed.status_code != 200:
                raise RuntimeError(_error_text(listed))
            listed_payload = _parse_rpc(listed)
            if listed_payload.get("error"):
                raise RuntimeError(str(listed_payload["error"]))
            raw_tools = (listed_payload.get("result") or {}).get("tools") or []
        tools = [
            {
                "name": str(t.get("name") or "")[:128],
                "description": str(t.get("description") or "")[:255],
            }
            for t in raw_tools if isinstance(t, dict) and t.get("name")
        ][:50]
        latency = _latency(start)
        store.record_mcp_test(server_id, "ok", latency, tools=tools)
        return _ok({
            "state": "ok", "mock": False, "toolCount": len(tools), "tools": tools,
            "server": {
                "name": str(server_info.get("name") or ""),
                "version": str(server_info.get("version") or ""),
            },
            "latencyMs": latency,
            "item": public_mcp(store.get_mcp_server(server_id) or item),
        })
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}：{exc}"[:512]
        latency = _latency(start)
        store.record_mcp_test(server_id, "error", latency, error)
        return _ok({
            "state": "error", "mock": False, "error": error, "toolCount": 0,
            "tools": [], "latencyMs": latency,
            "item": public_mcp(store.get_mcp_server(server_id) or item),
        })


def _mock_agent(item: dict) -> dict:
    label = AGENT_LABELS.get(item["kind"], "第三方智能体")
    return {
        "state": "ok",
        "mock": True,
        "reply": f"pong（{label} 模拟回复）",
        "conversationId": f"mock-{item['code']}",
        "usage": {},
        "latencyMs": 18,
    }


def _agent_reply(kind: str, payload: dict) -> tuple[str, str, dict]:
    if kind == "dify":
        return (
            str(payload.get("answer") or ""),
            str(payload.get("conversation_id") or ""),
            (payload.get("metadata") or {}).get("usage") or {},
        )
    if kind == "coze":
        reply = ""
        messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
        for message in messages:
            if isinstance(message, dict) and message.get("type") == "answer":
                reply = str(message.get("content") or "")
                break
        return reply, str(payload.get("conversation_id") or ""), payload.get("usage") or {}
    return (
        str(payload.get("answer") or payload.get("reply") or payload.get("content") or ""),
        str(payload.get("conversationId") or payload.get("conversation_id") or ""),
        payload.get("usage") or {},
    )


def test_agent_app(app_id: int) -> dict:
    item = store.get_agent_app(app_id)
    if item is None:
        return _fail("第三方智能体不存在", 404)
    start = time.monotonic()
    kind = str(item.get("kind") or "dify")

    if settings.integration_agent_provider != "live":
        result = _mock_agent(item)
        store.record_agent_test(app_id, "ok", result["latencyMs"], reply=result["reply"])
        return _ok({**result, "item": public_agent(store.get_agent_app(app_id) or item)})

    token = _secret_of(item)
    base = str(item["baseUrl"]).rstrip("/")
    timeout = min(float(settings.integration_test_timeout or 20), 60)
    try:
        with httpx.Client(timeout=timeout, trust_env=_trust_env(base),
                          headers=_auth_headers(token)) as client:
            if kind == "dify":
                resp = client.post(f"{base}/chat-messages", json={
                    "inputs": {},
                    "query": "请回复：pong",
                    "response_mode": "blocking",
                    "conversation_id": "",
                    "user": "nxz-integration-test",
                })
            elif kind == "coze":
                resp = client.post(f"{base}/open_api/v2/chat", json={
                    "bot_id": item.get("remoteAppId") or "",
                    "user": "nxz-integration-test",
                    "query": "请回复：pong",
                    "stream": False,
                })
            else:
                resp = client.post(base, json={
                    "query": "请回复：pong",
                    "user": "nxz-integration-test",
                    "source": "nengxiaozhi",
                })
        if resp.status_code != 200:
            error = _error_text(resp)
            store.record_agent_test(app_id, "error", _latency(start), error)
            return _ok({
                "state": "error", "mock": False, "error": error,
                "latencyMs": _latency(start),
                "item": public_agent(store.get_agent_app(app_id) or item),
            })
        payload = resp.json()
        reply, conversation_id, usage = _agent_reply(kind, payload)
        if not reply:
            raise ValueError("响应成功但未找到 answer / reply 字段")
        latency = _latency(start)
        store.record_agent_test(app_id, "ok", latency, reply=reply)
        return _ok({
            "state": "ok", "mock": False, "reply": reply[:500],
            "conversationId": conversation_id, "usage": usage,
            "latencyMs": latency,
            "item": public_agent(store.get_agent_app(app_id) or item),
        })
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}：{exc}"[:512]
        latency = _latency(start)
        store.record_agent_test(app_id, "error", latency, error)
        return _ok({
            "state": "error", "mock": False, "error": error,
            "latencyMs": latency,
            "item": public_agent(store.get_agent_app(app_id) or item),
        })


# ------------------------------------------------------------------ 路由与概览
def local_route(model_code: str | None = None) -> dict | None:
    """``LLM_PROVIDER=local`` 时的路由来源。

    - 指定了本地模型编码 → 用该条；
    - ``default`` / 空 → 用当前启用（active）的本地模型；
    - 编码不存在时回退到 active，保证切换后旧绑定不会立刻失效。
    """
    code = str(model_code or "").strip()
    item = None
    if code and code != "default":
        item = store.find_local_model_by_code(code)
    if item is None:
        item = store.active_local_model()
    if item is None:
        return None
    key = ""
    try:
        key = decrypt_field(item.get("_secret")) or ""
    except CryptoError:
        key = ""
    return {
        "modelCode": item["code"],
        "label": item["name"],
        "baseUrl": item["baseUrl"],
        "remoteModel": item["remoteModel"],
        "apiKey": key,
        "timeout": float(item["timeout"] or 30),
        "status": int(item["status"] or 0),
        "isLocal": 1,
    }


def local_choices() -> list[dict]:
    items = []
    for item in store.list_local_models():
        items.append({
            "modelCode": item["code"],
            "modelName": item["name"],
            "vendor": "",
            "vendorLabel": "本地模型",
            "remoteModel": item["remoteModel"],
            "isLocal": 1,
            "status": int(item["status"] or 0),
            "available": bool(item["status"]),
            "isDefault": bool(item.get("active")),
            "hasKey": bool(item.get("_secret")),
            "active": bool(item.get("active")),
        })
    return items


def overview() -> dict:
    local = store.list_local_models()
    mcp = store.list_mcp_servers()
    agents = store.list_agent_apps()
    active = next((i for i in local if i.get("active")), None)
    provider = str(settings.llm_provider or "gateway").lower()
    if provider == "local":
        if not local:
            hint = "已启用本地模型模式，但尚未配置本地模型；问答将不可用。"
        elif active is None:
            hint = "已启用本地模型模式，但没有「当前启用」的本地模型；请在列表中设置一个。"
        elif not active.get("status"):
            hint = f"当前启用的本地模型「{active['name']}」已停用；问答将不可用。"
        else:
            hint = f"问答将直连本地模型「{active['name']}」（{active['remoteModel']}）。"
    else:
        hint = ("当前为网关模式：可用模型仍由 campus-ai-gateway 下发；"
                "本地模型配置用于离线/直连部署（LLM_PROVIDER=local）。")
    return {
        "llmProvider": provider,
        "llmMock": bool(settings.llm_mock),
        "activeLocalModel": public_local(active) if active else None,
        "localModelCount": len(local),
        "mcpServerCount": len(mcp),
        "agentAppCount": len(agents),
        "mcpTestProvider": settings.integration_mcp_provider,
        "agentTestProvider": settings.integration_agent_provider,
        "agentKinds": [
            {"value": value, "label": AGENT_LABELS[value]} for value in AGENT_KINDS
        ],
        "mcpTransports": [
            {"value": "http", "label": "HTTP（可测试）"},
            {"value": "sse", "label": "SSE（仅保存配置）"},
            {"value": "stdio", "label": "stdio（仅保存配置）"},
        ],
        "hint": hint,
    }


# ------------------------------------------------------------------ 示例数据
def seed_examples() -> dict:
    """写入可直接演示的示例配置（按编码幂等，不覆盖已有记录）。"""
    local_defs = [
        {
            "code": "local-qwen", "name": "本地 Qwen2.5-7B（示例）",
            "baseUrl": "http://127.0.0.1:8001/v1", "remoteModel": "qwen2.5-7b-instruct",
            "timeout": 30, "status": 1, "remark": SAMPLE_REMARK,
        },
        {
            "code": "local-glm", "name": "本地 GLM-4-9B（示例）",
            "baseUrl": "http://127.0.0.1:8002/v1", "remoteModel": "glm-4-9b-chat",
            "timeout": 30, "status": 1, "remark": SAMPLE_REMARK,
        },
    ]
    mcp_defs = [
        {
            "code": "campus-kb-tools", "name": "校园知识检索 MCP（示例）",
            "transport": "http", "endpoint": "http://127.0.0.1:9001/mcp",
            "status": 1, "remark": SAMPLE_REMARK,
        },
        {
            "code": "schedule-tools", "name": "课表查询 MCP（示例）",
            "transport": "stdio", "command": "python",
            "args": ["-m", "mcp_server.schedule"],
            "status": 1, "remark": SAMPLE_REMARK,
        },
    ]
    agent_defs = [
        {
            "code": "dify-campus", "name": "Dify 校园助手（示例）",
            "kind": "dify", "baseUrl": "https://dify.example.edu/v1",
            "remoteAppId": "", "status": 1, "remark": SAMPLE_REMARK,
        },
        {
            "code": "coze-guide", "name": "Coze 办事向导（示例）",
            "kind": "coze", "baseUrl": "https://api.coze.cn",
            "remoteAppId": "7000000000000000001", "status": 1, "remark": SAMPLE_REMARK,
        },
    ]
    created = {"localModels": 0, "mcpServers": 0, "agentApps": 0}
    existing_local = {i["code"] for i in store.list_local_models()}
    for item in local_defs:
        if item["code"] in existing_local:
            continue
        store.create_local_model({**item, "_secret": None, "active": 0})
        created["localModels"] += 1
    if store.active_local_model() is None:
        first = next((i for i in store.list_local_models() if i["code"] == "local-qwen"), None)
        if first:
            store.activate_local_model(first["localId"])

    existing_mcp = {i["code"] for i in store.list_mcp_servers()}
    for item in mcp_defs:
        if item["code"] in existing_mcp:
            continue
        store.create_mcp_server({**item, "_secret": None})
        created["mcpServers"] += 1

    existing_agents = {i["code"] for i in store.list_agent_apps()}
    for item in agent_defs:
        if item["code"] in existing_agents:
            continue
        store.create_agent_app({**item, "_secret": None})
        created["agentApps"] += 1

    return {
        "created": created,
        "total": sum(created.values()),
        "items": {
            "localModels": list_local_models(),
            "mcpServers": list_mcp_servers(),
            "agentApps": list_agent_apps(),
        },
    }
