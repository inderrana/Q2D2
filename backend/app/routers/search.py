"""Manual web search endpoint (used by the UI on demand)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..deps import current_user
from ..services import search as search_svc, usage, users


router = APIRouter(prefix="/api/search", tags=["search"])


@router.get("/providers")
def list_providers(user: dict = Depends(current_user)):
    settings = users.get_settings(user["id"])
    return {
        "active": search_svc.resolve(settings).id,
        "providers": search_svc.available_providers(settings),
    }


@router.post("")
async def do_search(payload: dict, user: dict = Depends(current_user)):
    q = ((payload or {}).get("query") or "").strip()
    if not q:
        raise HTTPException(400, "query required")
    settings = users.get_settings(user["id"])
    provider = search_svc.resolve(settings)
    try:
        results = await search_svc.search_and_fetch(
            q, max_results=(payload or {}).get("k", 5), settings=settings,
        )
    except search_svc.WebSearchNotConfigured as e:
        raise HTTPException(503, str(e))
    usage.record_web_search(user["id"], q, len(results), provider.id)
    return {"provider": provider.id, "results": results}
