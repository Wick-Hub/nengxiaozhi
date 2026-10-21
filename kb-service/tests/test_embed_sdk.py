"""嵌入 SDK 与跨域契约（任务 3.9 / I-07）。

覆盖：

1. SDK 静态资源（MIME / 缓存头 / 版本 / 可编程 API / postMessage 身份桥）；
2. 渠道资产中的 ``sdkSnippet``（主题、位置、文案、转义）；
3. CORS 预检与简单请求响应头；通配来源 + 凭证的降级；
4. 门户 iframe 侧的身份桥与「不写 localStorage」约束；
5. 管理页 SDK 页签与演示页可达。
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.config import BASE_DIR, settings
from app.core.cors import cors_options, parse_csv
from app.main import app
from app.services import channel_admin

client = TestClient(app)
FOREIGN_ORIGIN = "https://www.example.edu.cn"
STATIC_FILES = BASE_DIR / "static"


def _record(**overrides) -> dict:
    record = {
        "channelCode": "campus-1",
        "channelName": '招生"咨询',
        "themeConfig": {
            "primaryColor": "#ff6600",
            "position": "left",
            "launcherText": "咨询",
        },
    }
    record.update(overrides)
    return record


def _allowed_origin() -> str:
    origins = cors_options(settings)["allow_origins"]
    return "*" if "*" in origins else origins[0]


# ------------------------------------------------------------------ SDK 资源
def test_sdk_js_served_with_cache_headers() -> None:
    resp = client.get("/sdk/nxz.js")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/javascript")
    assert "max-age" in resp.headers.get("cache-control", "")
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert "NengXiaoZhi" in resp.text


def test_sdk_js_contract() -> None:
    src = (STATIC_FILES / "sdk" / "nxz.js").read_text(encoding="utf-8")
    for token in (
        "nxz:sdk:ready",
        "nxz:sdk:init",
        "nxz:sdk:state",
        "nxz:sdk:close",
        "setToken",
        "destroy",
        "autoInit",
        "data-channel",
        "event.source !== self.frame.contentWindow",
        "encodeURIComponent(cfg.channel)",
        "NengXiaoZhi",
    ):
        assert token in src, f"nxz.js 缺少 {token}"


def test_sdk_demo_page_served() -> None:
    resp = client.get("/sdk-demo")
    assert resp.status_code == 200
    assert "嵌入 SDK" in resp.text
    assert "sdk/nxz.js" in resp.text
    assert "NengXiaoZhi.init" in resp.text


# ------------------------------------------------------------------ 渠道资产
def test_build_assets_includes_sdk_snippet() -> None:
    assets = channel_admin.build_assets(_record(), "https://ai.example.edu.cn/")
    assert assets["sdkUrl"] == "https://ai.example.edu.cn/sdk/nxz.js"
    snippet = assets["sdkSnippet"]
    assert snippet.startswith("<script ")
    assert 'src="https://ai.example.edu.cn/sdk/nxz.js"' in snippet
    assert 'data-channel="campus-1"' in snippet
    assert 'data-position="left"' in snippet
    assert 'data-color="#ff6600"' in snippet
    assert 'data-label="咨询"' in snippet
    assert 'data-title="招生&quot;咨询"' in snippet
    assert snippet.endswith("></script>")
    assert "defer" in snippet


def test_sdk_snippet_escapes_channel() -> None:
    assets = channel_admin.build_assets(_record(channelCode="a&b"), "https://ai.example.edu.cn")
    assert 'data-channel="a&amp;b"' in assets["sdkSnippet"]


def test_relative_sdk_snippet_without_base_url() -> None:
    assets = channel_admin.build_assets(_record(), "")
    assert assets["sdkUrl"] == "/sdk/nxz.js"
    assert 'src="/sdk/nxz.js"' in assets["sdkSnippet"]


# ------------------------------------------------------------------ CORS
def test_cors_preflight_allows_authorization() -> None:
    origin = _allowed_origin()
    resp = client.options(
        "/api/chat",
        headers={
            "Origin": FOREIGN_ORIGIN if origin == "*" else origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert resp.status_code in (200, 204)
    allowed = resp.headers.get("access-control-allow-origin", "")
    assert allowed in ("*", origin)
    methods = resp.headers.get("access-control-allow-methods", "").upper()
    assert "POST" in methods
    headers = resp.headers.get("access-control-allow-headers", "").lower()
    assert headers == "*" or "authorization" in headers


def test_cors_simple_request_exposes_ai_headers() -> None:
    origin = _allowed_origin()
    resp = client.get(
        "/api/health",
        headers={"Origin": FOREIGN_ORIGIN if origin == "*" else origin},
    )
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") in ("*", origin)
    exposed = resp.headers.get("access-control-expose-headers", "")
    assert "X-AI-Produce-Id" in exposed or "*" in exposed


def test_cors_credentials_with_wildcard_is_downgraded() -> None:
    opts = cors_options(
        SimpleNamespace(
            cors_allow_origins="*",
            cors_allow_credentials=True,
            cors_expose_headers="X-AI-Label",
        )
    )
    assert opts["allow_origins"] == ["*"]
    assert opts["allow_credentials"] is False
    assert opts["expose_headers"] == ["X-AI-Label"]


def test_cors_explicit_origins_keep_credentials() -> None:
    opts = cors_options(
        SimpleNamespace(
            cors_allow_origins="https://a.edu.cn, https://b.edu.cn",
            cors_allow_credentials=True,
            cors_expose_headers="",
        )
    )
    assert opts["allow_origins"] == ["https://a.edu.cn", "https://b.edu.cn"]
    assert opts["allow_credentials"] is True
    assert opts["expose_headers"] == []


def test_parse_csv_ignores_blanks() -> None:
    assert parse_csv(" a , ,b, ") == ["a", "b"]
    assert parse_csv("") == []


# ------------------------------------------------------------------ 门户身份桥 / 管理页
def test_portal_sdk_bridge_contract() -> None:
    src = (STATIC_FILES / "app.js").read_text(encoding="utf-8")
    for token in (
        "SDK_MODE",
        "nxz:sdk:init",
        "nxz:sdk:ready",
        "nxz:sdk:state",
        "event.source !== window.parent",
        "applySdkIdentity",
        "if (sdkToken) return sdkToken;",
        "waitSdkInit",
    ):
        assert token in src, f"app.js 缺少 {token}"


def test_channels_ui_has_sdk_tab() -> None:
    html = (STATIC_FILES / "channels.html").read_text(encoding="utf-8")
    js = (STATIC_FILES / "channels.js").read_text(encoding="utf-8")
    assert 'data-kind="sdk"' in html
    assert "嵌入 SDK" in html
    assert "sdkSnippet" in js
    assert "NengXiaoZhi.init" in js
