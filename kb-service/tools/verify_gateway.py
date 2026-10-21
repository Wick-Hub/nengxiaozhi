"""campus-ai-gateway 对接自检（C-01 LLM 网关 · 任务 2.4 延伸）。

`campus-ai-gateway` 对上暴露 **OpenAI 兼容**接口（`/v1/models`、`/v1/chat/completions`），
因此能小智接入它是**配置级**动作——`LLMClient` 一行不用改。但「能接」不等于「接上就没坑」，
本工具把两边契约的核对固化成可复现检查。

## 两种模式

1. **静态自检**（默认，不需要网关）：核对能小智侧配置是否已按网关形态就位，
   并暴露接入前的结构性风险（别名未对齐、双重限流、request_id 不可对账）。
2. **在线自检**（`--base-url` + `--api-key`）：对真实网关跑端到端，
   逐项验证协议兼容性、SSE 不被缓冲、错误码可解析。

## 网关侧已被代码证实的契约（本工具据此断言）

| 事实 | 位置 |
| --- | --- |
| 数据面是透明反向代理，`FlushInterval=-1`（SSE 不缓冲） | `internal/proxy/twcore.go` |
| 响应体原样透传，`capture` 仅 1MB 上限的旁路审计副本 | `internal/app/server.go` `auditResponseWriter.Write` |
| `/v1/chat/completions` **要求 `model` 非空**，否则 400 `model_required` | `requiresModel()` |
| 客户端 `Authorization` 被**删除并替换**为 ThinkWatch Key | `authMiddleware` + `twcore.go` Director |
| Key 配了 `AllowedModels` 时，同 Key 的其他模型 → **403 `model_not_allowed`** | `handleV1` |
| 错误格式 `{"error":{"code","message","request_id"}}` | `internal/httpx/response.go` |
| 未列出的请求字段（`top_p`/`stream_options`/`messages`）**不校验、原样转发** | `requestEnvelope` 只有 5 个字段 |

用法::

    python tools/verify_gateway.py                          # 静态自检
    python tools/verify_gateway.py --self-test              # 起假端点验证「在线分支」本身可用
    python tools/verify_gateway.py --base-url http://gw:8080/v1 --api-key sk-campus-xxx
    python tools/verify_gateway.py --base-url ... --api-key ... --model campus-smart

退出码：全部通过为 0，存在 FAIL 为 1。
"""

from __future__ import annotations

import argparse
import inspect
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

os.environ.setdefault("KB_STORE", "json")

try:  # pragma: no cover - 仅影响交互输出
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

PASSED = 0
FAILED = 0
WARNED = 0

#: 已知厂商直连域名——命中即「不是网关」，接入后应改指网关
VENDOR_HOSTS = {
    "api.deepseek.com": "DeepSeek 直连",
    "ark.cn-beijing.volces.com": "火山方舟直连",
    "dashscope.aliyuncs.com": "阿里百炼直连",
    "open.bigmodel.cn": "智谱直连",
    "api.moonshot.cn": "Kimi 直连",
    "api.openai.com": "OpenAI 直连",
}

#: 网关错误码 → 中文归因（接入后排查用）
GATEWAY_ERRORS = {
    "invalid_api_key": "业务 Key 无效或已停用",
    "api_key_expired": "业务 Key 已过期",
    "ip_not_allowed": "调用方 IP 不在 Key 白名单",
    "model_required": "请求未带 model 字段",
    "model_not_allowed": "该 model 不在这个 Key 的白名单里",
    "rate_limit_exceeded": "Key 维度 RPM 超限",
    "token_limit_exceeded": "Key 维度 TPM 超限",
    "concurrency_limit_exceeded": "Key 维度并发超限",
    "tenant_request_quota_exceeded": "租户月度请求数超限",
    "tenant_token_quota_exceeded": "租户 token 配额超限",
}


def check(name: str, ok: bool, detail: str = "") -> bool:
    global PASSED, FAILED
    if ok:
        PASSED += 1
    else:
        FAILED += 1
    tail = f"  [{detail}]" if detail else ""
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{tail}")
    return ok


def warn(name: str, detail: str = "") -> None:
    """记录一条「不是错误、但接入前必须知道」的观察。"""
    global WARNED
    WARNED += 1
    tail = f"  [{detail}]" if detail else ""
    print(f"  [WARN] {name}{tail}")


def info(text: str) -> None:
    print(f"         {text}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def host_of(url: str) -> str:
    try:
        return (urlparse(str(url)).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""


def is_gateway_like(url: str) -> bool:
    """启发式：不是已知厂商域名 → 视为自建/网关地址。"""
    host = host_of(url)
    if not host:
        return False
    return host not in VENDOR_HOSTS


# ================================================================== 组 1：全局配置
def check_env_config() -> None:
    from app.config import settings
    from app.core import llm_router

    section("组 1  全局 LLM 配置（.env → default 路由）")
    route = llm_router.route_of(None)
    base = route.base_url or settings.llm_base_url
    info(f"LLM_BASE_URL = {base or '(空)'}")
    info(f"LLM_MODEL    = {settings.llm_model or '(空)'}")
    info(f"LLM_API_KEY  = {'已配置' if settings.llm_api_key else '未配置'}")
    info(f"路由来源      = {route.source}")

    base_ok = bool(base)
    check("已配置 LLM_BASE_URL", base_ok)
    if base_ok:
        host = host_of(base)
        if host in VENDOR_HOSTS:
            warn(
                "全局地址仍指向厂商直连，未走网关",
                f"{host} = {VENDOR_HOSTS[host]}",
            )
        elif is_gateway_like(base):
            check("全局地址形似网关（非已知厂商域名）", True, host)

    if settings.llm_model:
        check("已配置 LLM_MODEL", True, settings.llm_model)
    else:
        info("未配置 LLM_MODEL —— 将自动取网关清单的第一个模型（见组 2 核对）")

    if settings.llm_api_key:
        key = settings.llm_api_key
        if key.startswith("sk-campus-"):
            check("业务 Key 形似 sk-campus-*（网关签发格式）", True)
        else:
            warn(
                "Key 不是 sk-campus- 前缀",
                "若是厂商 Key 直连没问题；走网关时应换成网关签发的 sk-campus-*",
            )


# ================================================================== 组 2：网关模型清单
def check_gateway_models() -> None:
    """模型清单的唯一来源是网关 —— 这里核对「拿得到、解析得对」。

    架构变更后能小智不再有模型台账，所以原来的「库内模型记录核对」已无对象；
    改为核对 `GET {LLM_BASE_URL}/models` 的拉取结果。
    """
    from app.config import settings
    from app.services import gateway_admin

    section("组 2  网关模型清单（唯一来源）")
    got = gateway_admin.list_remote_models(refresh=True)

    info(f"清单地址：{got.get('baseUrl') or '(未配置)'}")
    if got["error"]:
        check("可拉取网关模型清单", False, str(got["error"])[:140])
        info("→ 修好这一项之前，助手的模型下拉会是空的、问答也会失败。")
        return

    check("可拉取网关模型清单", True, f"{len(got['items'])} 个")
    if not got["items"]:
        warn("网关返回的模型清单为空",
             "请把模型加入该业务 Key 的 AllowedModels 白名单")
        return

    for m in got["items"]:
        info(f"{m['id']:28s}  ownedBy={m.get('ownedBy') or '-'}")

    if not settings.llm_model:
        check("LLM_MODEL 省略 → 自动取网关默认", True, got["items"][0]["id"])
    else:
        default_model = settings.llm_model
        ids = {m["id"] for m in got["items"]}
        if default_model in ids:
            check("`.env` 的 LLM_MODEL 在网关清单内", True, default_model)
        else:
            warn(f"`.env` 的 LLM_MODEL（{default_model}）不在网关清单内",
                 "以 default 编码提问会由网关拒绝（403/404）")


# ================================================================== 组 3：助手绑定
def check_agent_bindings() -> None:
    from app.db import agent_store
    from app.services import gateway_admin

    section("组 3  助手 → 模型绑定一致性")
    try:
        agents = agent_store.list_agents()
    except Exception as exc:  # noqa: BLE001
        check("可读取助手", False, str(exc)[:120])
        return

    models = gateway_admin.list_remote_models()
    known = {m["id"] for m in models["items"]}
    check("可读取助手", True,
          f"{len(agents)} 个助手 / 网关提供 {len(known)} 个模型")

    if models["error"]:
        warn("网关清单拉取失败，本次不判定绑定是否有效",
             "网关不可达不等于模型不存在，避免误报")
        return

    orphan = []
    for a in agents:
        code = a.get("modelCode") or "default"
        if code == "default" or code in known:
            info(f"{a['agentName'][:20]:22s} modelCode={code}  [网关认可]")
        else:
            orphan.append((a["agentName"], code))
            info(f"{a['agentName'][:20]:22s} modelCode={code}  [不在网关清单]")

    if orphan:
        warn(
            f"{len(orphan)} 个助手绑定了网关清单外的模型",
            "这些助手提问会被网关拒绝；请在网关控制台加入白名单，或改绑其它模型",
        )
    else:
        check("所有助手的 modelCode 都被网关认可", True)

# ================================================================== 组 4：接入风险
def check_integration_risks() -> None:
    from app.config import settings
    from app.core import llm_client

    section("组 4  接入前必须知道的结构性风险")

    # 风险 1：双重限流
    if settings.rate_limit_enabled:
        warn(
            "双重限流已开启",
            "能小智按 IP 限流 + 网关按 Key 限流(RPM/TPM/并发)；同一请求可能被拦两次且话术不一致",
        )
    else:
        check("能小智侧限流已关闭（模型配额交由网关），无双重拦截", True)

    # 风险 2：request_id 可对账性（真实代码缺口）
    src = inspect.getsource(llm_client)
    has_rid = "request_id" in src
    if has_rid:
        check("LLM 客户端解析网关的 request_id（可与 gateway_request_log 对账）", True)
    else:
        warn(
            "LLM 客户端未解析 request_id —— 出错时无法与网关日志对账",
            "网关每个错误都带 request_id；当前只把 resp.text[:300] 塞进 LLMError",
        )

    # 风险 3：上游自述未验收项
    warn(
        "网关自述未最终验收的能力不可当能小智验收结论",
        "业务数据面 Failover / 30,000 RPM / 真实校园 IdP 均需在本校环境重跑",
    )

    # 风险 4：twcore 是路由的实际执行者
    warn(
        "只跑 gateway 不跑 twcore = 退化为纯限流+审计代理（无多厂商路由/降级）",
        "gateway 自身不实现路由引擎",
    )


# ================================================================== 组 5：在线端到端
def _headers(api_key: str) -> dict:
    return {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}


def check_online(base_url: str, api_key: str, model: str) -> None:
    import httpx

    section("组 5  在线端到端（真实网关）")
    base = base_url.rstrip("/")
    # 本地地址绕过系统代理（校园网 HTTP_PROXY 会截走 127.0.0.1）
    trust = host_of(base) not in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}

    with httpx.Client(timeout=60.0, trust_env=trust) as cli:
        # ---- /v1/models
        try:
            resp = cli.get(f"{base}/models", headers=_headers(api_key))
        except httpx.HTTPError as exc:
            check("可达 /v1/models", False, f"{type(exc).__name__}: {str(exc)[:120]}")
            return
        check("可达 /v1/models", resp.status_code == 200, f"HTTP {resp.status_code}")
        if resp.status_code == 200:
            try:
                payload = resp.json()
            except Exception:  # noqa: BLE001
                payload = {}
            entries = payload.get("data") if isinstance(payload, dict) else None
            ids = [e.get("id") for e in entries if isinstance(e, dict)] if entries else []
            info(f"可见模型：{ids or '(空)'}")
            if ids:
                warn("该 Key 配了 AllowedModels 白名单", "上列即白名单；白名单外的 model 会 403")
        else:
            _report_error_body(resp)

        # ---- 错误路径：无效 Key 必须是 401 且格式可解析
        try:
            bad = cli.post(
                f"{base}/chat/completions",
                headers=_headers("sk-campus-invalid-probe"),
                json={"model": model or "probe", "messages": [{"role": "user", "content": "hi"}]},
            )
            check("无效 Key 返回 401", bad.status_code == 401, f"HTTP {bad.status_code}")
            _report_error_body(bad)
        except httpx.HTTPError as exc:
            check("无效 Key 返回 401", False, str(exc)[:120])

        if not model:
            warn("未提供 --model，跳过成功路径（非流式 / 流式）验证")
            return

        # ---- 非流式成功路径
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "只回复两个字：收到"}],
            "temperature": 0.0,
            "max_tokens": 16,
        }
        t0 = time.monotonic()
        try:
            resp = cli.post(f"{base}/chat/completions", headers=_headers(api_key), json=payload)
        except httpx.HTTPError as exc:
            check("非流式 /chat/completions 成功", False, str(exc)[:150])
            return
        elapsed = int((time.monotonic() - t0) * 1000)
        ok = resp.status_code == 200
        check("非流式 /chat/completions 成功", ok, f"HTTP {resp.status_code} / {elapsed}ms")
        if not ok:
            _report_error_body(resp)
            return
        data = resp.json()
        check("响应含 choices[0].message.content",
              bool((data.get("choices") or [{}])[0].get("message", {}).get("content")))
        check("响应含 model 字段（能小智回填 modelCode 用）", bool(data.get("model")),
              str(data.get("model") or ""))
        check("响应含 usage（token 计量与对账）", bool(data.get("usage")),
              str(data.get("usage") or {}))
        ids = resp.headers.get_list("X-Request-Id")
        if ids:
            check("响应头带 X-Request-Id（可与网关日志对账）", True, ids[0])
            if len(ids) > 1:
                # 实测发现：网关自己生成 req_*，同时把上游 twcore 的响应头原样透传，
                # 于是有两个同名头。httpx 的 headers.get() 会用 ", " 拼接成
                # "req_x, tw_y" 的脏值——拿它对账在网关日志里查不到。
                warn(
                    f"检测到 {len(ids)} 个同名 X-Request-Id（网关透传了上游的头）",
                    f"取值必须用 get_list(...)[0] 取第一个 req_*；"
                    f"直接 headers.get() 会得到 {resp.headers.get('X-Request-Id')!r}",
                )
        else:
            warn("响应头无 X-Request-Id", "出错时无法与网关日志对账")

        # ---- 流式：验证 SSE 未被缓冲
        s_payload = {**payload, "stream": True, "stream_options": {"include_usage": True}}
        try:
            chunks: list[tuple[float, int]] = []
            seen_usage = False
            t_start = time.monotonic()
            with cli.stream(
                "POST", f"{base}/chat/completions",
                headers=_headers(api_key), json=s_payload,
            ) as resp:
                check("流式请求返回 200", resp.status_code == 200, f"HTTP {resp.status_code}")
                if resp.status_code != 200:
                    resp.read()
                    _report_error_body(resp)
                    return
                for raw in resp.iter_lines():
                    line = raw.strip()
                    if not line.startswith("data:"):
                        continue
                    body = line[5:].strip()
                    if not body or body == "[DONE]":
                        continue
                    chunks.append((time.monotonic() - t_start, len(body)))
                    if "usage" in body:
                        seen_usage = True
            total_ms = int((time.monotonic() - t_start) * 1000)

            check("流式收到多个 data 分块", len(chunks) > 1, f"{len(chunks)} 块 / {total_ms}ms")
            if chunks:
                ttft = int(chunks[0][0] * 1000)
                info(f"首个分块 {ttft}ms，全程 {total_ms}ms")
                # 被整体缓冲时首个分块会贴近总时长
                if total_ms > 300 and ttft >= total_ms * 0.9:
                    check("SSE 未被整体缓冲", False,
                          f"首块 {ttft}ms ≈ 总时长 {total_ms}ms，疑似被攒批")
                else:
                    check("SSE 未被整体缓冲（首块显著早于收尾）", True, f"{ttft}ms < {total_ms}ms")
            check("stream_options.include_usage 生效（末块带 usage）", seen_usage,
                  "上游或 twcore 可能不支持该字段")
        except httpx.HTTPError as exc:
            check("流式 /chat/completions 成功", False, str(exc)[:150])


def _report_error_body(resp) -> None:
    """解析网关错误体并给出中文归因（`{"error":{"code","message","request_id"}}`）。"""
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        info(f"响应体（非 JSON）：{resp.text[:200]}")
        return
    err = body.get("error") if isinstance(body, dict) else None
    if not isinstance(err, dict):
        info(f"响应体：{str(body)[:200]}")
        return
    code = str(err.get("code") or "")
    rid = str(err.get("request_id") or "")
    reason = GATEWAY_ERRORS.get(code, "未收录的错误码")
    info(f"错误码 {code}（{reason}）")
    if rid:
        info(f"request_id = {rid}  ← 可用它在 gateway_request_log 里定位这次请求")


# ===================================================================== 自测
def run_self_test() -> None:
    """起假网关，验证**在线分支本身**可用（含 403 错误归因）。

    没有这一步，在线分支就是「写了但从未执行过」——首次实现时它确实带着
    一个字段名错误（`api_base` vs `base_url`）与一个 keep-alive 陷阱。
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import _fake_gateway
    except ImportError as exc:  # pragma: no cover
        check("可导入假网关装置 tools/_fake_gateway.py", False, str(exc))
        return

    section("组 6  自测：用假端点验证在线分支可用")
    server, base = _fake_gateway.start()
    info(f"假网关：{base}")
    try:
        # ---- 成功路径：白名单内模型，check_online 内部逐条断言，此处只统计失败数
        before_fail = FAILED
        check_online(base, _fake_gateway.VALID_KEY, "campus-smart")
        happy_fails = FAILED - before_fail
        check("成功路径零 FAIL", happy_fails == 0, f"{happy_fails} 条失败")

        # ---- 403 路径：白名单外模型必须被独立识别并给出错误码
        check(
            "白名单外模型 → 403 且错误码可解析",
            _probe_forbidden(base, _fake_gateway.VALID_KEY, "deepseek-chat"),
        )
    finally:
        _fake_gateway.stop(server)
        info("假网关已关闭")


def _probe_forbidden(base: str, api_key: str, model: str) -> bool:
    """直接打一次，确认 403 与错误码解析都对。"""
    import httpx

    try:
        resp = httpx.post(
            f"{base.rstrip('/')}/chat/completions",
            headers=_headers(api_key),
            json={"model": model, "messages": [{"role": "user", "content": "hi"}]},
            timeout=15.0,
            trust_env=False,
        )
    except httpx.HTTPError as exc:
        info(f"请求异常：{str(exc)[:120]}")
        return False
    if resp.status_code != 403:
        info(f"期望 403，实得 {resp.status_code}")
        return False
    try:
        err = resp.json().get("error") or {}
    except Exception:  # noqa: BLE001
        return False
    ok = err.get("code") == "model_not_allowed" and bool(err.get("request_id"))
    if ok:
        info(f"403 code={err['code']} request_id={err['request_id']}")
    return ok


# ==================================================================== main
def main() -> int:
    parser = argparse.ArgumentParser(description="campus-ai-gateway 对接自检")
    parser.add_argument("--base-url", default="", help="网关地址，如 http://gw:8080/v1")
    parser.add_argument("--api-key", default="", help="网关签发的业务 Key（sk-campus-*）")
    parser.add_argument("--model", default="", help="网关虚拟模型别名，用于成功路径验证")
    parser.add_argument("--skip-static", action="store_true", help="只跑在线检查")
    parser.add_argument("--self-test", action="store_true",
                        help="起本地假端点，验证本工具的在线分支可用（不需要真实网关）")
    args = parser.parse_args()

    print("=" * 72)
    print("campus-ai-gateway 对接自检（C-01 LLM 网关）")
    print("=" * 72)

    if args.self_test:
        run_self_test()
        print("\n" + "=" * 72)
        print(f"结果：{PASSED} PASS / {FAILED} FAIL / {WARNED} WARN")
        print("=" * 72)
        return 1 if FAILED else 0

    if not args.skip_static:
        check_env_config()
        check_gateway_models()
        check_agent_bindings()
        check_integration_risks()

    if args.base_url and args.api_key:
        check_online(args.base_url, args.api_key, args.model)
    else:
        print("\n=== 组 5  在线端到端 ===")
        print("  未提供 --base-url / --api-key，跳过。")
        print("  网关就绪后执行：")
        print("    python tools/verify_gateway.py \\")
        print("        --base-url http://<gateway>:8080/v1 \\")
        print("        --api-key  sk-campus-xxxxx \\")
        print("        --model    <网关虚拟模型别名>")

    print("\n" + "=" * 72)
    print(f"结果：{PASSED} PASS / {FAILED} FAIL / {WARNED} WARN")
    print("=" * 72)
    if WARNED:
        print("WARN 不是失败——是接入前必须知道的观察项。")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
