"""统一身份认证（任务 4.1）：CAS 适配器、mock 全链路与安全边界。"""

from __future__ import annotations

from urllib.parse import parse_qs, quote, urlsplit

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.security import auth
from app.services import sso

client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset_sso():
    sso.reset_for_tests()
    yield
    sso.reset_for_tests()


# ---------------------------------------------------------------- 基础工具


@pytest.mark.parametrize("value", ["/admin", "/unrecognized?x=1", "/portal?channel=a"])
def test_safe_redirect_keeps_site_paths(value: str) -> None:
    assert sso.safe_redirect(value) == value


@pytest.mark.parametrize(
    "value",
    ["", None, "//evil.com", "http://evil.com", "https://evil.com/x", "/\\evil", "/a\nb"],
)
def test_safe_redirect_rejects_external(value: str | None) -> None:
    assert sso.safe_redirect(value) == "/"


def test_parse_service_response_with_namespace() -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <cas:serviceResponse xmlns:cas="http://www.yale.edu/tp/cas">
      <cas:authenticationSuccess>
        <cas:user>20230001</cas:user>
        <cas:attributes>
          <cas:name>张三</cas:name>
          <cas:userType>student</cas:userType>
        </cas:attributes>
      </cas:authenticationSuccess>
    </cas:serviceResponse>"""
    parsed = sso.parse_service_response(xml)
    assert parsed["userNo"] == "20230001"
    assert parsed["name"] == "张三"
    assert parsed["roleHint"] == "student"


def test_parse_service_response_default_namespace() -> None:
    xml = """<serviceResponse xmlns="http://www.yale.edu/tp/cas">
      <authenticationSuccess><user>T10001</user></authenticationSuccess>
    </serviceResponse>"""
    assert sso.parse_service_response(xml)["userNo"] == "T10001"


def test_parse_service_response_failure() -> None:
    xml = """<cas:serviceResponse xmlns:cas="http://www.yale.edu/tp/cas">
      <cas:authenticationFailure code="INVALID_TICKET">ticket 无效</cas:authenticationFailure>
    </cas:serviceResponse>"""
    with pytest.raises(sso.SsoError, match="ticket 无效"):
        sso.parse_service_response(xml)


def test_parse_service_response_missing_user_and_bad_xml() -> None:
    with pytest.raises(sso.SsoError, match="缺少学工号"):
        sso.parse_service_response(
            '<serviceResponse><authenticationSuccess><attributes/></authenticationSuccess></serviceResponse>'
        )
    with pytest.raises(sso.SsoError, match="XML"):
        sso.parse_service_response("<not-xml")


def test_map_role_hint() -> None:
    assert sso.map_role_hint("student") == "本科生"
    assert sso.map_role_hint("teacher") == "教职工"
    assert sso.map_role_hint("本科生") == "本科生"
    assert sso.map_role_hint("unknown") == ""


# ---------------------------------------------------------------- mock 全链路


def _mock_login_flow(redirect: str = "/admin", user_no: str = "20230001", role: str = ""):
    """走完 CAS 登录 → 回调，返回（回调地址, 交换码）。"""
    entry = client.get(
        "/api/auth/cas/login", params={"redirect": redirect}, follow_redirects=False
    )
    assert entry.status_code == 302
    mock_url = entry.headers["location"]
    assert mock_url.startswith("/api/auth/cas/mock/login?")

    issued = client.get(
        f"{mock_url}&userNo={quote(user_no)}&role={quote(role)}", follow_redirects=False
    )
    assert issued.status_code == 302
    callback_url = issued.headers["location"]

    callback = client.get(callback_url, follow_redirects=False)
    assert callback.status_code == 302
    location = callback.headers["location"]
    assert location.startswith(redirect + "#cas=")
    return callback_url, location.split("#cas=", 1)[1]


def test_mock_cas_full_flow_exchanges_token() -> None:
    _, code = _mock_login_flow()
    resp = client.post("/api/auth/cas/exchange", json={"code": code})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["userNo"] == "20230001"
    assert data["role"] == "本科生"
    claims = auth.decode_token(data["token"])
    assert claims["sub"] == "20230001"
    assert claims["role"] == "本科生"

    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {data['token']}"})
    assert me.json()["data"]["authenticated"] is True
    assert me.json()["data"]["ssoEnabled"] is True
    assert me.json()["data"]["ssoProvider"] == "mock-cas"


def test_exchange_code_is_single_use() -> None:
    _, code = _mock_login_flow("/")
    assert client.post("/api/auth/cas/exchange", json={"code": code}).status_code == 200
    again = client.post("/api/auth/cas/exchange", json={"code": code})
    assert again.status_code == 401
    assert "已使用" in again.json()["error"]


def test_exchange_rejects_wrong_purpose_and_bad_code() -> None:
    other = auth.create_token("20230001", "本科生", extra={"purpose": "other", "jti": "x"})
    assert client.post("/api/auth/cas/exchange", json={"code": other}).status_code == 401
    assert client.post("/api/auth/cas/exchange", json={"code": "not-a-token-xx"}).status_code == 401


def test_mock_role_hint_maps_to_local_role() -> None:
    _, code = _mock_login_flow("/", user_no="20990001", role="teacher")
    data = client.post("/api/auth/cas/exchange", json={"code": code}).json()["data"]
    assert data["role"] == "教职工"
    assert data["isAdmin"] is False


def test_invalid_ticket_redirects_with_error() -> None:
    resp = client.get(
        "/api/auth/cas/callback",
        params={"ticket": "ST-bogus", "redirect": "/admin"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/admin#cas_error=")


def test_ticket_is_bound_to_service() -> None:
    ticket = sso.issue_mock_ticket("20230001", "本科生", service="http://127.0.0.1:8000/a")
    with pytest.raises(sso.SsoError, match="service"):
        sso.validate_ticket(ticket, "http://127.0.0.1:8000/b")


def test_open_redirect_cannot_escape_site() -> None:
    entry = client.get(
        "/api/auth/cas/login", params={"redirect": "//evil.com/x"}, follow_redirects=False
    )
    service = parse_qs(urlsplit(entry.headers["location"]).query)["service"][0]
    assert parse_qs(urlsplit(service).query)["redirect"][0] == "/"


def test_mock_login_rejects_external_service() -> None:
    resp = client.get(
        "/api/auth/cas/mock/login",
        params={"service": "http://evil.com/steal", "userNo": "20230001"},
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_mock_login_disabled_when_real_cas_configured(monkeypatch) -> None:
    monkeypatch.setattr(settings, "cas_server_url", "https://cas.example.edu")
    resp = client.get(
        "/api/auth/cas/mock/login",
        params={"service": "http://127.0.0.1:8000/api/auth/cas/callback"},
        follow_redirects=False,
    )
    assert resp.status_code == 404
    me = client.get("/api/auth/me").json()["data"]
    assert me["ssoProvider"] == "cas"


def test_login_url_uses_configured_cas(monkeypatch) -> None:
    monkeypatch.setattr(settings, "cas_server_url", "https://cas.example.edu/")
    url = sso.build_login_url("http://app/api/auth/cas/callback")
    assert url.startswith("https://cas.example.edu/cas/login?service=")
