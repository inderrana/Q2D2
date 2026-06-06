"""Security headers + CSRF + rate limiting middleware (no extra deps)."""
from __future__ import annotations

import os
import time
from collections import defaultdict, deque
from typing import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------

# Vendored libs only — no remote scripts. fonts.googleapis.com kept allowed for
# optional Google Font URLs declared in branding.toml.
_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com data:; "
    "img-src 'self' data: blob:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        h = response.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        h.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        if os.getenv("COOKIE_SECURE", "0") == "1":
            h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        # CSP only on HTML responses to avoid breaking inline-style consumers
        ctype = h.get("content-type", "")
        if ctype.startswith("text/html"):
            h.setdefault("Content-Security-Policy", _CSP)
        return response


# ---------------------------------------------------------------------------
# Lightweight in-process rate limiter (per IP + path-bucket)
# ---------------------------------------------------------------------------

_BUCKETS: dict[str, deque] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# (path-prefix, methods, max_per_minute)
_RULES: list[tuple[str, set[str], int]] = [
    ("/api/auth/login",       {"POST"}, 10),
    ("/api/auth/register",    {"POST"}, 5),
    ("/api/chat",             {"POST"}, 60),
    ("/api/documents/upload", {"POST"}, 30),
    ("/api/documents/url",    {"POST"}, 20),
]


def _rule_for(request: Request):
    path = request.url.path
    for prefix, methods, limit in _RULES:
        if request.method in methods and path.startswith(prefix):
            return prefix, limit
    return None


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        rule = _rule_for(request)
        if rule is not None:
            prefix, limit = rule
            ip = _client_ip(request)
            key = f"{ip}:{prefix}"
            now = time.monotonic()
            window = 60.0
            q = _BUCKETS[key]
            while q and now - q[0] > window:
                q.popleft()
            if len(q) >= limit:
                retry = max(1, int(window - (now - q[0])))
                return JSONResponse(
                    {"detail": "rate limit exceeded"},
                    status_code=429,
                    headers={"Retry-After": str(retry)},
                )
            q.append(now)
        return await call_next(request)


# ---------------------------------------------------------------------------
# CSRF: require a custom header on state-changing same-origin requests.
# Same-origin AJAX in app.js sends X-Requested-With automatically (we add it).
# Cross-origin "simple" multipart/form-data POSTs from a malicious page cannot
# set a custom header without preflight, which is the actual mitigation here.
# ---------------------------------------------------------------------------

_CSRF_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_CSRF_EXEMPT = (
    "/api/auth/login",
    "/api/auth/register",
    "/api/auth/logout",
)


class CSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method in _CSRF_METHODS and request.url.path.startswith("/api/"):
            if not any(request.url.path.startswith(p) for p in _CSRF_EXEMPT):
                if request.headers.get("x-requested-with") != "fetch":
                    return JSONResponse(
                        {"detail": "missing X-Requested-With header"},
                        status_code=403,
                    )
        return await call_next(request)


def install(app: FastAPI) -> None:
    # Order: rate-limit -> CSRF -> headers (innermost first when adding)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(RateLimitMiddleware)
