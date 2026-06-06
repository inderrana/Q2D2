"""Tavily provider — fast, generous free tier."""
from __future__ import annotations

import os
from typing import Any

import httpx

from .base import Provider, enrich


URL = "https://api.tavily.com/search"


class TavilyProvider(Provider):
    def is_configured(self, settings: dict) -> bool:
        return bool(self._key(settings))

    def _key(self, settings: dict) -> str:
        return (settings.get("tavily_api_key") or os.getenv("TAVILY_API_KEY") or "").strip()

    async def search_and_fetch(self, query, *, max_results, max_chars, settings):
        key = self._key(settings)
        payload = {
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
            "include_answer": False,
            "include_raw_content": True,
        }
        async with httpx.AsyncClient(timeout=20.0) as cx:
            r = await cx.post(URL, json=payload, headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            })
            r.raise_for_status()
            data = r.json()
        results: list[dict[str, Any]] = []
        for it in data.get("results", [])[:max_results]:
            results.append({
                "title": it.get("title") or "",
                "url": it.get("url"),
                "snippet": (it.get("raw_content") or it.get("content") or "").strip(),
            })
        return await enrich(results, max_chars=max_chars)


PROVIDER = TavilyProvider(
    id="tavily",
    label="Tavily",
    description="Fast, AI-tuned search. Free tier includes ~1000 searches/month.",
    requires=["TAVILY_API_KEY (env) or settings.tavily_api_key"],
    monthly_quota=1000,
    help_url="https://tavily.com",
)
