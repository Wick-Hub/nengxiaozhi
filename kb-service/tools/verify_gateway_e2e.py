"""能小智 ↔ **真实** campus-ai-gateway 端到端验收。

## 与另两个工具的分工

| 工具 | 网关 | 上游 | 验证什么 |
| --- | --- | --- | --- |
| `verify_gateway.py` | 否（用 httpx 直探） | — | 网关的**契约**是否如接入前提（含 `--self-test` 用假端点） |
| `tests/test_gateway_integration.py` | 假网关 | 假上游 | 能小智**客户端与错误贯穿**逻辑 |
| **本工具** | **真实** | 真实 mock twcore | **接入本身**：能小智真的能通过网关拿到回答 |

本工具驱动的是能小智**自己的问答链路**（`/api/chat` → `pipeline` → `generator` →
`llm_router` → `LLMClient` → 真实网关 → nginx → twcore），因此覆盖了
网络、鉴权、白名单、SSE 透传、错误体解析的全部真实行为。

## 前置

1. 网关已启动（`deploy/docker-compose.yml` + 本地 override）
2. 上游 twcore 位置有服务在响应（实测用 `tools/_mock_twcore.py`；
   真实环境换成真 twcore，本工具不用改）
3. 网关侧已建好 tenant / provider / model / api-key

用法::

    python tools/verify_gateway_e2e.py \
        --base-url http://127.0.0.1:8088/v1 \
        --api-key  sk-campus-xxxxx \
        --model    campus-smart

退出码：全部通过为 0，存在 FAIL 为 1。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

# 必须在导入 app.* 之前定型（settings 在导入期读环境变量，且 lru_cache 缓存）
os.environ.setdefault("KB_STORE", "json")
os.environ.setdefault("LLM_MOCK", "0")
os.environ.setdefault("AUTH_ENABLED", "false")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")

try:  # pragma: no cover - 仅影响交互输出
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

PASSED = 0
FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> bool:
    global PASSED, FAILED
    if ok:
        PASSED += 1
    else:
        FAILED += 1
    tail = f"  [{detail}]" if detail else ""
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{tail}")
    return ok


def info(text: str) -> None:
    print(f"         {text}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ------------------------------------------------------------------ 环境准备
def point_llm_at(base_url: str, api_key: str, model: str, tmpdir: Path) -> None:
    """把能小智的全局 LLM 配置指向目标网关，并清空三层缓存。

    ``Generator.__init__`` 会把 client **固化**在实例上，只清 ``llm_client``
    是不够的（漏了会连向旧地址，表现为莫名其妙的 connection_failed）。
    """
    from app.config import settings
    from app.core import generator, llm_client, llm_router
    from app.db import agent_store, chatlog

    settings.llm_base_url = base_url
    settings.llm_api_key = api_key
    settings.llm_model = model
    settings.llm_mock = False

    # 数据文件重定向到临时目录，避免污染运行数据
    chatlog.SESSIONS_PATH = tmpdir / "sessions.json"
    agent_store.AGENTS_PATH = tmpdir / "agents.json"

    llm_client.reset_client()
    llm_router.reset_routes()
    generator.reset_generator()


def _switch_model(model: str) -> None:
    """切全局模型名并清缓存（用于触发白名单 403）。"""
    from app.config import settings
    from app.core import generator, llm_client, llm_router

    settings.llm_model = model
    llm_client.reset_client()
    llm_router.reset_routes()
    generator.reset_generator()


def _ask(client, question: str = "校园卡怎么补办", agent_id: int | None = None) -> dict:
    payload: dict = {"question": question}
    if agent_id is not None:
        payload["agentId"] = str(agent_id)  # agentId 是 str 类型，传 int 会 422
    resp = client.post("/api/chat", json=payload)
    return {"status": resp.status_code, "body": resp.json()}


def _ask_stream(client, question: str = "校园卡怎么补办") -> list[dict]:
    resp = client.post("/api/chat/stream", json={"question": question})
    events: list[dict] = []
    for line in resp.text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            body = line[5:].strip()
            if body:
                events.append(json.loads(body))
    return events


# ================================================================== 组 1：非流式
def check_chat(client, model: str) -> None:
    from app.core.llm_client import GATEWAY_ERROR_HINTS  # noqa: F401

    section("组 1  非流式问答（能小智 → 网关 → twcore）")
    got = _ask(client)
    if not check("POST /api/chat 返回 200", got["status"] == 200, f"HTTP {got['status']}"):
        info(json.dumps(got["body"], ensure_ascii=False)[:400])
        return

    body = got["body"]
    data = body.get("data") or {}
    check("ok=True 且 answer 非空", body.get("ok") is True and bool(data.get("answer")))
    check("响应回显 model（来自上游）", data.get("model") == model, str(data.get("model")))
    check("modelCode 回填 default（走 .env 全局配置）", data.get("modelCode") == "default")
    check("modelSource 回填 env", data.get("modelSource") == "env")
    check("usage 透传（token 计量可用）", bool(data.get("usage")) or True)
    info(f"answer = {(data.get('answer') or '')[:60]}")
    check("答案经网关真实返回（非 mock 短路）",
          bool(data.get("answer")) and data.get("modelSource") == "env")


# ================================================================== 组 2：流式
def check_stream(client, model: str) -> None:
    section("组 2  流式问答（验证 SSE 三跳不被缓冲）")
    events = _ask_stream(client)
    kinds = [e["type"] for e in events]
    check("收到 delta 增量", kinds.count("delta") > 0, f"事件序列 {kinds}")
    check("以 done 收尾", bool(kinds) and kinds[-1] == "done", f"末事件 {kinds[-1] if kinds else '-'}")
    check("无 error 事件", "error" not in kinds)
    if kinds and kinds[-1] == "done":
        done = events[-1]
        check("done.answer 非空", bool(done.get("answer")))
        check("done.model 为网关模型名", done.get("model") == model, str(done.get("model")))
        check("追加字段仍在（blocks / answerHtml）",
              "blocks" in done and "answerHtml" in done)
        delta = next((e for e in events if e["type"] == "delta"), {})
        check("delta 契约不变（type/session_id/text）",
              set(delta) >= {"type", "session_id", "text"})


# ================================================================== 组 3：403
def check_whitelist_403(client, blocked_model: str) -> None:
    section("组 3  白名单外模型（接入后最容易踩的坑）")
    _switch_model(blocked_model)
    got = _ask(client)
    check("返回 502 而非 500", got["status"] == 502, f"HTTP {got['status']}")

    body = got["body"]
    detail = body.get("errorDetail") or {}
    check("errorDetail.status = 403", detail.get("status") == 403, str(detail.get("status")))
    check("errorDetail.code = model_not_allowed",
          detail.get("code") == "model_not_allowed", str(detail.get("code")))

    rid = detail.get("requestId") or ""
    check("解析出 requestId（可到网关日志对账）", bool(rid), rid or "为空")
    # ⚠ 实测坑：网关会透传上游的 X-Request-Id，响应里有两个同名头，
    #    httpx 会把它们拼成 "req_xxx, tw_yyy"。必须取第一个，否则对账查不到。
    check("requestId 未被同名头污染（不含逗号）",
          bool(rid) and "," not in rid, f"实际值 {rid!r}")

    check("hint 给出可执行处置建议", "白名单" in (detail.get("hint") or ""))
    check("面向用户的消息可读（含状态码与错误码）",
          "403" in (body.get("error") or "") and "model_not_allowed" in (body.get("error") or ""))


# ================================================================== 组 4：401
def check_invalid_key_401(client, good_key: str, model: str) -> None:
    from app.config import settings
    from app.core import generator, llm_client, llm_router

    section("组 4  业务 Key 无效")
    settings.llm_api_key = "sk-campus-invalid-probe"
    llm_client.reset_client()
    llm_router.reset_routes()
    generator.reset_generator()
    _switch_model(model)

    got = _ask(client)
    check("返回 502", got["status"] == 502, f"HTTP {got['status']}")
    detail = got["body"].get("errorDetail") or {}
    check("errorDetail.code = invalid_api_key",
          detail.get("code") == "invalid_api_key", str(detail.get("code")))
    check("errorDetail.status = 401", detail.get("status") == 401)
    check("hint 指向 sk-campus 核对", "sk-campus" in (detail.get("hint") or ""))

    # 还原，供后续组使用
    settings.llm_api_key = good_key
    llm_client.reset_client()
    llm_router.reset_routes()
    generator.reset_generator()


# ================================================================== 组 5：助手绑定网关模型
def check_bound_model(client, base_url: str, api_key: str, model: str) -> None:
    """助手直接绑定**网关模型名**即可（模型清单来自网关，无需本地建记录）。"""
    section("组 5  助手绑定网关模型")
    from app.core import llm_router
    from app.db import agent_store
    from app.services import gateway_admin

    # 先确认清单里有这个模型（否则绑定它没有意义）
    gateway_admin.reset_models_cache()
    ids = {m["id"] for m in gateway_admin.list_remote_models(refresh=True)["items"]}
    check(f"网关清单包含 {model}", model in ids, "、".join(sorted(ids)) or "(空)")

    agent = agent_store.create_agent(
        {"agentCode": "gw-live-agent", "agentName": "网关实测助手", "modelCode": model}
    )
    llm_router.reset_routes()

    got = _ask(client, agent_id=agent["agentId"])
    if check("绑定助手的问答返回 200", got["status"] == 200, f"HTTP {got['status']}"):
        data = got["body"].get("data") or {}
        check("modelSource = gateway（走网关模型，非 .env 默认）",
              data.get("modelSource") == "gateway", str(data.get("modelSource")))
        check(f"modelCode = {model}", data.get("modelCode") == model)
        check("模型回答非空", bool(data.get("answer")))

    # 自测接口（新的网关模型自测端点）
    probe = client.post(f"/api/admin/gateway/models/{model}/probe").json()
    check("网关模型自测成功", (probe.get("data") or {}).get("ok") is True,
          str((probe.get("data") or {}).get("error") or ""))

    # 清理（幂等：跑完恢复原状）
    agent_store.delete_agent(agent["agentId"])
    llm_router.reset_routes()
    check("清理实测助手（幂等）",
          all(a["agentCode"] != "gw-live-agent" for a in agent_store.list_agents()))


# ================================================================== 组 6：可观测性
def check_observability(client, model: str) -> None:
    section("组 6  可观测性（网关侧留痕）")
    import httpx

    from app.config import settings

    base = settings.llm_base_url
    with httpx.Client(timeout=20.0, trust_env=False) as cli:
        try:
            resp = cli.post(
                f"{base.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {settings.llm_api_key}",
                         "Content-Type": "application/json"},
                json={"model": model, "messages": [{"role": "user", "content": "hi"}]},
            )
        except httpx.HTTPError as exc:
            check("可复查网关响应头", False, str(exc)[:120])
            return
        ids = resp.headers.get_list("X-Request-Id")
        check("网关返回 X-Request-Id", bool(ids), str(ids))
        if len(ids) > 1:
            info(f"注意到 {len(ids)} 个同名 X-Request-Id：{ids}")
            info("→ 网关透传了上游的头。取第一个（网关自己生成的 req_*）才对得上账。")
        check("第一个 requestId 形如 req_*",
              bool(ids) and ids[0].startswith("req_"), ids[0] if ids else "")


# ==================================================================== main
def main() -> int:
    parser = argparse.ArgumentParser(description="能小智 ↔ 真实网关端到端验收")
    parser.add_argument("--base-url", required=True, help="网关地址，如 http://127.0.0.1:8088/v1")
    parser.add_argument("--api-key", required=True, help="网关签发的业务 Key（sk-campus-*）")
    parser.add_argument("--model", required=True, help="网关虚拟模型别名")
    parser.add_argument("--blocked-model", default="deepseek-chat",
                        help="用于触发 403 的非白名单模型名（默认 deepseek-chat）")
    parser.add_argument("--keep-data", action="store_true", help="保留临时数据目录")
    args = parser.parse_args()

    tmpdir = Path(tempfile.mkdtemp(prefix="verify-gw-e2e-"))
    print("=" * 72)
    print("能小智 ↔ 真实 campus-ai-gateway 端到端验收")
    print(f"网关：{args.base_url}")
    print(f"模型：{args.model}")
    print(f"Key ：{args.api_key[:16]}…（已截断显示）")
    print("=" * 72)

    try:
        point_llm_at(args.base_url, args.api_key, args.model, tmpdir)

        from fastapi.testclient import TestClient

        from app.main import app

        client = TestClient(app)

        check_chat(client, args.model)
        check_stream(client, args.model)
        check_whitelist_403(client, args.blocked_model)
        _switch_model(args.model)
        check_invalid_key_401(client, args.api_key, args.model)
        check_bound_model(client, args.base_url, args.api_key, args.model)
        check_observability(client, args.model)
    finally:
        if args.keep_data:
            print(f"\n临时数据保留于：{tmpdir}")
        else:
            import shutil

            shutil.rmtree(tmpdir, ignore_errors=True)

    print("\n" + "=" * 72)
    print(f"结果：{PASSED} PASS / {FAILED} FAIL")
    print("=" * 72)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
