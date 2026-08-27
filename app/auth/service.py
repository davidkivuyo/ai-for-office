from __future__ import annotations

from datetime import datetime, timedelta, timezone

import bcrypt
from jose import JWTError, jwt

from app.config import get_settings


def hash_password(password: str) -> str:
    # bcrypt.gensalt() uses cost 12 by default; hash is $2b$... (60 chars)
    # Truncate to 72 bytes to match bcrypt limit (passlib behavior)
    pw_bytes = password.encode("utf-8")[:72]
    hashed = bcrypt.hashpw(pw_bytes, bcrypt.gensalt())
    return hashed.decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8")[:72], hashed.encode("utf-8"))
    except ValueError:
        return False


def create_access_token(*, subject: str, expires_minutes: int | None = None) -> str:
    settings = get_settings()
    delta = timedelta(minutes=expires_minutes if expires_minutes is not None else settings.access_token_expire_minutes)
    expire = datetime.now(timezone.utc) + delta
    payload = {"sub": subject, "exp": expire}
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> str | None:
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])
        sub: str | None = payload.get("sub")
        return sub
    except JWTError:
        return None
