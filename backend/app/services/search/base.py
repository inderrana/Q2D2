"""Provider base classes and shared helpers."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import httpx
import trafilatura


class WebSearchNotConfigured(Exception):
    """Raised when the active provider is missing required configuration."""


@dataclass
class Provider:
    id: str
    label: str
    description: str = ""
    requires: list[str] = field(default_factory=list)
    free: bool = True
    monthly_quota: int | None = None
    help_url: str | None = None

    def is_configured(self, settings: dict) -> bool:  # pragma: no cover - overridden
        return True

    async def search_and_fetch(self, query: str, *, max_results: int,
                               max_chars: int, settings: dict) -> list[dict[str, Any]]:
        raise NotImplementedError


USER_AGENT = "Mozilla/5.0 (compatible; LocalRAG/1.0)"


async def fetch_page(url: str, *, timeout: float = 12.0) -> str:
    """Fetch a URL with SSRF guard + size cap and return clean main text."""
    from ...config import MAX_FETCH_BYTES
    from ...core.ssrf import assert_safe_url
    try:
        url = assert_safe_url(url)
    except Exception:
        return ""
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        ) as cx:
            async with cx.stream("GET", url) as r:
                r.raise_for_status()
                buf = bytearray()
                async for chunk in r.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > MAX_FETCH_BYTES:
                        return ""
            text = trafilatura.extract(
                buf.decode("utf-8", errors="replace"),
                include_comments=False, include_tables=False,
            ) or ""
            return text.strip()
    except Exception:
        return ""


async def enrich(results: list[dict[str, Any]], *, max_chars: int) -> list[dict[str, Any]]:
    """Replace each result's short blurb with the extracted main body text.
    Falls back to the original snippet when fetch/extract fails so the model
    still gets *something* per source."""
    pages = await asyncio.gather(*[fetch_page(r["url"]) for r in results])
    out: list[dict[str, Any]] = []
    for r, body in zip(results, pages):
        text = (body or (r.get("snippet") or "")).strip()
        if not text:
            continue
        out.append({"title": r.get("title") or r["url"],
                    "url": r["url"],
                    "snippet": text[:max_chars]})
    return out
