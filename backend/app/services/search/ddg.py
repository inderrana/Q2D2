"""DuckDuckGo HTML provider — no key, may rate-limit under load."""
from __future__ import annotations

from typing import Any

import httpx
from bs4 import BeautifulSoup

from .base import Provider, USER_AGENT, enrich


URL = "https://html.duckduckgo.com/html/"


class DuckDuckGoProvider(Provider):
    def is_configured(self, settings: dict) -> bool:
        return True  # no config required

    async def search_and_fetch(self, query, *, max_results, max_chars, settings):
        async with httpx.AsyncClient(
            timeout=20.0, follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
        ) as cx:
            r = await cx.post(URL, data={"q": query})
            r.raise_for_status()
            html = r.text
        soup = BeautifulSoup(html, "html.parser")
        results: list[dict[str, Any]] = []
        for el in soup.select("div.result")[: max_results * 2]:
            a = el.select_one("a.result__a")
            if not a or not a.get("href"):
                continue
            url = a["href"]
            # DDG wraps real URLs in a /l/?uddg=... redirect; parse if present.
            if "/l/?" in url and "uddg=" in url:
                from urllib.parse import parse_qs, urlparse, unquote
                q = parse_qs(urlparse(url).query)
                url = unquote(q.get("uddg", [url])[0])
            snippet_el = el.select_one(".result__snippet")
            results.append({
                "title": a.get_text(strip=True),
                "url": url,
                "snippet": snippet_el.get_text(" ", strip=True) if snippet_el else "",
            })
            if len(results) >= max_results:
                break
        return await enrich(results, max_chars=max_chars)


PROVIDER = DuckDuckGoProvider(
    id="duckduckgo",
    label="DuckDuckGo",
    description="No API key required. Best-effort; may rate-limit under heavy load.",
    requires=[],
    monthly_quota=None,
    help_url="https://duckduckgo.com",
)
