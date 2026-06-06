"""Brave Search API provider — 2000/month free tier."""
from __future__ import annotations

import os
from typing import Any

import httpx

from .base import Provider, enrich


URL = "https://api.search.brave.com/res/v1/web/search"


class BraveProvider(Provider):
    def _key(self, settings: dict) -> str:
        return (settings.get("brave_api_key") or os.getenv("BRAVE_API_KEY") or "").strip()

    def is_configured(self, settings: dict) -> bool:
        return bool(self._key(settings))

    async def search_and_fetch(self, query, *, max_results, max_chars, settings):
        key = self._key(settings)
        params = {"q": query, "count": min(max_results, 20), "safesearch": "moderate"}
        async with httpx.AsyncClient(timeout=20.0) as cx:
            r = await cx.get(URL, params=params, headers={
                "Accept": "application/json",
                "X-Subscription-Token": key,
            })
            r.raise_for_status()
            data = r.json()
        web = (data.get("web") or {}).get("results", [])[:max_results]
        results: list[dict[str, Any]] = []
        for it in web:
            results.append({
                "title": it.get("title") or "",
                "url": it.get("url"),
                "snippet": (it.get("description") or "").strip(),
            })
        return await enrich(results, max_chars=max_chars)


PROVIDER = BraveProvider(
    id="brave",
    label="Brave Search",
    description="Independent index. Free tier: 2000 searches/month.",
    requires=["BRAVE_API_KEY (env) or settings.brave_api_key"],
    monthly_quota=2000,
    help_url="https://brave.com/search/api/",
)
