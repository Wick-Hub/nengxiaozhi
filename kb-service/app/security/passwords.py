"""管理账号密码哈希（任务 2.9）。

标准库 PBKDF2-HMAC-SHA256，零新增依赖。存储格式::

    pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>

盐为 16 字节随机值；校验用 :func:`hmac.compare_digest` 常量时间比较。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 260_000
SALT_BYTES = 16


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def hash_password(password: str, *, iterations: int = ITERATIONS) -> str:
    if not password:
        raise ValueError("密码不能为空")
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{ALGORITHM}${iterations}${_b64e(salt)}${_b64e(digest)}"


def verify_password(password: str, stored: str | None) -> bool:
    """校验密码；``stored`` 为空表示该账号沿用免密演示登录。"""
    if not stored:
        return False
    try:
        algo, iterations, salt_b64, hash_b64 = stored.split("$", 3)
        if algo != ALGORITHM:
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            (password or "").encode("utf-8"),
            _b64d(salt_b64),
            int(iterations),
        )
        return hmac.compare_digest(_b64e(digest), hash_b64)
    except (ValueError, TypeError):
        return False
