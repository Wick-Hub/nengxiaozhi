"""集成中心测试（任务 4.8 / C-05、M-A-07/10）。

覆盖五层：

1. **配置管理**：本地模型 / MCP / 第三方智能体的校验、幂等、唯一性与三态密钥；
2. **密钥安全**：落库为 AES-GCM 密文，公开结构只给 ``hasKey`` / ``hasToken`` /
   ``hasHeaders`` 与 Header 名称，任何层级都不出现明文；
3. **mock 全链路**：外部资源未就绪时的确定性测试适配器；
4. **live 适配器**：用真实本地 HTTP 服务验证 OpenAI 兼容、MCP JSON-RPC（含 SSE
   与会话头）、Dify / Coze / Webhook 的实际报文，而不是 monkeypatch 假象；
5. **路由与页面**：``LLM_PROVIDER=local`` 时切换当前模型立即生效、网关模式下
   仍是网关模型清单，以及管理页与导航入口。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.core import llm_client, llm_router
from app.db import integration_store as store
from app.main import app
from app.services import gateway_admin, integration_admin as service

client = TestClient(app)


# ------------------------------------------------------------------ 测试数据辅助
def _local_payload(code: str = "local-a", **overrides) -> dict:
    return {
        "code": code,
        "name": f"本地模型 {code}",
        "baseUrl": "http://127.0.0.1:8101/v1",
        "remoteModel": "qwen2.5-7b-instruct",
        "timeout": 20,
        "status": 1,
        **overrides,
    }


def _mcp_payload(code: str = "kb-tools", **overrides) -> dict:
    return {
        "code": code,
        "name": f"MCP {code}",
        "transport": "http",
        "endpoint": "http://127.0.0.1:9001/mcp",
        "status": 1,
        **overrides,
    }


def _agent_payload(code: str = "dify-app", **overrides) -> dict:
    return {
        "code": code,
        "name": f"智能体 {code}",
        "kind": "dify",
        "baseUrl": "https://dify.example.edu/v1",
        "status": 1,
        **overrides,
    }


# ================================================================== 1. 示例数据
def test_seed_examples_is_idempotent_and_marked() -> None:
    first = service.seed_examples()
    assert first["created"] == {"localModels": 2, "mcpServers": 2, "agentApps": 2}
    assert first["total"] == 6

    second = service.seed_examples()
    assert second["total"] == 0

    locals_ = service.list_local_models()
    assert {i["code"] for i in locals_} == {"local-qwen", "local-glm"}
    assert all(i["remark"] == service.SAMPLE_REMARK for i in locals_)
    assert [i["code"] for i in locals_ if i["active"]] == ["local-qwen"]
    assert {i["code"] for i in service.list_mcp_servers()} == {
        "campus-kb-tools", "schedule-tools",
    }
    assert {i["code"] for i in service.list_agent_apps()} == {"dify-campus", "coze-guide"}
    assert all(
        i["remark"] == service.SAMPLE_REMARK for i in service.list_agent_apps()
    )


# ================================================================== 2. 本地模型
def test_local_model_crud_and_secret_three_state() -> None:
    created = service.create_local_model(_local_payload(apiKey="sk-local-secret"))
    assert created["ok"]
    item = created["data"]
    assert item["hasKey"] is True
    assert "apiKey" not in item
    assert "sk-local-secret" not in json.dumps(item, ensure_ascii=False)
    raw = store.get_local_model(item["localId"])["_secret"]
    assert raw and raw.startswith(b"v1")
    assert b"sk-local-secret" not in raw

    # 不传 apiKey → 保留原值
    renamed = service.update_local_model(item["localId"], {"name": "改名后的本地模型"})
    assert renamed["ok"] and renamed["data"]["hasKey"] is True
    assert renamed["data"]["name"] == "改名后的本地模型"

    # 传空串 → 清空
    cleared = service.update_local_model(item["localId"], {"apiKey": ""})
    assert cleared["ok"] and cleared["data"]["hasKey"] is False

    assert service.delete_local_model(item["localId"])["ok"]
    assert service.delete_local_model(item["localId"])["status"] == 404


def test_local_model_validation_and_duplicates() -> None:
    assert service.create_local_model(_local_payload())["ok"]
    dup = service.create_local_model(_local_payload())
    assert dup["ok"] is False and dup["status"] == 409

    bad_code = service.create_local_model(_local_payload("A Bad Code"))
    assert bad_code["ok"] is False and "小写字母开头" in bad_code["error"]

    bad_url = service.create_local_model(
        _local_payload("local-b", baseUrl="127.0.0.1:8100")
    )
    assert bad_url["ok"] is False and "http(s)" in bad_url["error"]

    bad_timeout = service.create_local_model(_local_payload("local-c", timeout=0))
    assert bad_timeout["ok"] is False and "1–600" in bad_timeout["error"]

    bad_status = service.create_local_model(_local_payload("local-d", status=2))
    assert bad_status["ok"] is False and "0 或 1" in bad_status["error"]

    assert service.update_local_model(9999, {"name": "x"})["status"] == 404


def test_activate_local_model_keeps_single_active() -> None:
    a = service.create_local_model(_local_payload("local-a"))["data"]
    b = service.create_local_model(_local_payload("local-b"))["data"]
    assert a["active"] == 0 and b["active"] == 0

    assert service.activate_local_model(a["localId"])["ok"]
    assert service.activate_local_model(b["localId"])["ok"]

    items = {i["code"]: i for i in service.list_local_models()}
    assert items["local-a"]["active"] == 0
    assert items["local-b"]["active"] == 1
    assert service.activate_local_model(9999)["status"] == 404


# ================================================================== 3. MCP
def test_mcp_headers_encrypted_and_three_state() -> None:
    created = service.create_mcp_server(
        _mcp_payload(
            headers={"X-Token": "secret-token", "Authorization": "Bearer abc"},
        )
    )
    assert created["ok"]
    item = created["data"]
    assert item["hasHeaders"] is True
    assert item["headerNames"] == ["Authorization", "X-Token"]
    assert "secret-token" not in json.dumps(item, ensure_ascii=False)
    raw = store.get_mcp_server(item["serverId"])["_secret"]
    assert raw and b"secret-token" not in raw

    # 未提供 headers → 保持原值
    kept = service.update_mcp_server(item["serverId"], {"name": "改名"})
    assert kept["data"]["hasHeaders"] is True

    # 传空对象 → 清空
    cleared = service.update_mcp_server(item["serverId"], {"headers": {}})
    assert cleared["data"]["hasHeaders"] is False
    assert cleared["data"]["headerNames"] == []


def test_mcp_validation() -> None:
    stdio_missing = service.create_mcp_server(
        _mcp_payload("mcp-a", transport="stdio", endpoint="", command="")
    )
    assert stdio_missing["ok"] is False and "启动命令" in stdio_missing["error"]

    stdio_unsafe = service.create_mcp_server(
        _mcp_payload("mcp-b", transport="stdio", endpoint="", command="python -m x")
    )
    assert stdio_unsafe["ok"] is False and "shell" in stdio_unsafe["error"]

    bad_transport = service.create_mcp_server(_mcp_payload("mcp-c", transport="ws"))
    assert bad_transport["ok"] is False and "http / sse / stdio" in bad_transport["error"]

    bad_header = service.create_mcp_server(
        _mcp_payload("mcp-d", headers={"Bad Header": "x"})
    )
    assert bad_header["ok"] is False and "Header 名称不合法" in bad_header["error"]

    crlf = service.create_mcp_server(
        _mcp_payload("mcp-e", headers={"X-Token": "a\r\nInjected: 1"})
    )
    assert crlf["ok"] is False and "不能包含换行" in crlf["error"]

    assert service.create_mcp_server(_mcp_payload())["ok"]
    assert service.create_mcp_server(_mcp_payload())["status"] == 409
    assert service.delete_mcp_server(9999)["status"] == 404


def test_mcp_stdio_saves_command_and_args() -> None:
    created = service.create_mcp_server(
        _mcp_payload(
            "schedule-tools",
            transport="stdio",
            endpoint="",
            command="python",
            args=["-m", "mcp_server.schedule"],
        )
    )
    assert created["ok"]
    item = created["data"]
    assert item["transport"] == "stdio"
    assert item["endpoint"] == ""
    assert item["command"] == "python"
    assert item["args"] == ["-m", "mcp_server.schedule"]


# ================================================================== 4. 第三方智能体
def test_agent_secret_encrypted_and_kind_validation() -> None:
    created = service.create_agent_app(
        _agent_payload(token="dify-secret-token", remoteAppId="app-1")
    )
    assert created["ok"]
    item = created["data"]
    assert item["hasToken"] is True
    assert item["kindLabel"] == "Dify 应用"
    assert "dify-secret-token" not in json.dumps(item, ensure_ascii=False)
    raw = store.get_agent_app(item["appId"])["_secret"]
    assert raw and b"dify-secret-token" not in raw

    kept = service.update_agent_app(item["appId"], {"name": "改名"})
    assert kept["data"]["hasToken"] is True
    cleared = service.update_agent_app(item["appId"], {"token": ""})
    assert cleared["data"]["hasToken"] is False

    coze_missing = service.create_agent_app(
        _agent_payload("coze-x", kind="coze", baseUrl="https://api.coze.cn")
    )
    assert coze_missing["ok"] is False and "应用 ID" in coze_missing["error"]

    bad_kind = service.create_agent_app(_agent_payload("bad-kind", kind="openai"))
    assert bad_kind["ok"] is False and "dify / coze / webhook" in bad_kind["error"]

    assert service.create_agent_app(_agent_payload("agent-ok"))["ok"]
    assert service.create_agent_app(_agent_payload("agent-ok"))["status"] == 409
    assert service.delete_agent_app(9999)["status"] == 404


def test_agent_supports_three_kinds() -> None:
    dify = service.create_agent_app(_agent_payload("dify-a"))
    coze = service.create_agent_app(
        _agent_payload("coze-a", kind="coze", baseUrl="https://api.coze.cn",
                       remoteAppId="7000000000000000001")
    )
    hook = service.create_agent_app(
        _agent_payload("hook-a", kind="webhook", baseUrl="https://agent.example.edu/hook")
    )
    assert [dify["ok"], coze["ok"], hook["ok"]] == [True, True, True]
    assert {i["kindLabel"] for i in service.list_agent_apps()} == {
        "Dify 应用", "Coze 智能体", "自定义 Webhook",
    }


# ================================================================== 5. mock 适配器
def test_mock_adapters_record_state() -> None:
    local = service.create_local_model(_local_payload())["data"]
    result = service.test_local_model(local["localId"])
    assert result["ok"]
    data = result["data"]
    assert data["state"] == "ok" and data["mock"] is True
    assert data["reply"] == "pong（模拟回复）"
    assert data["item"]["lastTestState"] == "ok"
    assert data["item"]["lastLatencyMs"] > 0

    mcp = service.create_mcp_server(_mcp_payload("campus-kb-tools"))["data"]
    result = service.test_mcp_server(mcp["serverId"])
    assert result["data"]["state"] == "ok"
    assert result["data"]["toolCount"] == 3
    assert result["data"]["tools"][0]["name"] == "campus_kb_tools_search_kb"
    assert result["data"]["item"]["toolCount"] == 3
    assert result["data"]["item"]["lastTestState"] == "ok"

    agent = service.create_agent_app(_agent_payload())["data"]
    result = service.test_agent_app(agent["appId"])
    assert result["data"]["state"] == "ok"
    assert "Dify 应用 模拟回复" in result["data"]["reply"]
    assert result["data"]["item"]["lastTestState"] == "ok"

    assert service.test_local_model(9999)["status"] == 404
    assert service.test_mcp_server(9999)["status"] == 404
    assert service.test_agent_app(9999)["status"] == 404


# ================================================================== 6. live 适配器
class _StubHandler(BaseHTTPRequestHandler):
    """一个够用的仿真上游：OpenAI / MCP / Dify / Coze / Webhook。"""

    protocol_version = "HTTP/1.1"
    requests: list[dict] = []

    def log_message(self, *_a):
        return

    # ---------------------------------------------------------- 工具
    def _record(self, body: bytes) -> dict:
        try:
            payload = json.loads(body) if body else {}
        except ValueError:
            payload = {}
        record = {
            "path": self.path,
            "method": self.command,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": payload,
        }
        type(self).requests.append(record)
        return payload

    def _send(self, status: int, payload, *, content_type: str = "application/json",
              extra: dict | None = None) -> None:
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    # ---------------------------------------------------------- GET
    def do_GET(self):  # noqa: N802
        self._record(b"")
        if self.path == "/v1/models":
            self._send(200, {"object": "list", "data": [{"id": "qwen-test"}]})
            return
        if self.path.startswith("/bad/"):
            self._send(401, {"error": {"message": "invalid key"}})
            return
        self._send(404, {"error": "not found"})

    # ---------------------------------------------------------- POST
    def do_POST(self):  # noqa: N802
        payload = self._record(self._read_body())
        if self.path == "/v1/chat/completions":
            self._send(200, {
                "model": payload.get("model") or "qwen-test",
                "choices": [{"message": {"role": "assistant", "content": "pong"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
            })
            return
        if self.path.startswith("/bad/"):
            self._send(401, {"error": {"message": "invalid key"}})
            return
        if self.path == "/mcp":
            method = str(payload.get("method") or "")
            if method == "initialize":
                event = {
                    "jsonrpc": "2.0", "id": payload.get("id"),
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "stub-mcp", "version": "9.9"},
                    },
                }
                self._send(
                    200,
                    f"event: message\ndata: {json.dumps(event)}\n\n".encode(),
                    content_type="text/event-stream",
                    extra={"Mcp-Session-Id": "sess-1"},
                )
                return
            if method == "tools/list":
                self._send(200, {
                    "jsonrpc": "2.0", "id": payload.get("id"),
                    "result": {"tools": [
                        {"name": "search_kb", "description": "检索知识库"},
                        {"name": "get_schedule", "description": "查询课表"},
                    ]},
                })
                return
            self._send(202, b"", content_type="application/json")
            return
        if self.path == "/v1/chat-messages":
            self._send(200, {
                "answer": "pong-dify",
                "conversation_id": "conv-dify",
                "metadata": {"usage": {"total_tokens": 7}},
            })
            return
        if self.path == "/open_api/v2/chat":
            self._send(200, {
                "messages": [
                    {"type": "verbose", "content": "thinking"},
                    {"type": "answer", "content": "pong-coze"},
                ],
                "conversation_id": "conv-coze",
                "usage": {"total_tokens": 9},
            })
            return
        if self.path == "/hook":
            self._send(200, {"reply": "pong-hook", "conversationId": "conv-hook"})
            return
        self._send(404, {"error": "not found"})


@pytest.fixture()
def live_stub():
    _StubHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_address[1]}"
    yield origin
    server.shutdown()
    server.server_close()


def test_live_local_model_probe(live_stub, monkeypatch) -> None:
    monkeypatch.setattr(settings, "integration_test_provider", "live")
    created = service.create_local_model(
        _local_payload("local-live", baseUrl=f"{live_stub}/v1", apiKey="sk-live")
    )
    result = service.test_local_model(created["data"]["localId"])
    data = result["data"]
    assert data["state"] == "ok", data
    assert data["mock"] is False
    assert data["reply"] == "pong"
    assert data["models"] == ["qwen-test"]
    assert data["item"]["lastTestState"] == "ok"

    chat = [r for r in _StubHandler.requests if r["path"] == "/v1/chat/completions"][0]
    assert chat["headers"].get("authorization") == "Bearer sk-live"
    assert chat["body"]["model"] == "qwen2.5-7b-instruct"


def test_live_local_model_failure_is_recorded(live_stub, monkeypatch) -> None:
    monkeypatch.setattr(settings, "integration_test_provider", "live")
    created = service.create_local_model(
        _local_payload("local-bad", baseUrl=f"{live_stub}/bad/v1")
    )
    data = service.test_local_model(created["data"]["localId"])["data"]
    assert data["state"] == "error"
    assert "HTTP 401" in data["error"]
    assert data["item"]["lastTestState"] == "error"
    assert "invalid key" in data["item"]["lastTestError"]


def test_live_mcp_probe_with_session_and_custom_header(live_stub, monkeypatch) -> None:
    monkeypatch.setattr(settings, "integration_mcp_provider", "live")
    created = service.create_mcp_server(
        _mcp_payload(
            "campus-kb-tools",
            endpoint=f"{live_stub}/mcp",
            headers={"X-Token": "tok-live"},
        )
    )
    data = service.test_mcp_server(created["data"]["serverId"])["data"]
    assert data["state"] == "ok", data
    assert data["mock"] is False
    assert data["toolCount"] == 2
    assert [t["name"] for t in data["tools"]] == ["search_kb", "get_schedule"]
    assert data["server"] == {"name": "stub-mcp", "version": "9.9"}
    assert data["item"]["toolCount"] == 2

    calls = [r for r in _StubHandler.requests if r["path"] == "/mcp"]
    assert [r["body"].get("method") for r in calls] == [
        "initialize", "notifications/initialized", "tools/list",
    ]
    assert calls[0]["headers"].get("x-token") == "tok-live"
    assert calls[-1]["headers"].get("mcp-session-id") == "sess-1"


def test_live_mcp_sse_and_stdio_are_config_only(monkeypatch) -> None:
    monkeypatch.setattr(settings, "integration_mcp_provider", "live")

    sse = service.create_mcp_server(
        _mcp_payload("sse-tools", transport="sse", endpoint="http://127.0.0.1:9/sse")
    )["data"]
    sse_result = service.test_mcp_server(sse["serverId"])["data"]
    assert sse_result["state"] == "config-only"
    assert "仅对 HTTP" in sse_result["message"]

    stdio = service.create_mcp_server(
        _mcp_payload("stdio-tools", transport="stdio", endpoint="",
                     command="python", args=["-m", "mcp_server.x"])
    )["data"]
    stdio_result = service.test_mcp_server(stdio["serverId"])["data"]
    assert stdio_result["state"] == "config-only"
    assert "stdio" in stdio_result["message"]


@pytest.mark.parametrize(
    ("kind", "base_path", "remote_app_id", "expected", "conversation"),
    [
        ("dify", "/v1", "", "pong-dify", "conv-dify"),
        ("coze", "", "7000000000000000001", "pong-coze", "conv-coze"),
        ("webhook", "/hook", "", "pong-hook", "conv-hook"),
    ],
)
def test_live_agent_adapters(
    live_stub, monkeypatch, kind, base_path, remote_app_id, expected, conversation
) -> None:
    monkeypatch.setattr(settings, "integration_agent_provider", "live")
    created = service.create_agent_app(
        _agent_payload(
            f"agent-{kind}",
            kind=kind,
            baseUrl=f"{live_stub}{base_path}",
            remoteAppId=remote_app_id,
            token="agent-token",
        )
    )
    data = service.test_agent_app(created["data"]["appId"])["data"]
    assert data["state"] == "ok", data
    assert data["mock"] is False
    assert data["reply"] == expected
    assert data["conversationId"] == conversation
    assert data["item"]["lastTestState"] == "ok"

    path = {"dify": "/v1/chat-messages", "coze": "/open_api/v2/chat", "webhook": "/hook"}[kind]
    call = [r for r in _StubHandler.requests if r["path"] == path][0]
    assert call["headers"].get("authorization") == "Bearer agent-token"
    if kind == "coze":
        assert call["body"]["bot_id"] == remote_app_id


def test_local_llm_client_works_without_api_key(live_stub) -> None:
    client_ = llm_client.LLMClient(
        mock=False,
        api_key="",
        base_url=f"{live_stub}/v1",
        model="qwen-test",
        local=True,
    )
    assert client_.available() is True
    reply = client_.chat([{"role": "user", "content": "hi"}])
    assert reply.content == "pong"
    call = [r for r in _StubHandler.requests if r["path"] == "/v1/chat/completions"][-1]
    assert "authorization" not in call["headers"]


# ================================================================== 7. 路由
def test_local_provider_routes_to_active_model(monkeypatch) -> None:
    a = service.create_local_model(
        _local_payload("local-a", baseUrl="http://127.0.0.1:8101/v1", apiKey="sk-a")
    )["data"]
    b = service.create_local_model(
        _local_payload("local-b", baseUrl="http://127.0.0.1:8102/v1",
                       remoteModel="glm-4-9b-chat", apiKey="sk-b")
    )["data"]
    service.activate_local_model(a["localId"])

    monkeypatch.setattr(settings, "llm_provider", "local")
    monkeypatch.setattr(settings, "llm_model", "should-not-be-used")

    route = llm_router.resolve("default")
    assert route.source == llm_router.SOURCE_LOCAL
    assert route.source_label == "本地模型"
    assert route.model_code == "local-a"
    assert route.base_url == "http://127.0.0.1:8101/v1"
    assert route.remote_model == "qwen2.5-7b-instruct"
    assert route.api_key == "sk-a"  # 路由内部需要 Key
    assert "apiKey" not in route.to_dict()  # 管理接口不外泄
    assert route.is_local == 1

    first = llm_router.get_client_for("default")
    assert first._model == "qwen2.5-7b-instruct"
    assert first._local is True
    assert first._base_url == "http://127.0.0.1:8101/v1"

    # 切换「当前启用」立刻生效，不需要重启
    service.activate_local_model(b["localId"])
    second = llm_router.get_client_for("default")
    assert second._model == "glm-4-9b-chat"
    assert second._base_url == "http://127.0.0.1:8102/v1"

    explicit = llm_router.get_client_for("local-a")
    assert explicit._model == "qwen2.5-7b-instruct"
    assert llm_router.resolve("local-a").source == llm_router.SOURCE_LOCAL

    choices = gateway_admin.choices()
    assert choices["provider"] == "local"
    assert {i["modelCode"] for i in choices["items"]} == {"local-a", "local-b"}
    assert choices["items"][0]["isDefault"] is True  # local-b 当前启用，排在首位


def test_local_provider_without_model_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_provider", "local")
    monkeypatch.setattr(settings, "llm_mock", False)

    route = llm_router.resolve("default")
    assert route.source == llm_router.SOURCE_LOCAL
    assert route.error and "集成中心" in route.error
    assert route.available is False

    built = llm_client.get_client()
    assert built.available() is False

    data = service.overview()
    assert data["llmProvider"] == "local"
    assert "尚未配置本地模型" in data["hint"]


def test_local_provider_disabled_model_is_reported(monkeypatch) -> None:
    item = service.create_local_model(_local_payload())["data"]
    service.activate_local_model(item["localId"])
    service.update_local_model(item["localId"], {"status": 0})
    monkeypatch.setattr(settings, "llm_provider", "local")

    route = llm_router.resolve("default")
    assert route.is_local == 0
    assert route.error and "已停用" in route.error
    assert route.available is False
    assert "已停用" in service.overview()["hint"]


def test_gateway_mode_ignores_local_models(monkeypatch) -> None:
    item = service.create_local_model(_local_payload())["data"]
    service.activate_local_model(item["localId"])
    monkeypatch.setattr(settings, "llm_provider", "gateway")
    monkeypatch.setattr(settings, "llm_model", "deepseek-flash")

    route = llm_router.resolve("default")
    assert route.source in (llm_router.SOURCE_ENV, llm_router.SOURCE_GATEWAY)
    assert route.source != llm_router.SOURCE_LOCAL
    assert "本地模型配置用于离线/直连部署" in service.overview()["hint"]


# ================================================================== 8. API 与页面
def test_api_contract_and_error_mapping() -> None:
    overview = client.get("/api/admin/integrations/overview")
    assert overview.status_code == 200
    body = overview.json()
    assert body["ok"] is True
    assert body["data"]["llmProvider"] in {"gateway", "local"}
    assert len(body["data"]["agentKinds"]) == 3
    assert len(body["data"]["mcpTransports"]) == 3

    created = client.post(
        "/api/admin/integrations/local-models",
        json=_local_payload(apiKey="sk-api"),
    )
    assert created.status_code == 200, created.text
    item = created.json()["data"]
    assert item["hasKey"] is True
    assert "sk-api" not in created.text

    listed = client.get("/api/admin/integrations/local-models").json()
    assert [i["code"] for i in listed["data"]["items"]] == ["local-a"]

    assert client.post(
        f"/api/admin/integrations/local-models/{item['localId']}/activate"
    ).status_code == 200
    tested = client.post(
        f"/api/admin/integrations/local-models/{item['localId']}/test"
    )
    assert tested.status_code == 200
    assert tested.json()["data"]["state"] == "ok"

    duplicate = client.post(
        "/api/admin/integrations/local-models", json=_local_payload()
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "该编码已被使用"

    invalid = client.post(
        "/api/admin/integrations/local-models",
        json=_local_payload("bad-url", baseUrl="ftp://x"),
    )
    assert invalid.status_code == 400

    patched = client.patch(
        f"/api/admin/integrations/local-models/{item['localId']}",
        json={"timeout": 45},
    )
    assert patched.status_code == 200
    assert patched.json()["data"]["timeout"] == 45

    assert client.delete(
        f"/api/admin/integrations/local-models/{item['localId']}"
    ).status_code == 200
    assert client.delete(
        f"/api/admin/integrations/local-models/{item['localId']}"
    ).status_code == 404

    seeded = client.post("/api/admin/integrations/seed-examples")
    assert seeded.status_code == 200
    assert seeded.json()["data"]["total"] == 6


def test_api_mcp_and_agent_crud() -> None:
    mcp = client.post("/api/admin/integrations/mcp-servers",
                      json=_mcp_payload(headers={"X-Token": "t"}))
    assert mcp.status_code == 200, mcp.text
    assert mcp.json()["data"]["headerNames"] == ["X-Token"]
    server_id = mcp.json()["data"]["serverId"]
    assert client.get("/api/admin/integrations/mcp-servers").json()["data"]["items"]
    assert client.post(
        f"/api/admin/integrations/mcp-servers/{server_id}/test"
    ).json()["data"]["toolCount"] == 3
    assert client.delete(f"/api/admin/integrations/mcp-servers/{server_id}").status_code == 200

    agent = client.post("/api/admin/integrations/agent-apps",
                        json=_agent_payload(token="tok"))
    assert agent.status_code == 200, agent.text
    app_id = agent.json()["data"]["appId"]
    assert client.post(
        f"/api/admin/integrations/agent-apps/{app_id}/test"
    ).json()["data"]["state"] == "ok"
    assert client.delete(f"/api/admin/integrations/agent-apps/{app_id}").status_code == 200


def test_page_and_admin_nav() -> None:
    page = client.get("/integrations")
    assert page.status_code == 200
    html = page.text
    assert "集成中心" in html
    assert "本地模型" in html
    assert "MCP 服务" in html
    assert "第三方智能体" in html
    assert 'src="integrations.js"' in html

    admin = client.get("/admin")
    assert admin.status_code == 200
    assert 'href="/integrations"' in admin.text
