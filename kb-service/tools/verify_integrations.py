"""集成中心验收（任务 4.8 / C-05、M-A-07/10）。

默认 **JSON 模式 + 临时数据文件**，无需 MySQL；``--store mysql`` 切到真实库。
MySQL 模式只写入 ``vfi-*`` 前缀的配置，结束时删除并恢复「当前启用」的本地模型，
不影响已有数据。

覆盖：

1. 表结构：``integration_local_model`` / ``integration_mcp_server`` /
   ``integration_agent_app``（仅 MySQL）；
2. 示例配置：首次写入 6 条、重复执行 0 条、全部带「示例数据」标记；
3. 本地模型：校验/唯一性/三态密钥/当前启用切换/mock 连通性；
4. MCP：Header 加密、名称回显、stdio 参数、mock 工具清单；
5. 第三方智能体：Dify / Coze / Webhook 三类、Token 三态；
6. 密钥安全：落库为 AES-GCM 密文，接口与页面不回显明文；
7. 路由：``LLM_PROVIDER=local`` 时按当前启用模型路由，切换立即生效；
8. live 适配器：真实本地 HTTP 服务验证 OpenAI / MCP JSON-RPC / Dify / Coze /
   Webhook 报文与失败记录；
9. 接口、权限、页面与导航；
10. 操作日志（仅 MySQL）。

用法::

    python tools/verify_integrations.py
    python tools/verify_integrations.py --store mysql
    python tools/verify_integrations.py --keep-data

退出码：全部通过为 0，存在 FAIL 为 1。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

os.environ.setdefault("KB_STORE", "json")
os.environ.setdefault("LLM_MOCK", "1")
os.environ.setdefault("AUTH_ENABLED", "false")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")

try:  # pragma: no cover - 仅影响交互输出
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

PASSED = 0
FAILED = 0
PREFIX = "vfi-"
SECRET = "sk-verify-integration-secret"
TOKEN = "verify-agent-token"


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
    else:
        FAILED += 1
    tail = f"  [{detail}]" if detail else ""
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{tail}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def info(text: str) -> None:
    print(f"         {text}")


def _mysql() -> bool:
    from app.config import settings

    return settings.store_label == "mysql"


# ==================================================================== 0. 表结构
def check_schema() -> bool:
    if not _mysql():
        info("JSON 模式：跳过 MySQL 表结构检查。")
        return True
    from sqlalchemy import text

    from app.db.session import db_session

    section("0. MySQL 表结构")
    want = {
        "integration_local_model": {
            "local_id", "model_code", "model_name", "base_url", "remote_model",
            "api_key_enc", "timeout", "status", "is_active", "remark",
            "last_test_at", "last_test_state", "last_test_error", "last_latency_ms",
            "created_at", "updated_at",
        },
        "integration_mcp_server": {
            "server_id", "server_code", "server_name", "transport", "endpoint",
            "command", "args_json", "headers_enc", "status", "remark",
            "last_test_at", "last_test_state", "last_test_error", "last_latency_ms",
            "tools_json", "created_at", "updated_at",
        },
        "integration_agent_app": {
            "app_id", "app_code", "app_name", "kind", "base_url", "remote_app_id",
            "token_enc", "status", "remark", "last_test_at", "last_test_state",
            "last_test_error", "last_latency_ms", "last_reply",
            "created_at", "updated_at",
        },
    }
    ok = True
    with db_session() as conn:
        for table, columns in want.items():
            rows = conn.execute(
                text(
                    "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t"
                ),
                {"t": table},
            ).scalars()
            have = set(rows)
            missing = columns - have
            check(f"{table} 表结构", not missing,
                  f"缺列 {sorted(missing)}" if missing else f"{len(have)} 列")
            ok = ok and not missing
    if not ok:
        info("请先执行：python tools/migrate_integrations_4_8.py")
    return ok


# ==================================================================== 清理
def snapshot_active() -> int | None:
    from app.db import integration_store as store

    active = store.active_local_model()
    return int(active["localId"]) if active else None


def cleanup(active_id: int | None) -> None:
    from app.db import integration_store as store

    for item in store.list_local_models():
        if str(item.get("code") or "").startswith(PREFIX):
            store.delete_local_model(item["localId"])
    for item in store.list_mcp_servers():
        if str(item.get("code") or "").startswith(PREFIX):
            store.delete_mcp_server(item["serverId"])
    for item in store.list_agent_apps():
        if str(item.get("code") or "").startswith(PREFIX):
            store.delete_agent_app(item["appId"])
    if _mysql() and active_id:
        store.activate_local_model(active_id)


# ==================================================================== 1. 示例
def check_seed() -> None:
    from app.services import integration_admin as service

    section("1. 示例配置")
    first = service.seed_examples()
    check("首次写入 6 条", first["total"] == 6, str(first["created"]))
    check("本地模型 2 条", first["created"]["localModels"] == 2, str(first["created"]))
    check("MCP 2 条", first["created"]["mcpServers"] == 2, str(first["created"]))
    check("智能体 2 条", first["created"]["agentApps"] == 2, str(first["created"]))
    second = service.seed_examples()
    check("重复写入 0 条", second["total"] == 0, str(second["created"]))

    items = (
        service.list_local_models()
        + service.list_mcp_servers()
        + service.list_agent_apps()
    )
    seeded = [i for i in items if str(i.get("code") or "") in {
        "local-qwen", "local-glm", "campus-kb-tools", "schedule-tools",
        "dify-campus", "coze-guide",
    }]
    check("示例均带可替换标记",
          len(seeded) == 6 and all(i["remark"] == service.SAMPLE_REMARK for i in seeded),
          f"{len(seeded)} 条")
    check("默认启用 local-qwen",
          any(i["code"] == "local-qwen" and i["active"] for i in service.list_local_models()))


# ==================================================================== 2. 本地模型
def check_local_models() -> tuple[int, int]:
    from app.db import integration_store as store
    from app.services import integration_admin as service

    section("2. 本地大模型")
    first = service.create_local_model({
        "code": f"{PREFIX}local-a",
        "name": "验收本地模型 A",
        "baseUrl": "http://127.0.0.1:18001/v1",
        "remoteModel": "qwen2.5-7b-instruct",
        "apiKey": SECRET,
        "timeout": 20,
    })
    check("新增本地模型", first["ok"], str(first.get("error") or ""))
    if not first["ok"]:
        return 0, 0
    a = first["data"]
    check("公开结构只给 hasKey", a["hasKey"] is True
          and "apiKey" not in a and "secret" not in a)
    check("接口不回显密钥", SECRET not in json.dumps(a, ensure_ascii=False))

    raw = store.get_local_model(a["localId"])["_secret"]
    check("密钥已加密落库", bool(raw) and bytes(raw).startswith(b"v1"))
    check("密文中不含明文", bool(raw) and SECRET.encode() not in bytes(raw))

    kept = service.update_local_model(a["localId"], {"name": "验收本地模型 A（改名）"})
    check("不传 Key 时保留", kept["ok"] and kept["data"]["hasKey"] is True)
    cleared = service.update_local_model(a["localId"], {"apiKey": ""})
    check("空串清空 Key", cleared["ok"] and cleared["data"]["hasKey"] is False)
    restored = service.update_local_model(a["localId"], {"apiKey": SECRET})
    check("重新写入 Key", restored["ok"] and restored["data"]["hasKey"] is True)

    duplicate = service.create_local_model({
        "code": f"{PREFIX}local-a", "name": "重复", "baseUrl": "http://127.0.0.1:1/v1",
        "remoteModel": "x",
    })
    check("重复编码 409", duplicate["status"] == 409, str(duplicate.get("status")))
    bad_url = service.create_local_model({
        "code": f"{PREFIX}bad-url", "name": "坏地址", "baseUrl": "ftp://x",
        "remoteModel": "x",
    })
    check("非法地址 400", bad_url["ok"] is False and "http(s)" in bad_url["error"])
    bad_timeout = service.update_local_model(a["localId"], {"timeout": 0})
    check("非法超时被拒", bad_timeout["ok"] is False and "1–600" in bad_timeout["error"])

    tested = service.test_local_model(a["localId"])
    check("mock 连通性测试", tested["ok"] and tested["data"]["state"] == "ok",
          str(tested.get("error") or ""))
    check("测试状态已记录", tested["ok"]
          and tested["data"]["item"]["lastTestState"] == "ok")

    second = service.create_local_model({
        "code": f"{PREFIX}local-b",
        "name": "验收本地模型 B",
        "baseUrl": "http://127.0.0.1:18002/v1",
        "remoteModel": "glm-4-9b-chat",
        "apiKey": SECRET,
    })["data"]
    check("设为当前启用 A", service.activate_local_model(a["localId"])["ok"])
    check("切换到 B", service.activate_local_model(second["localId"])["ok"])
    active = [i for i in service.list_local_models() if i["active"]]
    check("全表仅一条启用", len(active) == 1 and active[0]["code"] == f"{PREFIX}local-b",
          str([i["code"] for i in active]))
    return a["localId"], second["localId"]


# ==================================================================== 3. MCP
def check_mcp() -> int:
    from app.db import integration_store as store
    from app.services import integration_admin as service

    section("3. MCP 服务")
    created = service.create_mcp_server({
        "code": f"{PREFIX}kb-tools",
        "name": "验收 MCP 服务",
        "transport": "http",
        "endpoint": "http://127.0.0.1:19001/mcp",
        "headers": {"X-Token": "mcp-secret", "Authorization": "Bearer mcp"},
    })
    check("新增 HTTP MCP", created["ok"], str(created.get("error") or ""))
    if not created["ok"]:
        return 0
    item = created["data"]
    check("只回显 Header 名称", item["headerNames"] == ["Authorization", "X-Token"],
          str(item["headerNames"]))
    check("接口不回显 Header 值",
          "mcp-secret" not in json.dumps(item, ensure_ascii=False))
    raw = store.get_mcp_server(item["serverId"])["_secret"]
    check("Header 加密落库", bool(raw) and b"mcp-secret" not in bytes(raw))

    kept = service.update_mcp_server(item["serverId"], {"name": "验收 MCP（改名）"})
    check("不传 headers 时保留", kept["data"]["hasHeaders"] is True)
    cleared = service.update_mcp_server(item["serverId"], {"headers": {}})
    check("空对象清空 headers", cleared["data"]["hasHeaders"] is False)
    restored = service.update_mcp_server(
        item["serverId"], {"headers": {"X-Token": "mcp-secret"}}
    )
    check("重新写入 Header", restored["data"]["hasHeaders"] is True)

    stdio = service.create_mcp_server({
        "code": f"{PREFIX}stdio",
        "name": "验收 stdio MCP",
        "transport": "stdio",
        "endpoint": "",
        "command": "python",
        "args": ["-m", "mcp_server.schedule"],
    })
    check("stdio 配置可保存", stdio["ok"]
          and stdio["data"]["command"] == "python"
          and stdio["data"]["args"] == ["-m", "mcp_server.schedule"])
    unsafe = service.create_mcp_server({
        "code": f"{PREFIX}unsafe", "name": "危险命令", "transport": "stdio",
        "command": "python -c x",
    })
    check("拒绝 shell 元字符", unsafe["ok"] is False and "shell" in unsafe["error"])

    tested = service.test_mcp_server(item["serverId"])
    check("mock 工具探测", tested["ok"] and tested["data"]["state"] == "ok",
          str(tested.get("error") or ""))
    check("工具清单已记录", tested["ok"] and tested["data"]["item"]["toolCount"] == 3,
          str(tested["data"]["item"].get("toolCount") if tested["ok"] else ""))
    return item["serverId"]


# ==================================================================== 4. 智能体
def check_agents() -> int:
    from app.db import integration_store as store
    from app.services import integration_admin as service

    section("4. 第三方智能体")
    dify = service.create_agent_app({
        "code": f"{PREFIX}dify",
        "name": "验收 Dify",
        "kind": "dify",
        "baseUrl": "https://dify.example.edu/v1",
        "token": TOKEN,
    })
    check("新增 Dify", dify["ok"], str(dify.get("error") or ""))
    if not dify["ok"]:
        return 0
    item = dify["data"]
    check("只给 hasToken", item["hasToken"] is True and "token" not in item)
    raw = store.get_agent_app(item["appId"])["_secret"]
    check("Token 加密落库", bool(raw) and TOKEN.encode() not in bytes(raw))

    coze = service.create_agent_app({
        "code": f"{PREFIX}coze", "name": "验收 Coze", "kind": "coze",
        "baseUrl": "https://api.coze.cn", "remoteAppId": "7000000000000000001",
    })
    hook = service.create_agent_app({
        "code": f"{PREFIX}hook", "name": "验收 Webhook", "kind": "webhook",
        "baseUrl": "https://agent.example.edu/hook",
    })
    check("Coze 配置", coze["ok"] and coze["data"]["kindLabel"] == "Coze 智能体")
    check("Webhook 配置", hook["ok"] and hook["data"]["kindLabel"] == "自定义 Webhook")
    missing_id = service.create_agent_app({
        "code": f"{PREFIX}coze-bad", "name": "缺 App ID", "kind": "coze",
        "baseUrl": "https://api.coze.cn",
    })
    check("Coze 缺应用 ID 被拒", missing_id["ok"] is False and "应用 ID" in missing_id["error"])

    tested = service.test_agent_app(item["appId"])
    check("mock 智能体测试", tested["ok"] and tested["data"]["state"] == "ok",
          str(tested.get("error") or ""))
    check("回复已记录", tested["ok"] and "模拟回复" in tested["data"]["item"]["lastReply"])
    return item["appId"]


# ==================================================================== 5. 密钥
def check_secrets_at_rest() -> None:
    from app.db import integration_store as store

    section("5. 敏感信息落库形态")
    if not _mysql():
        text = store.INTEGRATION_PATH.read_text(encoding="utf-8")
        check("JSON 存储含密文包装", "_secretB64" in text)
        check("JSON 无本地模型明文", SECRET not in text)
        check("JSON 无智能体 Token 明文", TOKEN not in text)
        check("JSON 无 MCP Header 明文", "mcp-secret" not in text)
        return

    from sqlalchemy import select

    from app.db import models as M
    from app.db.session import db_session

    with db_session() as conn:
        local_key = conn.execute(
            select(M.IntegrationLocalModel.api_key_enc).where(
                M.IntegrationLocalModel.model_code == f"{PREFIX}local-a"
            )
        ).scalar_one()
        agent_token = conn.execute(
            select(M.IntegrationAgentApp.token_enc).where(
                M.IntegrationAgentApp.app_code == f"{PREFIX}dify"
            )
        ).scalar_one()
    check("本地模型 Key 为密文", bytes(local_key or b"").startswith(b"v1"))
    check("本地模型 Key 不含明文", SECRET.encode() not in bytes(local_key or b""))
    check("智能体 Token 为密文", bytes(agent_token or b"").startswith(b"v1"))
    check("智能体 Token 不含明文", TOKEN.encode() not in bytes(agent_token or b""))


# ==================================================================== 6. 路由
def check_routing(a_id: int, b_id: int) -> None:
    from app.config import settings
    from app.core import llm_router
    from app.services import gateway_admin, integration_admin as service

    section("6. 本地模型路由")
    original_provider = settings.llm_provider
    original_model = settings.llm_model
    try:
        settings.llm_provider = "local"
        settings.llm_model = "should-be-ignored"
        service.activate_local_model(a_id)

        route = llm_router.resolve("default")
        check("路由来源为本地", route.source == llm_router.SOURCE_LOCAL, route.source)
        check("模型编码取当前启用", route.model_code == f"{PREFIX}local-a", route.model_code)
        check("端点取本地配置", route.base_url == "http://127.0.0.1:18001/v1", route.base_url)
        check("模型名取本地配置", route.remote_model == "qwen2.5-7b-instruct", route.remote_model)
        check("管理结构不含 Key", "apiKey" not in route.to_dict())

        client_a = llm_router.get_client_for("default")
        check("客户端走本地端点", client_a._base_url == "http://127.0.0.1:18001/v1")
        check("客户端无需 Key 可用", client_a.available() is True)

        service.activate_local_model(b_id)
        client_b = llm_router.get_client_for("default")
        check("切换当前模型立即生效", client_b._model == "glm-4-9b-chat", client_b._model)
        check("切换后端点同步", client_b._base_url == "http://127.0.0.1:18002/v1")

        explicit = llm_router.resolve(f"{PREFIX}local-a")
        check("按编码指定本地模型", explicit.remote_model == "qwen2.5-7b-instruct")

        choices = gateway_admin.choices()
        check("模型下拉切换为本地", choices["provider"] == "local"
              and {i["modelCode"] for i in choices["items"]} >= {
                  f"{PREFIX}local-a", f"{PREFIX}local-b",
              })
    finally:
        settings.llm_provider = original_provider
        settings.llm_model = original_model
        llm_router.reset_routes()
        from app.core import llm_client

        llm_client.reset_client()


# ==================================================================== 7. live
class _Stub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests: list[dict] = []

    def log_message(self, *_a):
        return

    def _record(self, body: bytes) -> dict:
        try:
            payload = json.loads(body) if body else {}
        except ValueError:
            payload = {}
        type(self).requests.append({
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": payload,
        })
        return payload

    def _send(self, status: int, payload, content_type: str = "application/json",
              extra: dict | None = None) -> None:
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def do_GET(self):  # noqa: N802
        self._record(b"")
        if self.path == "/v1/models":
            self._send(200, {"data": [{"id": "qwen-live"}]})
            return
        if self.path.startswith("/bad/"):
            self._send(401, {"error": {"message": "invalid key"}})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        payload = self._record(self._body())
        if self.path == "/v1/chat/completions":
            self._send(200, {
                "model": payload.get("model"),
                "choices": [{"message": {"content": "pong"}}],
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
                    "result": {"serverInfo": {"name": "stub-mcp", "version": "1.0"}},
                }
                self._send(200, f"event: message\ndata: {json.dumps(event)}\n\n".encode(),
                           "text/event-stream", {"Mcp-Session-Id": "sess-live"})
                return
            if method == "tools/list":
                self._send(200, {
                    "jsonrpc": "2.0", "id": payload.get("id"),
                    "result": {"tools": [{"name": "search_kb"}, {"name": "get_schedule"}]},
                })
                return
            self._send(202, b"")
            return
        if self.path == "/v1/chat-messages":
            self._send(200, {"answer": "pong-dify", "conversation_id": "c-dify"})
            return
        if self.path == "/open_api/v2/chat":
            self._send(200, {
                "messages": [{"type": "answer", "content": "pong-coze"}],
                "conversation_id": "c-coze",
            })
            return
        if self.path == "/hook":
            self._send(200, {"reply": "pong-hook", "conversationId": "c-hook"})
            return
        self._send(404, {"error": "not found"})


def check_live_adapters() -> None:
    from app.config import settings
    from app.services import integration_admin as service

    section("7. live 适配器（真实本地 HTTP）")
    _Stub.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_address[1]}"
    originals = (
        settings.integration_test_provider,
        settings.integration_mcp_provider,
        settings.integration_agent_provider,
    )
    try:
        settings.integration_test_provider = "live"
        settings.integration_mcp_provider = "live"
        settings.integration_agent_provider = "live"

        local = service.create_local_model({
            "code": f"{PREFIX}live-local", "name": "live 本地模型",
            "baseUrl": f"{origin}/v1", "remoteModel": "qwen-live", "apiKey": SECRET,
        })
        data = service.test_local_model(local["data"]["localId"])["data"]
        check("OpenAI 兼容测试成功", data["state"] == "ok" and data["reply"] == "pong",
              str(data.get("error") or ""))
        check("OpenAI 模型清单", data["models"] == ["qwen-live"], str(data["models"]))
        calls = [r for r in _Stub.requests if r["path"] == "/v1/chat/completions"]
        check("OpenAI 带鉴权头",
              bool(calls) and calls[-1]["headers"].get("authorization") == f"Bearer {SECRET}")

        bad_local = service.create_local_model({
            "code": f"{PREFIX}live-bad", "name": "live 失败模型",
            "baseUrl": f"{origin}/bad/v1", "remoteModel": "x",
        })
        data = service.test_local_model(bad_local["data"]["localId"])["data"]
        check("失败写入 error 状态", data["state"] == "error"
              and "HTTP 401" in data["error"], str(data.get("error") or ""))
        check("失败原因可回看", "invalid key" in data["item"]["lastTestError"])

        mcp = service.create_mcp_server({
            "code": f"{PREFIX}live-mcp", "name": "live MCP", "transport": "http",
            "endpoint": f"{origin}/mcp", "headers": {"X-Token": "tok-live"},
        })
        data = service.test_mcp_server(mcp["data"]["serverId"])["data"]
        check("MCP JSON-RPC 探测成功", data["state"] == "ok"
              and [t["name"] for t in data["tools"]] == ["search_kb", "get_schedule"],
              str(data.get("error") or ""))
        calls = [r for r in _Stub.requests if r["path"] == "/mcp"]
        check("MCP 自定义 Header 生效",
              bool(calls) and calls[0]["headers"].get("x-token") == "tok-live")
        check("MCP 会话头已回传",
              len(calls) >= 3 and calls[-1]["headers"].get("mcp-session-id") == "sess-live")

        dify = service.create_agent_app({
            "code": f"{PREFIX}live-dify", "name": "live Dify", "kind": "dify",
            "baseUrl": f"{origin}/v1", "token": TOKEN,
        })
        data = service.test_agent_app(dify["data"]["appId"])["data"]
        check("Dify 测试成功", data["state"] == "ok" and data["reply"] == "pong-dify",
              str(data.get("error") or ""))

        coze = service.create_agent_app({
            "code": f"{PREFIX}live-coze", "name": "live Coze", "kind": "coze",
            "baseUrl": origin, "remoteAppId": "7000000000000000001",
        })
        data = service.test_agent_app(coze["data"]["appId"])["data"]
        check("Coze 测试成功", data["state"] == "ok" and data["reply"] == "pong-coze",
              str(data.get("error") or ""))

        hook = service.create_agent_app({
            "code": f"{PREFIX}live-hook", "name": "live Webhook", "kind": "webhook",
            "baseUrl": f"{origin}/hook",
        })
        data = service.test_agent_app(hook["data"]["appId"])["data"]
        check("Webhook 测试成功", data["state"] == "ok" and data["reply"] == "pong-hook",
              str(data.get("error") or ""))
    finally:
        (
            settings.integration_test_provider,
            settings.integration_mcp_provider,
            settings.integration_agent_provider,
        ) = originals
        server.shutdown()
        server.server_close()


# ==================================================================== 8. 接口
def check_api(client) -> None:
    section("8. 管理接口")
    overview = client.get("/api/admin/integrations/overview")
    check("概览 200", overview.status_code == 200, overview.text[:120])
    if overview.status_code == 200:
        data = overview.json()["data"]
        check("概览含模型来源", data["llmProvider"] in {"gateway", "local"},
              data["llmProvider"])
        check("概览含测试来源", data["mcpTestProvider"] in {"mock", "live"}
              and data["agentTestProvider"] in {"mock", "live"})
        check("概览含三类下拉", len(data["agentKinds"]) == 3
              and len(data["mcpTransports"]) == 3)

    created = client.post("/api/admin/integrations/local-models", json={
        "code": f"{PREFIX}api-local",
        "name": "接口本地模型",
        "baseUrl": "http://127.0.0.1:18009/v1",
        "remoteModel": "api-model",
        "apiKey": SECRET,
    })
    check("接口新增本地模型", created.status_code == 200, created.text[:160])
    if created.status_code == 200:
        item = created.json()["data"]
        check("接口不回显 Key", SECRET not in created.text)
        listed = client.get("/api/admin/integrations/local-models").json()["data"]["items"]
        check("清单包含新模型", any(i["localId"] == item["localId"] for i in listed))
        activated = client.post(
            f"/api/admin/integrations/local-models/{item['localId']}/activate"
        )
        check("接口设为当前启用", activated.status_code == 200, activated.text[:120])
        tested = client.post(
            f"/api/admin/integrations/local-models/{item['localId']}/test"
        )
        check("接口测试 200", tested.status_code == 200
              and tested.json()["data"]["state"] == "ok", tested.text[:120])
        patched = client.patch(
            f"/api/admin/integrations/local-models/{item['localId']}",
            json={"timeout": 45},
        )
        check("接口更新字段", patched.status_code == 200
              and patched.json()["data"]["timeout"] == 45)
        duplicate = client.post("/api/admin/integrations/local-models", json={
            "code": f"{PREFIX}api-local", "name": "重复",
            "baseUrl": "http://127.0.0.1:1/v1", "remoteModel": "x",
        })
        check("重复编码 409", duplicate.status_code == 409, str(duplicate.status_code))
        invalid = client.post("/api/admin/integrations/local-models", json={
            "code": f"{PREFIX}api-bad", "name": "坏地址",
            "baseUrl": "ftp://x", "remoteModel": "x",
        })
        check("非法地址 400", invalid.status_code == 400, str(invalid.status_code))
        deleted = client.delete(
            f"/api/admin/integrations/local-models/{item['localId']}"
        )
        check("接口删除 200", deleted.status_code == 200)
        check("重复删除 404", client.delete(
            f"/api/admin/integrations/local-models/{item['localId']}"
        ).status_code == 404)

    mcp = client.post("/api/admin/integrations/mcp-servers", json={
        "code": f"{PREFIX}api-mcp", "name": "接口 MCP", "transport": "http",
        "endpoint": "http://127.0.0.1:19009/mcp", "headers": {"X-Token": "api-secret"},
    })
    check("接口新增 MCP", mcp.status_code == 200, mcp.text[:160])
    if mcp.status_code == 200:
        item = mcp.json()["data"]
        check("Header 名称回显", item["headerNames"] == ["X-Token"])
        test = client.post(f"/api/admin/integrations/mcp-servers/{item['serverId']}/test")
        check("MCP 测试 200", test.status_code == 200
              and test.json()["data"]["toolCount"] == 3, test.text[:120])
        check("MCP 删除 200", client.delete(
            f"/api/admin/integrations/mcp-servers/{item['serverId']}"
        ).status_code == 200)

    agent = client.post("/api/admin/integrations/agent-apps", json={
        "code": f"{PREFIX}api-agent", "name": "接口智能体", "kind": "webhook",
        "baseUrl": "https://agent.example.edu/hook", "token": TOKEN,
    })
    check("接口新增智能体", agent.status_code == 200, agent.text[:160])
    if agent.status_code == 200:
        item = agent.json()["data"]
        check("Token 只给 hasToken", item["hasToken"] is True and TOKEN not in agent.text)
        test = client.post(f"/api/admin/integrations/agent-apps/{item['appId']}/test")
        check("智能体测试 200", test.status_code == 200
              and test.json()["data"]["state"] == "ok", test.text[:120])
        check("智能体删除 200", client.delete(
            f"/api/admin/integrations/agent-apps/{item['appId']}"
        ).status_code == 200)


# ==================================================================== 9. 权限
def check_permissions_page(client) -> None:
    from app.security import permissions

    section("9. 权限、页面与导航")
    check("权限点已注册",
          {"integrations.view", "integrations.manage"} <= set(permissions.all_codes()))
    check("GET → integrations.view",
          permissions.required_for("GET", "/api/admin/integrations/overview")
          == "integrations.view")
    check("POST → integrations.manage",
          permissions.required_for("POST", "/api/admin/integrations/local-models")
          == "integrations.manage")
    check("权限点总数 42", len(permissions.all_codes()) == 42,
          str(len(permissions.all_codes())))

    page = client.get("/integrations")
    check("集成中心页面 200", page.status_code == 200, str(page.status_code))
    check("页面含三个页签",
          all(word in page.text for word in ("本地模型", "MCP 服务", "第三方智能体")))
    check("页面加载脚本", 'src="integrations.js"' in page.text)
    admin = client.get("/admin")
    check("后台入口 200", admin.status_code == 200, str(admin.status_code))
    check("后台含集成中心入口", 'href="/integrations"' in admin.text)


# ==================================================================== 10. 审计
def check_oplog(client) -> None:
    if not _mysql():
        info("JSON 模式不落审计表，跳过 INTEGRATION_* 操作日志检查。")
        return
    section("10. 操作日志")
    for action, label in (
        ("INTEGRATION_SAVE", "保存配置"),
        ("INTEGRATION_TEST", "连通性测试"),
        ("INTEGRATION_ACTIVATE", "切换当前模型"),
        ("INTEGRATION_DELETE", "删除配置"),
    ):
        body = client.get(
            "/api/admin/operation-logs", params={"action": action, "pageSize": 50}
        ).json()["data"]
        check(f"操作日志留痕：{label}", body["total"] >= 1,
              f"{action} total={body['total']}")


# ==================================================================== main
def main() -> int:
    parser = argparse.ArgumentParser(description="集成中心验收（任务 4.8）")
    parser.add_argument("--store", default="json", choices=["json", "mysql"],
                        help="存储模式；默认 json（无需 MySQL）")
    parser.add_argument("--keep-data", action="store_true",
                        help="保留临时数据（默认测完删除）")
    args = parser.parse_args()

    os.environ["KB_STORE"] = args.store

    tmpdir = Path(tempfile.mkdtemp(prefix="verify-integrations-"))
    if not _mysql():
        from app.db import integration_store

        integration_store.INTEGRATION_PATH = tmpdir / "integrations.json"
        print(f"临时数据目录：{tmpdir}")

    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import app

    print("=" * 68)
    print("集成中心验收（任务 4.8 · C-05、M-A-07/10）")
    print(f"存储模式：{settings.store_label}    鉴权：AUTH_ENABLED={settings.auth_enabled}")
    print("=" * 68)

    client = TestClient(app)
    active_before: int | None = None
    ok = False
    try:
        if not check_schema():
            return 1
        active_before = snapshot_active()
        if not _mysql():
            check_seed()
        else:
            info("MySQL 模式：跳过示例写入，避免污染真实配置。")
        a_id, b_id = check_local_models()
        check_mcp()
        check_agents()
        check_secrets_at_rest()
        if a_id and b_id:
            check_routing(a_id, b_id)
        check_live_adapters()
        check_api(client)
        check_permissions_page(client)
        check_oplog(client)
        ok = True
    finally:
        cleanup(active_before)
        if not args.keep_data:
            shutil.rmtree(tmpdir, ignore_errors=True)
        elif not _mysql():
            print(f"临时数据保留在：{tmpdir}")

    print(f"\n结果：{PASSED} PASS / {FAILED} FAIL")
    return 0 if FAILED == 0 and ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
