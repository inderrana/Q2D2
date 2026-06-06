"""Usage and stats endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..deps import current_user
from ..services import usage


router = APIRouter(prefix="/api/usage", tags=["usage"])


@router.get("")
def my_usage(user: dict = Depends(current_user)):
    return usage.user_stats(user["id"])


@router.get("/admin")
def all_usage(user: dict = Depends(current_user)):
    if not user.get("is_admin"):
        raise HTTPException(403, "admin only")
    return usage.admin_stats()
