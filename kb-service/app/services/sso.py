"""统一身份认证（任务 4.1）：CAS 适配器 + 内置 mock CAS。

外部 CAS 未就绪时（``CAS_SERVER_URL`` 为空）启用内置 mock：同一套
「登录 → 回调带 ticket → serviceValidate → 换发本地 JWT」流程，仅把
CAS 服务端换成进程内实现，便于前端联调与自动化验收。

安全要点：

- 回调地址只接受站内相对路径，拒绝 ``//evil`` / 绝对 URL，避免开放重定向。
- ticket 一次性消费（mock 存内存并校验 service），过短的重放窗口。
- 浏览器只拿到**一次性的短时交换码**（JWT，2 分钟、``purpose=cas``），
  再由前端 POST 换正式 token；正式 token 不进 URL/Referer。
- 交换码带 ``jti``，内存记录已用集合防重放（单进程部署；多 worker 需换 Redis）。
- 角色默认以本地 ``sys_user`` 绑定为准；``CAS_TRUST_ROLE_ATTRIBUTE`` 关闭时
  上游角色属性不参与提权。
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlencode, urlsplit

import httpx

from app.config import settings
from app.security import rbac
from app.security.auth import TokenError, create_token, decode_token

logger = logging.getLogger(__name__)

MOCK_TICKET_TTL = 120.0
EXCHANGE_TTL_MINUTES = 2
#: mock 演示身份（未在 sys_user 登记时也能走通）；真实 CAS 返回什么由上游决定
MOCK_USERS: dict[str, tuple[str, str]] = {
    "20230001": ("本科生演示账号", "本科生"),
    "20240001": ("研究生演示账号", "研究生"),
    "T10001": ("教职工演示账号", "教职工"),
    "admin": ("系统管理员", "管理员"),
}


class SsoError(Exception):
    """CAS 流程失败（ticket 无效、上游不可达、响应格式不符）。"""


_lock = threading.Lock()
_mock_tickets: dict[str, dict] = {}
_used_codes: dict[str, float] = {}


def mock_mode() -> bool:
    return not settings.cas_server_url.strip()


def enabled() -> bool:
    """CAS 是否对前端开放（外部地址或 mock 均可）。"""
    return True


# ---------------------------------------------------------------- 地址与重定向


def safe_redirect(value: str | None, default: str = "/") -> str:
    """只允许站内相对路径，避免 ``?redirect=//evil.com`` 式开放重定向。"""
    raw = (value or "").strip()
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return default
    if "\\" in raw or "\r" in raw or "\n" in raw:
        return default
    parts = urlsplit(raw)
    if parts.scheme or parts.netloc:
        return default
    return raw


def _service_base(request_base: str) -> str:
    base = settings.cas_service_base or settings.public_base_url or request_base
    return (base or "").rstrip("/")


def build_service_url(request_base: str, redirect: str) -> str:
    query = urlencode({"redirect": redirect})
    return f"{_service_base(request_base)}/api/auth/cas/callback?{query}"


def build_login_url(service: str) -> str:
    if mock_mode():
        return "/api/auth/cas/mock/login?" + urlencode({"service": service})
    base = settings.cas_server_url.rstrip("/")
    return f"{base}{settings.cas_login_path}?" + urlencode({"service": service})


# ---------------------------------------------------------------- mock CAS


def issue_mock_ticket(user_no: str, role: str = "", name: str = "", service: str = "") -> str:
    user_no = (user_no or "").strip()
    if not user_no:
        raise SsoError("mock 登录需要 userNo")
    ticket = "ST-" + secrets.token_urlsafe(18)
    now = time.time()
    with _lock:
        for key, item in list(_mock_tickets.items()):
            if item["expires"] < now:
                _mock_tickets.pop(key, None)
        _mock_tickets[ticket] = {
            "userNo": user_no,
            "role": role,
            "name": name,
            "service": service,
            "expires": now + MOCK_TICKET_TTL,
        }
    return ticket


def _consume_mock_ticket(ticket: str, service: str) -> dict:
    with _lock:
        item = _mock_tickets.pop(ticket, None)
    if item is None:
        raise SsoError("ticket 无效或已使用")
    if item["expires"] < time.time():
        raise SsoError("ticket 已过期")
    if item["service"] and service and item["service"] != service:
        raise SsoError("ticket 与 service 不匹配")
    return item


# ---------------------------------------------------------------- CAS XML


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_service_response(xml_text: str) -> dict:
    """解析 CAS 2.0/3.0 ``serviceValidate`` 响应。

    返回 ``{userNo, name, roleHint, attributes}``；失败抛 :class:`SsoError`。
    命名空间按 local-name 处理，兼容 ``cas:`` / 默认命名空间两种写法。
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise SsoError("CAS 返回内容不是合法 XML") from exc

    success = None
    failure = None
    for node in root.iter():
        name = _local(node.tag)
        if name == "authenticationSuccess":
            success = node
        elif name == "authenticationFailure":
            failure = node
    if success is None:
        message = "CAS 认证失败"
        if failure is not None:
            message = (failure.text or "").strip() or failure.get("code") or message
        raise SsoError(message)

    user_no = ""
    attrs: dict[str, str] = {}
    for node in success.iter():
        name = _local(node.tag)
        if name == "user" and node.text and not user_no:
            user_no = node.text.strip()
        elif name == "attributes":
            for child in list(node):
                key = _local(child.tag)
                if child.text and child.text.strip():
                    attrs.setdefault(key, child.text.strip())
    if not user_no:
        user_no = attrs.get("userNo") or attrs.get("uid") or attrs.get("account") or ""
    if not user_no:
        raise SsoError("CAS 响应缺少学工号")
    role_hint = attrs.get("role") or attrs.get("roleName") or attrs.get("userType") or ""
    return {
        "userNo": user_no,
        "name": attrs.get("name") or attrs.get("realName") or "",
        "roleHint": role_hint,
        "attributes": attrs,
    }


def validate_ticket(ticket: str, service: str, request_base: str = "") -> dict:
    """校验 ticket 并返回身份属性。"""
    ticket = (ticket or "").strip()
    if not ticket:
        raise SsoError("缺少 ticket")
    if mock_mode():
        item = _consume_mock_ticket(ticket, service)
        return {
            "userNo": item["userNo"],
            "name": item["name"],
            "roleHint": item["role"],
            "attributes": {"mock": "1"},
        }

    base = settings.cas_server_url.rstrip("/")
    url = f"{base}{settings.cas_validate_path}"
    try:
        with httpx.Client(timeout=settings.cas_timeout, trust_env=False) as cli:
            resp = cli.get(url, params={"service": service, "ticket": ticket})
    except httpx.HTTPError as exc:
        raise SsoError(f"CAS 服务不可达：{exc}") from exc
    if resp.status_code != 200:
        raise SsoError(f"CAS 校验失败（HTTP {resp.status_code}）")
    return parse_service_response(resp.text)


# ---------------------------------------------------------------- 角色映射


def map_role_hint(hint: str) -> str:
    hint = (hint or "").strip()
    if not hint:
        return ""
    if rbac.is_valid_role(hint):
        return hint
    table: dict[str, str] = {}
    for pair in settings.cas_role_map.split(","):
        code, _, name = pair.partition(":")
        if code.strip() and name.strip():
            table[code.strip().lower()] = name.strip()
    return table.get(hint.lower(), "")


# ---------------------------------------------------------------- 一次性交换码


def issue_exchange_code(identity: dict) -> str:
    return create_token(
        identity["userNo"],
        identity["role"],
        user_id=identity.get("userId"),
        expires_minutes=EXCHANGE_TTL_MINUTES,
        extra={
            "purpose": "cas",
            "jti": secrets.token_urlsafe(16),
            "rid": identity.get("roleId"),
            "mgr": bool(identity.get("manager")),
        },
    )


def consume_exchange_code(code: str) -> dict:
    """校验交换码并标记已用；返回 payload。"""
    try:
        payload = decode_token((code or "").strip())
    except TokenError as exc:
        raise SsoError("登录凭证无效或已过期，请重新登录") from exc
    if payload.get("purpose") != "cas":
        raise SsoError("登录凭证类型不正确")
    jti = str(payload.get("jti") or "")
    if not jti:
        raise SsoError("登录凭证缺少标识")
    now = time.time()
    with _lock:
        for key, used_at in list(_used_codes.items()):
            if now - used_at > EXCHANGE_TTL_MINUTES * 60:
                _used_codes.pop(key, None)
        if jti in _used_codes:
            raise SsoError("登录凭证已使用，请重新登录")
        _used_codes[jti] = now
    return payload


def reset_for_tests() -> None:
    with _lock:
        _mock_tickets.clear()
        _used_codes.clear()
