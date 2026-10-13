"""认证：JWT 签发校验 + argon2 密码哈希。"""
from __future__ import annotations

import time
from datetime import UTC, datetime

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.core.config import settings

_ph = PasswordHasher()

ALGO = "HS256"


def hash_password(plain: str) -> str:
    return _ph.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _ph.verify(hashed, plain)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def create_token(user_id: str, username: str, role: str) -> str:
    now = int(time.time())
    payload = {
        "sub": user_id,
        "username": username,
        "role": role,
        "iat": now,
        "exp": now + settings.jwt_expire_hours * 3600,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGO)


def decode_token(token: str) -> dict | None:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[ALGO])
        return payload if payload.get("sub") else None
    except jwt.PyJWTError:
        return None


def now_utc() -> datetime:
    return datetime.now(UTC)
