"""AES-256-GCM 加解密：数据库密码、LLM API Key 等敏感信息落库前必须加密。

密文格式: base64(nonce[12] + ciphertext + tag[16])
AAD 绑定记录类型，防止跨记录挪用密文。
"""
from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import settings

_PREFIX = "enc:v1:"


def _key() -> bytes:
    return settings.encryption_key_bytes


def encrypt(plaintext: str, aad: str = "") -> str:
    if plaintext is None:
        return ""
    if plaintext == "":
        return ""
    if plaintext.startswith(_PREFIX):
        return plaintext  # 已加密，幂等
    aes = AESGCM(_key())
    nonce = os.urandom(12)
    ct = aes.encrypt(nonce, plaintext.encode("utf-8"), aad.encode("utf-8") or None)
    return _PREFIX + base64.b64encode(nonce + ct).decode("ascii")


def decrypt(cipher: str, aad: str = "") -> str:
    if not cipher:
        return ""
    if not cipher.startswith(_PREFIX):
        return cipher  # 兼容历史明文（迁移期）
    try:
        blob = base64.b64decode(cipher[len(_PREFIX):])
        nonce, ct = blob[:12], blob[12:]
        aes = AESGCM(_key())
        pt = aes.decrypt(nonce, ct, aad.encode("utf-8") or None)
        return pt.decode("utf-8")
    except Exception:
        raise ValueError("解密失败：密钥不匹配或密文损坏")


def mask(cipher_or_plain: str) -> str:
    """对外展示：仅返回掩码。"""
    return "***" if cipher_or_plain else ""
