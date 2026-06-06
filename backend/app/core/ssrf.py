"""SSRF guard for fetch-by-URL endpoints (e.g. POST /api/documents/url)."""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from fastapi import HTTPException


_ALLOWED_SCHEMES = {"http", "https"}


def _is_private_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # err on the side of caution
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def assert_safe_url(url: str) -> str:
    """Reject obvious SSRF targets: file://, internal addresses, metadata IPs.
    Returns the validated URL string. Raises HTTPException on failure.
    """
    if not url or not isinstance(url, str):
        raise HTTPException(400, "url required")
    parsed = urlparse(url.strip())
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise HTTPException(400, f"unsupported url scheme: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise HTTPException(400, "url missing host")
    # Block obviously dangerous metadata host strings before DNS lookup.
    lowered = host.lower()
    if lowered in {"localhost", "metadata.google.internal", "metadata"}:
        raise HTTPException(400, "url host is not allowed")
    # Resolve and check every A/AAAA record.
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror:
        raise HTTPException(400, "url host could not be resolved")
    for info in infos:
        ip = info[4][0]
        if _is_private_ip(ip):
            raise HTTPException(400, "url resolves to a private/internal address")
    return url.strip()
