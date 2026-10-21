"""能小智 ↔ campus-ai-gateway 端到端接入测试（任务 2.4 延伸）。

## 这个文件回答什么问题

前面的 `tools/verify_gateway.py` 是**用独立 httpx** 探网关的契约；本文件不同——
它驱动**能小智自己的问答链路**（`/api/chat` → `pipeline` → `generator` →
`llm_router` → `LLMClient` → 网关 → 回来），验证的是「接入之后能小智真的能跑通」，
而不是「网关的接口长得对不对」。

## 为什么用假网关而不是真网关

`campus-ai-gateway` 需要 Go 运行时 + PostgreSQL + Redis（本机都没有）。
`tools/_fake_gateway.py` 按它的**源码契约**实现（响应原样透传、SSE 不缓冲、
错误体 `{"error":{"code","message","request_id"}}`、`AllowedModels` 白名单），
因此能覆盖接入后能小智会遇到的真实分支。

**实机验收仍须在有网关的环境重跑**——本文件不能替代它。

## 覆盖的三类分支

1. **成功路径**：默认路径（`.env`）与库内模型（管理页配置）两条都要通，
   且 `modelCode` / `modelSource` 回填正确；
2. **上游失败**：403 白名单 / 401 无效 Key / 连接失败——**都必须是 502 + 结构化
   `errorDetail`，而不是 500**（这是接入前 `/api/chat` 的真实缺陷）；
3. **流式**：SSE 的 `delta`/`done` 正常，失败时 `error` 事件带 `errorDetail`。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import _fake_gateway  # noqa: E402  （须先补 sys.path）

from app.config import settings  # noqa: E402
from app.core import generator, llm_client, llm_router  # noqa: E402
from app.main import app  # noqa: E402
from app.services import gateway_admin  # noqa: E402

client = TestClient(app)

#: 假网关白名单里的模型（见 _fake_gateway.WHITELIST）
GOOD_MODEL = "campus-smart"
#: 不在白名单 → 触发 403 model_not_allowed
BLOCKED_MODEL = "deepseek-chat"


@pytest.fixture()
def gw(monkeypatch):
    """起假网关，并把能小智的全局 LLM 配置指向它。

    **必须清掉四处缓存**，否则会拿 monkeypatch 之前构造的旧实例、连向
    上一个测试已经关掉的端口（表现为莫名其妙的 connection_failed）：

    * ``llm_client._client`` —— ``default`` 路径的全局单例；
    * ``llm_router._clients`` —— 按 ``modelCode`` 缓存的多实例；
    * ``generator._generator`` —— **最容易漏的一个**：``Generator.__init__`` 里
      ``self.client = client or get_client()`` 把 client **固化**在实例上，
      只清 llm_client 是不够的；
    * ``gateway_admin`` 的**模型清单缓存**（30s TTL）—— 不清会把上一个用例
      从别的假网关拉到的清单串过来，表现为「清单与当前网关不符」。

    同样的道理在生产里也成立：改了模型配置要重启服务，否则跑的还是旧 client。
    """
    server, base = _fake_gateway.start()
    monkeypatch.setattr(settings, "llm_base_url", base)
    monkeypatch.setattr(settings, "llm_api_key", _fake_gateway.VALID_KEY)
    monkeypatch.setattr(settings, "llm_model", GOOD_MODEL)
    monkeypatch.setattr(settings, "llm_mock", False)
    _reset_llm_caches()
    yield base
    _reset_llm_caches()
    _fake_gateway.stop(server)


def _reset_llm_caches() -> None:
    """清空与 LLM 相关的全部缓存（含网关模型清单）。"""
    llm_client.reset_client()
    llm_router.reset_routes()
    generator.reset_generator()
    gateway_admin.reset_models_cache()


def _probe(model_code: str) -> dict:
    """调网关模型自测接口（替换了原 `model_admin.probe_model`）。"""
    resp = client.post(f"/api/admin/gateway/models/{model_code}/probe")
    return {"status": resp.status_code, "body": resp.json()}


def _ask(question: str = "校园卡怎么补办", agent_id: int | None = None) -> dict:
    """发一次非流式问答。

    ``agentId`` 是**字符串**类型（``ChatRequest.agent_id: str | None``），
    传 int 会被 FastAPI 判 422。不传时走默认路径（无助手 → 沿用 `.env`）。
    """
    payload: dict = {"question": question}
    if agent_id is not None:
        payload["agentId"] = str(agent_id)
    resp = client.post("/api/chat", json=payload)
    return {"status": resp.status_code, "body": resp.json()}


def _ask_stream(question: str = "校园卡怎么补办", agent_id: int | None = None) -> list[dict]:
    """收集 SSE 事件（TestClient 会缓冲整个响应体，故可直接解析）。"""
    payload: dict = {"question": question}
    if agent_id is not None:
        payload["agentId"] = str(agent_id)
    resp = client.post("/api/chat/stream", json=payload)
    assert resp.status_code == 200, f"流式接口本身应 200，实得 {resp.status_code}"
    events: list[dict] = []
    for line in resp.text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            body = line[5:].strip()
            if body:
                events.append(json.loads(body))
    return events


# ================================================================== 成功路径
def test_default_path_goes_through_gateway(gw) -> None:
    """未绑定模型的默认路径：走 `.env` → 网关，并回填 modelSource=env。"""
    got = _ask()
    assert got["status"] == 200, got["body"]
    body = got["body"]
    assert body["ok"] is True
    assert body["data"]["answer"], "应拿到模型回答"
    # 响应回显的 model 来自网关（假网关原样回显请求里的 model）
    assert body["data"]["model"] == GOOD_MODEL
    assert body["data"]["modelCode"] == "default"
    assert body["data"]["modelSource"] == "env"


def test_bound_model_goes_through_gateway(gw, monkeypatch) -> None:
    """助手绑定**网关模型名**后，问答真的走它（`modelSource=gateway`）。

    架构变更后不再需要「先在模型管理里建一条记录」——
    模型清单来自网关，``model_code`` 本身就是网关的模型名。
    """
    from app.db import agent_store

    agent = agent_store.create_agent(
        {"agentCode": "gw-agent", "agentName": "网关助手", "modelCode": GOOD_MODEL}
    )

    got = _ask(agent_id=agent["agentId"])
    assert got["status"] == 200, got["body"]
    body = got["body"]
    assert body["ok"] is True
    assert body["data"]["modelSource"] == "gateway"
    assert body["data"]["modelCode"] == GOOD_MODEL
    assert body["data"]["model"] == GOOD_MODEL

    agent_store.delete_agent(agent["agentId"])


def test_probe_gateway_model_reports_success(gw) -> None:
    """网关模型自测接口：走网关并回显地址与实际模型名。"""
    got = _probe(GOOD_MODEL)
    assert got["status"] == 200, got["body"]
    data = got["body"]["data"]
    assert data["ok"] is True
    assert data["baseUrl"] == gw
    assert data["modelCode"] == GOOD_MODEL
    assert data["respondedModel"] == GOOD_MODEL


def test_probe_rejects_model_not_in_gateway_list(gw) -> None:
    """不在网关清单里的编码：**不发调用**，直接给出指向白名单的提示。

    这比真发一次然后报 403 更快，而且错误信息更准确
    （``model_not_allowed`` 只说明"网关拒绝"，不说明"该去配白名单"）。
    """
    got = _probe("not-a-real-model")
    assert got["status"] == 200, got["body"]
    data = got["body"]["data"]
    assert data["ok"] is False
    assert data["code"] == "not_in_gateway_list"
    assert "白名单" in data["hint"]


# ================================================================== 失败路径
def test_gateway_403_becomes_502_not_500(gw, monkeypatch) -> None:
    """**最重要的用例**：网关白名单不匹配时，/api/chat 必须是 502 + 结构化错误。

    接入前这里是 500（`pipeline` 不捕获 `LLMError`，直接冒泡），
    用户只看到「Internal Server Error」，完全无法定位。
    """
    monkeypatch.setattr(settings, "llm_model", BLOCKED_MODEL)  # 不在白名单
    llm_client.reset_client()

    got = _ask()
    assert got["status"] == 502, f"应为 502，实得 {got['status']}：{got['body']}"
    body = got["body"]
    assert body["ok"] is False
    assert body["data"] is None

    detail = body["errorDetail"]
    assert detail["status"] == 403
    assert detail["code"] == "model_not_allowed"
    assert detail["requestId"] == _fake_gateway.REQUEST_ID, "request_id 必须被解析出来"
    assert "白名单" in detail["hint"], "必须给出可执行的处置建议"

    # 面向用户的消息要能看懂，而不是一坨原始响应文本
    assert "403" in body["error"] and "model_not_allowed" in body["error"]
    assert _fake_gateway.REQUEST_ID in body["error"]


def test_gateway_401_invalid_key_becomes_502(gw, monkeypatch) -> None:
    """业务 Key 无效 → 502，且错误码可辨（而不是笼统的上游失败）。"""
    monkeypatch.setattr(settings, "llm_api_key", "sk-campus-wrong-key")
    llm_client.reset_client()

    got = _ask()
    assert got["status"] == 502
    assert got["body"]["errorDetail"]["code"] == "invalid_api_key"
    assert got["body"]["errorDetail"]["status"] == 401
    assert "sk-campus" in got["body"]["errorDetail"]["hint"]


def test_gateway_unreachable_becomes_502(monkeypatch) -> None:
    """网关没起来（连接被拒）→ 502，并提示检查地址/进程（不是 500）。"""
    monkeypatch.setattr(settings, "llm_api_key", _fake_gateway.VALID_KEY)
    monkeypatch.setattr(settings, "llm_mock", False)
    # 指向一个没人监听的端口
    monkeypatch.setattr(settings, "llm_base_url", "http://127.0.0.1:9/v1")
    llm_client.reset_client()

    got = _ask()
    assert got["status"] == 502, got["body"]
    assert got["body"]["errorDetail"]["code"] == "connection_failed"
    assert got["body"]["errorDetail"]["status"] is None
    assert "无法连接" in got["body"]["error"] or "连不上" in got["body"]["error"]


def test_probe_reports_gateway_error_detail(gw, monkeypatch) -> None:
    """自测失败时也要给出网关错误码与建议（不是只给一坨文本）。

    这里走 ``default``（让全局模型名变成白名单外的），触发网关 403。
    """
    monkeypatch.setattr(settings, "llm_model", BLOCKED_MODEL)
    llm_client.reset_client()
    llm_router.reset_routes()

    got = _probe("default")
    assert got["status"] == 200, got["body"]
    data = got["body"]["data"]
    assert data["ok"] is False
    assert data["code"] == "model_not_allowed"
    assert data["status"] == 403
    assert data["requestId"] == _fake_gateway.REQUEST_ID
    assert "白名单" in data["hint"]


# ================================================================== 流式
def test_stream_success_through_gateway(gw) -> None:
    """流式：delta 增量 + done 收尾，全程走网关。"""
    events = _ask_stream()
    kinds = [e["type"] for e in events]
    assert kinds.count("delta") > 0, f"应收到增量，实得 {kinds}"
    assert kinds[-1] == "done", f"应以 done 收尾，实得 {kinds}"
    assert "error" not in kinds

    done = events[-1]
    assert done["answer"]
    assert done["model"] == GOOD_MODEL
    assert done["modelCode"] == "default"
    assert done["session_id"]
    # 追加字段仍在（老前端忽略即可）
    assert "blocks" in done and "answerHtml" in done
    # delta 契约不变
    first_delta = next(e for e in events if e["type"] == "delta")
    assert set(first_delta) >= {"type", "session_id", "text"}


def test_stream_error_event_carries_detail(gw, monkeypatch) -> None:
    """流式失败：error 事件必须带 errorDetail，且保留原有 ``error`` 字段。"""
    monkeypatch.setattr(settings, "llm_model", BLOCKED_MODEL)
    llm_client.reset_client()

    events = _ask_stream()
    kinds = [e["type"] for e in events]
    assert kinds[-1] == "error", f"应以 error 收尾，实得 {kinds}"

    err = events[-1]
    # 老字段仍在（前端契约不破）
    assert isinstance(err["error"], str) and err["error"]
    # 新字段把网关信息带出来
    detail = err["errorDetail"]
    assert detail["code"] == "model_not_allowed"
    assert detail["status"] == 403
    assert detail["requestId"] == _fake_gateway.REQUEST_ID
    assert "白名单" in detail["hint"]


# ================================================================== 契约守卫
def test_llm_error_dict_shape_is_stable() -> None:
    """``LLMError.to_dict()`` 的键集合是前端契约，必须稳定。"""
    err = llm_client.LLMError("x", status_code=403, code="c", request_id="r", model_code="m")
    assert set(err.to_dict()) == {"message", "status", "code", "requestId", "modelCode", "hint"}
    # 单参构造（老用法）仍可用，且不炸
    plain = llm_client.LLMError("老式消息")
    assert str(plain) == "老式消息"
    assert plain.to_dict()["status"] is None and plain.to_dict()["hint"] == ""


def test_upstream_403_from_gateway_is_actionable() -> None:
    """网关错误体 → 可读消息 + 中文归因（不依赖真实网络）。"""
    err = llm_client.parse_upstream_error(
        403,
        json.dumps(
            {
                "error": {
                    "code": "model_not_allowed",
                    "message": "Model is not allowed for this API key",
                    "request_id": "req_x",
                }
            }
        ),
        model_code="campus-smart",
    )
    assert err.status_code == 403 and err.code == "model_not_allowed"
    assert err.request_id == "req_x"
    assert "白名单" in err.hint
    assert "request_id=req_x" in str(err)


def test_duplicate_request_id_header_is_normalized() -> None:
    """同名响应头必须只取第一个 —— 这是**接真实网关才发现**的坑。

    campus-ai-gateway 自己生成 ``X-Request-Id: req_*``，同时把上游
    （ThinkWatch Core）的响应头**原样透传**，于是响应里有两个同名头。
    ``httpx`` 对重复头用 ``, `` 拼接，直接塞进 ``requestId`` 会得到
    ``"req_aaa, tw_bbb"`` 这种脏值 —— 拿它去网关日志里根本查不到，
    等于**对账能力失效**。真实网关上的复现记录：成功路径与上游透传路径
    返回两个头；网关自己产生的错误（401/403/限流）只有一个干净的头。
    """
    import httpx

    # ① 先用真实 httpx.Response 复现「两个同名头 → 逗号拼接」这个行为本身
    resp = httpx.Response(
        502,
        headers=[("X-Request-Id", "req_aaa"), ("X-Request-Id", "tw_bbb")],
        text="",
    )
    raw = resp.headers.get("X-Request-Id", "")
    assert raw == "req_aaa, tw_bbb", f"httpx 的拼接行为变了？实得 {raw!r}"

    # ② 走真实取值路径：解析后必须是第一个（网关自己生成的 req_*）
    err = llm_client.parse_upstream_error(502, "", request_id=raw)
    assert err.request_id == "req_aaa", f"未归一化：{err.request_id!r}"
    assert "," not in err.request_id

    # ③ 单个头（网关自己产生的错误）不受影响
    err2 = llm_client.parse_upstream_error(403, "", request_id="req_clean")
    assert err2.request_id == "req_clean"

    # ④ 错误体里的 request_id 优先于响应头（它由网关自己写入，最权威）
    err3 = llm_client.parse_upstream_error(
        403,
        json.dumps({"error": {"code": "model_not_allowed", "request_id": "req_body"}}),
        request_id="req_header, tw_x",
    )
    assert err3.request_id == "req_body"

    # ⑤ 头为空时不应炸
    assert llm_client.parse_upstream_error(500, "", request_id="").request_id == ""
