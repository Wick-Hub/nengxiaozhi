"""字段级加密（任务 1.12）：AES-256-GCM。

用于 ``sys_user.id_card_enc`` / ``phone_enc`` 等敏感列的读写。
原型 ``kb-engine`` **没有任何加密实现**，本模块为新增能力。

设计要点：

- **AEAD**：AES-GCM，认证标签随密文一起存储，密文被篡改可检出。
- **密钥来源** ``settings.aes_key``：支持 base64（32 字节）或任意口令，
  统一经 SHA-256 派生 32 字节密钥，避免长度不合规导致运行时异常。
- 为空时退化为**进程内临时密钥**并告警（与 :mod:`app.security.auth` 的 JWT 策略一致）；
  此时**无法解密历史密文**，生产必须显式配置 ``AES_KEY``。
- **存储格式**：``b"v1" + nonce(12) + ciphertext+tag``，带版本前缀便于将来轮换算法。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import settings

logger = logging.getLogger(__name__)

VERSION = b"v1"
NONCE_LEN = 12
KEY_LEN = 32

_ephemeral_key: bytes | None = None


class CryptoError(Exception):
    """密文无法解密（格式不符 / 密钥不匹配 / 被篡改）。"""


def _key() -> bytes:
    global _ephemeral_key
    raw = (settings.aes_key or "").strip()
    if raw:
        try:
            decoded = base64.b64decode(raw, validate=True)
            if len(decoded) == KEY_LEN:
                return decoded
        except (binascii.Error, ValueError):
            pass
        return hashlib.sha256(raw.encode("utf-8")).digest()

    if _ephemeral_key is None:
        _ephemeral_key = os.urandom(KEY_LEN)
        logger.warning("AES_KEY 未配置，已生成进程内临时密钥（重启后无法解密历史密文）")
    return _ephemeral_key


def encrypt_field(plain: str | None) -> bytes | None:
    """加密单个字段；``None`` / 空串返回 ``None``（列可空，不写空密文）。"""
    if plain is None or plain == "":
        return None
    nonce = os.urandom(NONCE_LEN)
    ciphertext = AESGCM(_key()).encrypt(nonce, plain.encode("utf-8"), None)
    return VERSION + nonce + ciphertext


def decrypt_field(blob: bytes | bytearray | memoryview | None) -> str | None:
    """解密字段；空值返回 ``None``。密文异常统一抛 :class:`CryptoError`。"""
    if not blob:
        return None
    data = bytes(blob)
    if not data.startswith(VERSION):
        raise CryptoError("密文格式不支持（缺少版本前缀）")
    body = data[len(VERSION) :]
    nonce, ciphertext = body[:NONCE_LEN], body[NONCE_LEN:]
    if len(nonce) != NONCE_LEN or not ciphertext:
        raise CryptoError("密文长度不足")
    try:
        return AESGCM(_key()).decrypt(nonce, ciphertext, None).decode("utf-8")
    except Exception as exc:  # noqa: BLE001 - cryptography 的异常类型不稳定
        raise CryptoError(f"解密失败（密钥不匹配或密文被篡改）：{exc}") from exc


def encrypt_optional(value: str | None) -> bytes | None:
    """语义别名：明确「列可空」的调用点。"""
    return encrypt_field(value)


def is_encrypted(value: bytes | None) -> bool:
    """判断某个 BLOB 是否是本模块产出的密文。"""
    return bool(value) and bytes(value).startswith(VERSION)
