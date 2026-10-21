"""认证：JWT（任务 1.13）。

原型 ``kb-engine`` **没有任何鉴权**（``server.js`` 里没有 token / Authorization 逻辑），
所以本模块是**新增能力**而非移植，没有 Node 侧可做一致性回归。

实现要点：

- **HS256 自实现**（``hmac`` + ``hashlib``），零第三方依赖；``cryptography`` 留给 1.12 的 AES。
- 密钥取 ``settings.jwt_secret``；为空时退化为**进程内临时密钥**（重启即失效）并告警，
  生产必须显式配置。
- 标准声明 ``sub``（学工号）/ ``role`` / ``iat`` / ``exp``；可选 ``uid``。
- 签名校验用 :func:`hmac.compare_digest` 做**常量时间**比较，避免时序侧信道。
- 本模块只负责**签发与解析**；「无 token → 访客」的策略在 :mod:`app.security.deps`。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time

from app.config import settings

logger = logging.getLogger(__name__)

ALGORITHM = "HS256"
DEFAULT_TTL_MINUTES = 720

_ephemeral_secret: str | None = None


class TokenError(Exception):
    """JWT 无效：格式错误 / 签名不符 / 已过期。"""


def _secret() -> str:
    global _ephemeral_secret
    if settings.jwt_secret:
        return settings.jwt_secret
    if _ephemeral_secret is None:
        _ephemeral_secret = secrets.token_urlsafe(32)
        logger.warning("JWT_SECRET 未配置，已生成进程内临时密钥（重启后旧 token 全部失效）")
    return _ephemeral_secret


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(message: str) -> str:
    mac = hmac.new(_secret().encode("utf-8"), message.encode("ascii"), hashlib.sha256)
    return _b64e(mac.digest())


def create_token(
    subject: str,
    role: str,
    *,
    user_id: int | None = None,
    expires_minutes: int | None = None,
    extra: dict | None = None,
) -> str:
    """签发 HS256 JWT。``subject`` 为学工号（``sys_user.user_no``）。"""
    ttl = expires_minutes or settings.jwt_expire_minutes or DEFAULT_TTL_MINUTES
    now = int(time.time())
    payload: dict = {"sub": subject, "role": role, "iat": now, "exp": now + ttl * 60}
    if user_id is not None:
        payload["uid"] = user_id
    if extra:
        payload.update(extra)

    header = {"alg": ALGORITHM, "typ": "JWT"}
    signing_input = (
        f"{_b64e(json.dumps(header, separators=(',', ':')).encode())}."
        f"{_b64e(json.dumps(payload, separators=(',', ':'), ensure_ascii=False).encode())}"
    )
    return f"{signing_input}.{_sign(signing_input)}"


def decode_token(token: str) -> dict:
    """校验签名与 ``exp``，返回 payload；任何异常统一抛 :class:`TokenError`。"""
    if not token or token.count(".") != 2:
        raise TokenError("token 格式不正确")

    header_b64, payload_b64, signature = token.split(".")
    if not hmac.compare_digest(_sign(f"{header_b64}.{payload_b64}"), signature):
        raise TokenError("签名校验失败")

    try:
        header = json.loads(_b64d(header_b64))
        payload = json.loads(_b64d(payload_b64))
    except (ValueError, TypeError) as exc:
        raise TokenError(f"token 内容无法解析：{exc}") from exc

    if header.get("alg") != ALGORITHM:
        raise TokenError(f"不支持的算法：{header.get('alg')}")

    exp = payload.get("exp")
    if not isinstance(exp, int):
        raise TokenError("token 缺少 exp")
    if exp < int(time.time()):
        raise TokenError("token 已过期")

    return payload
