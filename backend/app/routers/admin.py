"""Admin endpoints — user listing and role management."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..deps import current_user
from ..schemas import AdminCreateUserIn
from ..services import audit, embeddings as embed_svc, users


router = APIRouter(prefix="/api/admin", tags=["admin"])


def _require_admin(user: dict) -> None:
    if not user.get("is_admin"):
        raise HTTPException(403, "admin only")


class AdminUserPatch(BaseModel):
    is_admin: bool | None = None


@router.get("/users")
def list_users(user: dict = Depends(current_user)):
    _require_admin(user)
    return {"users": users.list_users_with_stats()}


@router.post("/users", response_model=dict)
def create_user(payload: AdminCreateUserIn, request: Request, user: dict = Depends(current_user)):
    _require_admin(user)
    try:
        new_user = users.create_user(
            username=payload.username,
            password=payload.password,
            display_name=payload.display_name,
            email=payload.email,
        )
        audit.log(user["id"], "admin.create_user", new_user["id"],
                  {"username": new_user["username"]}, request=request)
        return new_user
    except HTTPException as e:
        raise


@router.get("/audit")
def list_audit(user: dict = Depends(current_user), limit: int = 200):
    _require_admin(user)
    return {"events": audit.recent(min(max(limit, 1), 1000))}


@router.patch("/users/{uid}")
def patch_user(uid: str, payload: AdminUserPatch, request: Request,
               user: dict = Depends(current_user)):
    _require_admin(user)
    target = users.get_user(uid)
    if not target:
        raise HTTPException(404, "user not found")
    if payload.is_admin is not None:
        if not payload.is_admin and uid == user["id"]:
            raise HTTPException(400, "cannot demote yourself")
        if not payload.is_admin and users.admin_count() <= 1 and target["is_admin"]:
            raise HTTPException(400, "cannot demote the last admin")
        users.set_admin(uid, payload.is_admin)
        # Force re-login for the affected user (token reflects old role).
        users.bump_min_iat(uid)
        audit.log(user["id"], "admin.set_admin", uid,
                  {"is_admin": payload.is_admin}, request=request)
    return users.get_user(uid)


@router.delete("/users/{uid}")
def delete_user(uid: str, request: Request, user: dict = Depends(current_user)):
    _require_admin(user)
    if uid == user["id"]:
        raise HTTPException(400, "cannot delete yourself")
    target = users.get_user(uid)
    if not target:
        raise HTTPException(404, "user not found")
    if target["is_admin"] and users.admin_count() <= 1:
        raise HTTPException(400, "cannot delete the last admin")
    users.delete_user(uid)
    audit.log(user["id"], "admin.delete_user", uid,
              {"username": target.get("username")}, request=request)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Embedding model administration
# ---------------------------------------------------------------------------
# The active embedding model is a deployment-wide setting (not per-user)
# because vectors from different models are not comparable — mixing them in
# one collection would silently corrupt retrieval. Switching the model
# triggers a re-embed of every existing document.

class EmbedProbePayload(BaseModel):
    model: str


class EmbedApplyPayload(BaseModel):
    model: str
    dim: int  # client must pass the value returned by /probe so we don't
              # re-validate here and risk a race with an Ollama outage


@router.get("/embeddings")
def get_embeddings(user: dict = Depends(current_user)):
    _require_admin(user)
    return {
        "active": {
            "model": embed_svc.active_model(),
            "dim":   embed_svc.active_dim(),
        },
        "curated": embed_svc.CURATED_EMBED_MODELS,
        "reembed": embed_svc.reembed_status(),
    }


@router.post("/embeddings/probe")
async def probe_embeddings(payload: EmbedProbePayload,
                           user: dict = Depends(current_user)):
    _require_admin(user)
    model = (payload.model or "").strip()
    if not model:
        raise HTTPException(400, "model is required")
    try:
        dim = await embed_svc.probe_dim(model)
    except embed_svc.EmbedProbeError as e:
        raise HTTPException(400, str(e))
    return {"model": model, "dim": dim}


@router.post("/embeddings/apply")
async def apply_embeddings(payload: EmbedApplyPayload, request: Request,
                           user: dict = Depends(current_user)):
    _require_admin(user)
    model = (payload.model or "").strip()
    if not model or payload.dim <= 0:
        raise HTTPException(400, "model and dim required")
    if model == embed_svc.active_model() and payload.dim == embed_svc.active_dim():
        return {"ok": True, "noop": True, "reembed": embed_svc.reembed_status()}
    try:
        status = await embed_svc.start_reembed(model, payload.dim)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    audit.log(user["id"], "admin.embed_model_change", None,
              {"model": model, "dim": payload.dim}, request=request)
    return {"ok": True, "reembed": status}


@router.get("/embeddings/status")
def embeddings_status(user: dict = Depends(current_user)):
    _require_admin(user)
    return embed_svc.reembed_status()
