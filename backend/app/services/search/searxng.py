"""SearXNG provider — self-hosted, no key, fully private."""
from __future__ import annotations

import os
from typing import Any

import httpx

from .base import Provider, enrich


class SearxngProvider(Provider):
    def _url(self, settings: dict) -> str:
        return (settings.get("searxng_url") or os.getenv("SEARXNG_URL") or "").rstrip("/")

    def is_configured(self, settings: dict) -> bool:
        return bool(self._url(settings))

    async def search_and_fetch(self, query, *, max_results, max_chars, settings):
        base = self._url(settings)
        params = {"q": query, "format": "json", "safesearch": 1, "language": "en"}
        async with httpx.AsyncClient(timeout=20.0) as cx:
            # SearXNG's botdetection whitelists docker private subnets via
            # limiter.toml -> botdetection.ip_lists.pass_ip, so no X-Forwarded-For
            # spoofing is needed from us.
            r = await cx.get(f"{base}/search", params=params,
                             headers={"Accept": "application/json"})
            r.raise_for_status()
            data = r.json()
        results: list[dict[str, Any]] = []
        for it in data.get("results", [])[:max_results]:
            results.append({
                "title": it.get("title") or "",
                "url": it.get("url"),
                "snippet": (it.get("content") or "").strip(),
            })
        return await enrich(results, max_chars=max_chars)


PROVIDER = SearxngProvider(
    id="searxng",
    label="SearXNG",
    description="Self-hosted meta-search. Aggregates many engines. Fully private.",
    requires=["SearXNG instance URL with JSON format enabled"],
    monthly_quota=None,
    help_url="https://docs.searxng.org",
)
