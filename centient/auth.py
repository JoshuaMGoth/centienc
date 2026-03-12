"""CentienC — Authentication helpers (bcrypt + JWT)."""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt

logger = logging.getLogger("centient.auth")

# JWT secret — generated once per installation and stored in settings
_jwt_secret: str | None = None
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = 24


def get_jwt_secret() -> str:
    """Return (and cache) the JWT secret. Generate one if missing."""
    global _jwt_secret
    if _jwt_secret:
        return _jwt_secret
    # Try env, fall back to random generation
    _jwt_secret = os.environ.get("CENTIENT_JWT_SECRET", secrets.token_hex(32))
    return _jwt_secret


def set_jwt_secret(secret: str) -> None:
    global _jwt_secret
    _jwt_secret = secret


def hash_password(password: str) -> str:
    """Hash a password with bcrypt."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    """Verify a password against its bcrypt hash."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def create_token(user_id: int, username: str, role: str = "admin") -> str:
    """Create a JWT token for the given user."""
    payload = {
        "sub": user_id,
        "username": username,
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_HOURS),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, get_jwt_secret(), algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict[str, Any] | None:
    """Decode and validate a JWT token. Returns None if invalid."""
    try:
        return jwt.decode(token, get_jwt_secret(), algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        logger.debug("Token expired")
        return None
    except jwt.InvalidTokenError:
        logger.debug("Invalid token")
        return None
