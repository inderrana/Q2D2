"""Multi-provider web search facade.

Each provider implements the same interface (see ``base.Provider``).
The facade picks the active provider from per-user settings, with the
``TAVILY_API_KEY`` env var providing a sensible global default.

Public API kept stable for callers:
- ``WebSearchNotConfigured``
- ``is_configured(settings=None)`` — true if the active provider is usable
- ``async search_and_fetch(query, *, max_results, settings)`` — list of
  ``{title, url, snippet}`` items
- ``available_providers()`` — list of provider metadata (id, label, description,
  ``configured``, ``requires``)
- ``record_web_search`` is in services.usage, not here.
"""
from __future__ import annotations

import os
from typing import Any, Iterable

from . import brave, ddg, searxng, tavily
from .base import Provider, WebSearchNotConfigured


PROVIDERS: dict[str, Provider] = {
    p.id: p for p in (tavily.PROVIDER, searxng.PROVIDER, brave.PROVIDER, ddg.PROVIDER)
}


def _default_provider_id() -> str:
    env = (os.getenv("DEFAULT_SEARCH_PROVIDER") or "").strip().lower()
    if env in PROVIDERS:
        return env
    if tavily.PROVIDER.is_configured({}):
        return "tavily"
    return "duckduckgo"


def resolve(settings: dict | None) -> Provider:
    settings = settings or {}
    pid = (settings.get("search_provider") or _default_provider_id()).lower()
    return PROVIDERS.get(pid, PROVIDERS["duckduckgo"])


def is_configured(settings: dict | None = None) -> bool:
    return resolve(settings).is_configured(settings or {})


async def search_and_fetch(query: str, max_results: int = 4,
                           settings: dict | None = None,
                           max_chars: int = 2200) -> list[dict[str, Any]]:
    provider = resolve(settings)
    if not provider.is_configured(settings or {}):
        raise WebSearchNotConfigured(
            f"Web search disabled: provider '{provider.id}' is not configured. "
            f"Open Settings → Web Search."
        )
    return await provider.search_and_fetch(query, max_results=max_results,
                                           max_chars=max_chars, settings=settings or {})


def available_providers(settings: dict | None = None) -> list[dict[str, Any]]:
    s = settings or {}
    active = resolve(s).id
    out: list[dict[str, Any]] = []
    for p in PROVIDERS.values():
        out.append({
            "id": p.id,
            "label": p.label,
            "description": p.description,
            "requires": p.requires,
            "configured": p.is_configured(s),
            "active": p.id == active,
            "free": p.free,
            "monthly_quota": p.monthly_quota,
            "help_url": p.help_url,
        })
    return out


__all__: Iterable[str] = (
    "PROVIDERS",
    "WebSearchNotConfigured",
    "is_configured",
    "search_and_fetch",
    "available_providers",
    "resolve",
)
