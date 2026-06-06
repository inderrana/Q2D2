"""Ollama model management."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from ..config import DEFAULT_MODEL
from ..deps import current_user
from ..services import ollama


router = APIRouter(prefix="/api/models", tags=["models"])


_EMBED_FAMILIES = {"bert", "nomic-bert", "jina-bert", "xlm-roberta"}

# Substring hints in the model id that mark it as an embedding model even
# when Ollama's `details.family` doesn't make it obvious (BGE, mxbai,
# Arctic-Embed, EmbeddingGemma, Qwen3-Embedding, …).
_EMBED_NAME_HINTS = (
    "embed", "rerank", "bge-", "bge_", "mxbai", "arctic-embed",
    "embeddinggemma", "gte-", "e5-",
)


def _classify(model: dict) -> str:
    name = (model.get("name") or "").lower()
    fam = (model.get("details") or {}).get("family", "").lower()
    fams = [f.lower() for f in (model.get("details") or {}).get("families") or []]
    if fam in _EMBED_FAMILIES or any(f in _EMBED_FAMILIES for f in fams):
        return "embed"
    if any(h in name for h in _EMBED_NAME_HINTS):
        return "embed"
    return "chat"


@router.get("")
async def list_models(user: dict = Depends(current_user)):
    try:
        models = await ollama.list_models()
    except Exception as e:
        raise HTTPException(502, f"Ollama unreachable: {e}")
    for m in models:
        m["kind"] = _classify(m)
    return {"models": models, "default": DEFAULT_MODEL}


@router.get("/info")
async def info(name: str, user: dict = Depends(current_user)):
    """Return details for a model, pulled-or-not.

    If the model is already present locally, we use Ollama's /api/show which
    includes parameter count, quantization, context length, license, etc.
    Otherwise we probe the public registry for the manifest so the UI can show
    at least the download size before the user commits to pulling.
    """
    name = (name or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    local: dict | None = None
    try:
        local = await ollama.show_model(name)
    except Exception:
        local = None
    remote = await ollama.registry_manifest(name)
    return {"name": name, "local": local, "remote": remote}


@router.get("/capabilities")
async def capabilities(name: str, user: dict = Depends(current_user)):
    """Return the capability set for a model (e.g. {'tools', 'thinking'}).

    Used by the UI to decide whether to show the Thinking toggle and to
    skip the tool-calling path for models that don't advertise `tools`.
    """
    name = (name or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    try:
        caps = await ollama.model_capabilities(name)
    except Exception:
        caps = set()
    return {"name": name, "capabilities": sorted(caps)}


@router.post("/pull")
async def pull(payload: dict, user: dict = Depends(current_user)):
    if not user.get("is_admin"):
        raise HTTPException(403, "admin only")
    name = (payload or {}).get("name")
    if not name:
        raise HTTPException(400, "name required")

    async def gen():
        # If the client disconnects (user clicks Cancel and aborts the fetch),
        # this generator is cancelled by Starlette, which exits the underlying
        # httpx stream context in ollama.pull_model() and closes the upstream
        # TCP connection to Ollama — effectively cancelling the pull.
        async for line in ollama.pull_model(name):
            yield line + "\n"
    return StreamingResponse(gen(), media_type="application/x-ndjson")


@router.delete("/{name:path}")
async def delete(name: str, user: dict = Depends(current_user)):
    if not user.get("is_admin"):
        raise HTTPException(403, "admin only")
    await ollama.delete_model(name)
    return {"ok": True}


@router.get("/loaded")
async def loaded(user: dict = Depends(current_user)):
    """Models currently held in memory (RAM/VRAM) by Ollama."""
    try:
        models = await ollama.list_loaded()
    except Exception as e:
        raise HTTPException(502, f"Ollama unreachable: {e}")
    return {"models": models}


@router.post("/unload")
async def unload(payload: dict, user: dict = Depends(current_user)):
    """Unload one named model (`{"name": "..."}`) or all loaded models
    (`{"all": true}`). Available to any signed-in user since it only frees
    memory and doesn't affect installed weights."""
    name = (payload or {}).get("name")
    if (payload or {}).get("all"):
        try:
            loaded_models = await ollama.list_loaded()
        except Exception as e:
            raise HTTPException(502, f"Ollama unreachable: {e}")
        unloaded: list[str] = []
        for m in loaded_models:
            mname = m.get("name") or m.get("model")
            if not mname:
                continue
            try:
                await ollama.unload_model(mname)
                unloaded.append(mname)
            except Exception:
                # Best-effort: keep going so a stuck model doesn't block the rest.
                pass
        return {"ok": True, "unloaded": unloaded}
    if not name:
        raise HTTPException(400, "name or all required")
    try:
        await ollama.unload_model(name)
    except Exception as e:
        raise HTTPException(502, f"unload failed: {e}")
    return {"ok": True, "unloaded": [name]}


@router.post("/restart-service")
async def restart_service(user: dict = Depends(current_user)):
    """Forcibly restart the Ollama container to release stuck memory.
    Use this when 'Unload all' doesn't free enough RAM/VRAM (e.g. after a
    crashed load left mmap'd weights behind). Admin only."""
    if not user.get("is_admin"):
        raise HTTPException(403, "admin only")
    try:
        result = await ollama.restart_service()
    except ollama.OllamaError as e:
        raise HTTPException(e.status, e.detail)
    except Exception as e:
        raise HTTPException(502, f"restart failed: {e}")
    return result
