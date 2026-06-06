"""Password hashing + JWT session tokens."""
from __future__ import annotations

import os
import time
import uuid
from typing import Optional

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Request, Response

from ..config import JWT_ALG, JWT_ISSUER, JWT_SECRET, JWT_TTL_DAYS


COOKIE_NAME = "r2d2_session"

_ph = PasswordHasher()


def hash_password(p: str) -> str:
    return _ph.hash(p)


def verify_password(p: str, hashed: str) -> bool:
    try:
        _ph.verify(hashed, p)
        return True
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def make_token(user_id: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "sub": user_id,
            "iat": now,
            "exp": now + JWT_TTL_DAYS * 86400,
            "iss": JWT_ISSUER,
            "jti": uuid.uuid4().hex,
        },
        JWT_SECRET, algorithm=JWT_ALG,
    )


def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(
            token, JWT_SECRET, algorithms=[JWT_ALG],
            options={"require": ["exp", "iat", "sub"]},
            issuer=JWT_ISSUER,
        )
    except Exception:
        return None


def _cookie_secure(request: Optional[Request] = None) -> bool:
    """Determine if cookies should have Secure flag.
    
    - For https:// connections: always secure
    - For http:// on tunnel: insecure (X-Forwarded-Proto can indicate https upstream)  
    """
    env = os.getenv("COOKIE_SECURE", "1")
    if env == "0":
        return False
    if env != "1":
        return True  # Explicit "1"
    
    # If connection is HTTPS, always secure
    if request:
        scheme = request.headers.get("X-Forwarded-Proto", request.url.scheme).lower()
        if scheme == "https":
            return True
        # If we detect HTTP from tunnel (inderrana.com), don't require secure
        host = request.headers.get("host", "").lower()
        if "inderrana.com" in host:
            return False
    
    return True


def _get_cookie_domain(request: Optional[Request]) -> Optional[str]:
    """Determine cookie domain based on request host.
    
    - For inderrana.com subdomains: use .inderrana.com (wildcard)
    - For localhost: use None (host-only, works for that specific host)
    """
    if not request:
        return None
    
    host = request.headers.get("host", "").split(":")[0].lower()
    
    if "inderrana.com" in host:
        return ".inderrana.com"  # Wildcard: works for q2d2.inderrana.com, *.inderrana.com
    
    # For localhost or other domains, use host-only (None)
    return None


def set_session_cookie(resp: Response, token: str, request: Optional[Request] = None) -> None:
    domain = _get_cookie_domain(request)
    secure = _cookie_secure(request)
    resp.set_cookie(
        COOKIE_NAME, token,
        max_age=JWT_TTL_DAYS * 86400,
        httponly=True, samesite="none", secure=secure, path="/",
        domain=domain,
    )


def clear_session_cookie(resp: Response, request: Optional[Request] = None) -> None:
    domain = _get_cookie_domain(request)
    secure = _cookie_secure(request)
    resp.delete_cookie(COOKIE_NAME, path="/", secure=secure, samesite="none", domain=domain)
