"""FastAPI dependencies."""
from __future__ import annotations

from typing import Optional

from fastapi import Cookie, HTTPException

from .core.security import COOKIE_NAME, decode_token
from .services.users import get_user, get_min_iat


def _user_for_token(token: Optional[str]) -> Optional[dict]:
    if not token:
        return None
    payload = decode_token(token)
    if not payload:
        return None
    user = get_user(payload["sub"])
    if not user:
        return None
    # Server-side revocation: tokens issued before user.min_iat are invalid.
    min_iat = get_min_iat(user["id"])
    if min_iat and int(payload.get("iat", 0)) < min_iat:
        return None
    return user


def current_user(r2d2_session: Optional[str] = Cookie(default=None, alias=COOKIE_NAME)) -> dict:
    user = _user_for_token(r2d2_session)
    if not user:
        raise HTTPException(401, "not authenticated")
    return user


def optional_user(r2d2_session: Optional[str] = Cookie(default=None, alias=COOKIE_NAME)) -> Optional[dict]:
    return _user_for_token(r2d2_session)
