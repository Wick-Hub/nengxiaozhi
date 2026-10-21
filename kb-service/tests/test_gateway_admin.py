"""模型网关可观测性测试（C-01 接入 · 2.4 延伸）。

覆盖四层：

1. **URL 解析**：``origin_of`` / ``host_of`` 的边界（带/不带 ``/v1``、非法输入）；
2. **「形似网关」判定**：厂商域名 → 未走网关；自建地址 → 视为网关；
3. **探测三态**：``online`` / ``unknown`` / ``offline`` ——
   **用真实本地 HTTP 服务验证**，不是 monkeypatch 出来的假象；
4. **总览组装**：空配置 / 厂商直连 / 网关在线 / 网关离线 四种 hint，
   以及端点去重（.env 与模型记录指向同一 origin 时只出现一次）。

第 3 层刻意用真实 socket：``httpx`` 的超时、状态码解析、以及
**本地地址绕过系统代理**（校园网 ``HTTP_PROXY`` 会让 127.0.0.1 探测不到）
都只有在真实 HTTP 往返里才会暴露。
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.config import settings
from app.services import gateway_admin as ga


# ------------------------------------------------------------------ 真实本地端点
class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    #: 类级可调：由 fixture 按用例改写，决定 ``/healthz`` 怎么响应
    health_status = 200
    health_body = b"ok"
    #: ``/models`` 的返回体（模型清单的唯一来源）。``None`` → 返回 500
    models_body: bytes | None = b'{"data":[{"id":"deepseek-flash","owned_by":"campus-ai-gateway"}]}'
    models_status = 200

    def log_message(self, *_a):
        return

    def do_GET(self):  # noqa: N802
        if self.path == "/healthz":
            self.send_response(type(self).health_status)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(type(self).health_body)))
            self.end_headers()
            self.wfile.write(type(self).health_body)
            return
        if self.path.rstrip("/").endswith("/models"):
            body = type(self).models_body
            if body is None:
                self.send_response(type(self).models_status)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(type(self).models_status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture()
def live_server():
    """起一个真实 HTTP 服务（同时提供 ``/healthz`` 与 ``/models``），端口由系统分配。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_address[1]}"
    _Handler.health_status = 200
    _Handler.health_body = b"ok"
    _Handler.models_body = b'{"data":[{"id":"deepseek-flash","owned_by":"campus-ai-gateway"}]}'
    _Handler.models_status = 200
    ga.reset_models_cache()
    yield origin
    ga.reset_models_cache()
    server.shutdown()
    server.server_close()


# ================================================================== 1. URL 解析
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://gw:8080/v1", "http://gw:8080"),
        ("http://gw:8080", "http://gw:8080"),
        ("http://gw:8080/v1/", "http://gw:8080"),
        ("https://gw.example.edu.cn/v1", "https://gw.example.edu.cn"),
        ("HTTP://GW:8080/v1", "http://GW:8080"),  # scheme 归一为小写（urlparse 行为）
        ("", ""),
        ("   ", ""),
        ("gw:8080", ""),  # 缺 scheme
        ("ftp://gw:8080", ""),  # 非 http(s)
        ("http:///v1", ""),  # 缺主机
    ],
)
def test_origin_of(raw: str, expected: str) -> None:
    assert ga.origin_of(raw) == expected


def test_host_of_handles_garbage() -> None:
    assert ga.host_of("http://127.0.0.1:8088/v1") == "127.0.0.1"
    assert ga.host_of("") == ""
    assert ga.host_of("not a url") == ""


# ================================================================== 2. 网关判定
def test_is_gateway_like_excludes_vendor_hosts() -> None:
    """用排除法：厂商域名是有限的确定集合，不属于它的一律视为自建服务。"""
    assert ga.is_gateway_like("https://api.deepseek.com") is False
    assert ga.is_gateway_like("https://dashscope.aliyuncs.com") is False
    assert ga.is_gateway_like("http://127.0.0.1:8088") is True
    assert ga.is_gateway_like("http://gw.internal:8080") is True
    assert ga.is_gateway_like("") is False


def test_vendor_hosts_all_have_labels() -> None:
    """厂商表必须带中文标签——前端直接显示它，空标签会露出裸域名。"""
    assert ga.VENDOR_HOSTS
    assert all(host and label for host, label in ga.VENDOR_HOSTS.items())


def test_local_hosts_bypass_proxy() -> None:
    """本地地址必须绕过系统代理，否则校园网的 HTTP_PROXY 会让本地网关探测不到。

    这是真实踩过的坑（`llm_client` 里同样处理）：表现为「网关明明起着却显示连不上」。
    """
    assert ga._trust_env_for("http://127.0.0.1:8088") is False
    assert ga._trust_env_for("http://localhost:8088") is False
    assert ga._trust_env_for("https://gw.example.edu.cn") is True


# ================================================================== 3. 探测三态
def test_probe_online(live_server: str) -> None:
    got = ga.probe_gateway(live_server)
    assert got["state"] == "online"
    assert got["reachable"] is True
    assert got["status"] == 200
    assert got["error"] is None
    assert isinstance(got["latencyMs"], int)


def test_probe_unknown_when_health_not_200(live_server: str) -> None:
    """有响应但 ``/healthz`` 非 200 → ``unknown``，**不能报成离线**。

    本地推理服务（vLLM/Ollama）就长这样：服务活着，但没有网关的健康端点。
    报"离线"会让运维去排查一个其实正常的服务。
    """
    _Handler.health_status = 404
    got = ga.probe_gateway(live_server)
    assert got["state"] == "unknown"
    assert got["reachable"] is True, "有响应就算可达"
    assert got["status"] == 404
    assert "未提供网关健康端点" in got["error"]


def test_probe_offline_when_nothing_listening() -> None:
    """没有任何服务监听 → offline（连接被拒）。"""
    got = ga.probe_gateway("http://127.0.0.1:9")
    assert got["state"] == "offline"
    assert got["reachable"] is False
    assert got["error"]


def test_probe_rejects_empty_origin() -> None:
    got = ga.probe_gateway("")
    assert got["state"] == "offline" and got["error"]


# ================================================================== 4. 总览组装
def test_overview_empty_config(monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_base_url", "")
    ga.reset_models_cache()
    data = ga.gateway_overview()
    assert data["endpoints"] == []
    assert data["gatewayCount"] == 0
    assert data["credentialFree"] is True
    assert "未配置模型接口地址" in data["hint"] or "厂商直连" in data["hint"]
    # 错误码对照表在空配置时也要给（排查不依赖是否配了网关）
    assert any(h["code"] == "model_not_allowed" for h in data["errorHints"])


def test_overview_vendor_direct(monkeypatch) -> None:
    """指向厂商 → 标注「未走网关」，且**不做健康探测**。

    厂商地址可能因未配 Key 而返回 401，那不代表离线，探测没有意义。
    """
    monkeypatch.setattr(settings, "llm_base_url", "https://api.deepseek.com/v1")
    ga.reset_models_cache()
    data = ga.gateway_overview()
    assert data["gatewayCount"] == 0
    ep = data["endpoints"][0]
    assert ep["status"] == "vendor-direct"
    assert ep["probe"] is None
    assert ep["vendorLabel"] == "DeepSeek 直连"
    assert "未经过网关" in data["hint"]


def test_overview_gateway_online(monkeypatch, live_server: str) -> None:
    monkeypatch.setattr(settings, "llm_base_url", f"{live_server}/v1")
    ga.reset_models_cache()
    data = ga.gateway_overview()
    assert data["gatewayCount"] == 1
    assert data["onlineCount"] == 1
    ep = data["endpoints"][0]
    assert ep["status"] == "online"
    assert ep["origin"] == live_server
    # 控制台入口由 origin 推导（健康检查与控制台都在源站根上，不在 /v1 下）
    assert ep["consoleUrl"] == f"{live_server}/admin-ui/"
    assert data["consoleUrl"] == f"{live_server}/admin-ui/"
    # **模型清单来自网关**，且随总览一起下发
    assert [m["id"] for m in data["models"]] == ["deepseek-flash"]
    assert data["modelsError"] is None


def test_overview_gateway_offline(monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_base_url", "http://127.0.0.1:9/v1")
    ga.reset_models_cache()
    data = ga.gateway_overview()
    assert data["gatewayCount"] == 1
    assert data["onlineCount"] == 0
    assert data["endpoints"][0]["status"] == "offline"
    assert "连不上" in data["hint"]
    # 离线时仍给出控制台地址（人工去核对时用得上）
    assert data["consoleUrl"].endswith("/admin-ui/")
    # 清单也拉不到 —— 必须如实暴露 error，而不是假装"没有模型"
    assert data["models"] == [] and data["modelsError"]


# ================================================================== 4b. 模型清单（唯一来源）
def test_list_remote_models_parses_gateway_payload(monkeypatch, live_server: str) -> None:
    """``GET {base}/models`` 的 OpenAI 形态（``{"data":[{"id",...}]}``）要解析正确。"""
    monkeypatch.setattr(settings, "llm_base_url", f"{live_server}/v1")
    ga.reset_models_cache()
    got = ga.list_remote_models()
    assert got["error"] is None and got["stale"] is False
    assert got["items"] == [{"id": "deepseek-flash", "ownedBy": "campus-ai-gateway"}]
    assert got["baseUrl"] == f"{live_server}/v1/models"


def test_list_remote_models_caches_then_refresh(monkeypatch, live_server: str) -> None:
    """默认走缓存；``refresh=True`` 绕过缓存。

    为什么缓存：清单会被「助手下拉 / 网关页 / 绑定校验」反复读，而它是跨进程 HTTP。
    但必须能强制刷新，否则网关改了白名单要等 TTL 才生效。
    """
    monkeypatch.setattr(settings, "llm_base_url", f"{live_server}/v1")
    ga.reset_models_cache()

    first = ga.list_remote_models()
    assert [m["id"] for m in first["items"]] == ["deepseek-flash"]

    # 网关侧改了清单，但未刷新 → 仍拿到缓存
    _Handler.models_body = b'{"data":[{"id":"deepseek-v4-pro"}]}'
    cached = ga.list_remote_models()
    assert [m["id"] for m in cached["items"]] == ["deepseek-flash"], "默认应走缓存"

    # 强制刷新 → 拿到新值
    fresh = ga.list_remote_models(refresh=True)
    assert [m["id"] for m in fresh["items"]] == ["deepseek-v4-pro"]


def test_list_remote_models_keeps_last_known_on_failure(monkeypatch, live_server: str) -> None:
    """拉取失败时**保留上次成功的清单**，而不是清空。

    清空会让助手下拉瞬间变空、绑定校验误判「模型不存在」——比"用稍旧的清单"糟得多。
    """
    monkeypatch.setattr(settings, "llm_base_url", f"{live_server}/v1")
    ga.reset_models_cache()
    ga.list_remote_models(refresh=True)  # 先成功一次

    # 让网关返回 500，再强制刷新
    _Handler.models_status = 500
    _Handler.models_body = None
    got = ga.list_remote_models(refresh=True)
    assert got["error"], "必须如实报告错误"
    assert [m["id"] for m in got["items"]] == ["deepseek-flash"], "应保留上次成功的清单"
    assert got["stale"] is True


def test_list_remote_models_handles_non_json(monkeypatch, live_server: str) -> None:
    """网关返回非 JSON（如反代错误页）不能抛异常，要变成可读的 error。"""
    monkeypatch.setattr(settings, "llm_base_url", f"{live_server}/v1")
    ga.reset_models_cache()
    _Handler.models_body = b"<html>Bad Gateway</html>"
    got = ga.list_remote_models()
    assert got["items"] == [] and "不是合法 JSON" in (got["error"] or "")


def test_list_remote_models_without_base_url(monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_base_url", "")
    ga.reset_models_cache()
    got = ga.list_remote_models()
    assert got["items"] == [] and "LLM_BASE_URL" in (got["error"] or "")


def test_choices_includes_default_first(monkeypatch, live_server: str) -> None:
    """助手下拉：``default`` 永远第一项，其余来自网关。"""
    monkeypatch.setattr(settings, "llm_base_url", f"{live_server}/v1")
    monkeypatch.setattr(settings, "llm_api_key", "sk-campus-x")
    ga.reset_models_cache()
    c = ga.choices()
    assert c["items"][0]["modelCode"] == "default"
    assert c["items"][0]["source"] == "env"
    assert [i["modelCode"] for i in c["items"][1:]] == ["deepseek-flash"]
    assert c["error"] is None


def test_unknown_binding_flags_model_not_in_gateway_list(monkeypatch, live_server: str) -> None:
    """不在网关清单里的编码要给出**可执行的**提示（指向网关白名单）。"""
    monkeypatch.setattr(settings, "llm_base_url", f"{live_server}/v1")
    ga.reset_models_cache()

    assert ga.unknown_binding("default") is None
    assert ga.unknown_binding("deepseek-flash") is None

    msg = ga.unknown_binding("campus-smart")
    assert msg and "deepseek-flash" in msg and "AllowedModels" in msg


def test_unknown_binding_stays_silent_when_list_unavailable(monkeypatch) -> None:
    """清单拉不到时**不判非法** —— 网关暂时不可达 ≠ 模型不存在。

    否则网关抖一下，管理页就会把所有绑定标成「模型不存在」，把运营带偏。
    """
    monkeypatch.setattr(settings, "llm_base_url", "")
    ga.reset_models_cache()
    assert ga.unknown_binding("any-model") is None


def test_unknown_binding_when_whitelist_empty(monkeypatch, live_server: str) -> None:
    """网关在线但清单为空（白名单没配）→ 提示要指向白名单，而不是说"模型不存在"。"""
    monkeypatch.setattr(settings, "llm_base_url", f"{live_server}/v1")
    ga.reset_models_cache()
    _Handler.models_body = b'{"data":[]}'
    msg = ga.unknown_binding("deepseek-flash")
    assert msg and "白名单" in msg


# ================================================================== 5. 错误码表

def test_error_hints_cover_all_gateway_codes() -> None:
    """错误码表要覆盖网关源码里会出现的全部错误码，否则运维查不到。

    对照 ``campus-ai-gateway`` 的 ``httpx.Error(...)`` 调用点整理。
    ⚠ 升级到 ``4b87b6a`` 时补入熔断（``channels_circuit_open``）、限流状态不可用
    （``enforcement_state_unavailable``）、租户停用（``tenant_disabled``）、
    模型无路由（``model_not_configured``）、上游两类（``upstream_*``）—— 这些是新版本
    **新造出来的失败模式**，缺一条就意味着那类故障只显示裸状态码。
    升级到 ``0bffdb1`` 时补入渠道出站白名单拦截（``provider_egress_denied``）。
    """
    from app.core import llm_client

    required = {
        "invalid_api_key", "api_key_expired", "ip_not_allowed",
        "model_required", "model_not_allowed", "model_not_configured",
        "tenant_disabled",
        "rate_limit_exceeded", "token_limit_exceeded", "concurrency_limit_exceeded",
        "tenant_request_quota_exceeded", "tenant_token_quota_exceeded",
        "enforcement_state_unavailable",
        "channels_circuit_open", "upstream_unavailable", "upstream_error",
        "provider_egress_denied",
    }
    missing = required - set(ga.GATEWAY_ERROR_HINTS)
    assert not missing, f"网关错误码表缺少：{sorted(missing)}"

    # 两条链路（管理页 / 用户提示）必须覆盖同一批错误码，否则同一问题两处说法不一
    shared = required & set(llm_client.GATEWAY_ERROR_HINTS)
    assert shared == required, f"llm_client 侧缺少：{sorted(required - shared)}"


def test_error_hints_point_at_pages_that_exist() -> None:
    """⚠ 提示文案不能指向已下线的页面。

    2.4 重构把「模型管理」页整个删了（模型清单的唯一来源改成网关），
    但 ``GATEWAY_ERROR_HINTS`` 里「请在管理后台『模型管理』…」留了两个月没被发现 ——
    用户照着提示找不到那个页面。这里做反向守卫。
    """
    from app.core import llm_client

    stale_pages = ("模型管理",)
    for table_name, table in (
        ("gateway_admin", ga.GATEWAY_ERROR_HINTS),
        ("llm_client", llm_client.GATEWAY_ERROR_HINTS),
    ):
        for code, hint in table.items():
            for page in stale_pages:
                assert page not in hint, (
                    f"{table_name}.{code} 的提示指向已下线页面「{page}」：{hint}"
                )
            # 网关侧的错误只能靠网关控制台 / .env 处置，不能指向能小智的后台页
            assert "网关" in hint or ".env" in hint, (
                f"{table_name}.{code} 未说明去网关侧还是 .env 处置：{hint}"
            )


def test_error_hints_are_actionable() -> None:
    """每条建议都要说「去做什么」，而不是重复错误码本身。"""
    for code, hint in ga.GATEWAY_ERROR_HINTS.items():
        assert len(hint) >= 8, f"{code} 的建议太短，可能没写清处置动作"
        assert "请" in hint or "建议" in hint, f"{code} 的建议缺少动作导向"
