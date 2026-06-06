"""Public branding info for the front-end."""
from __future__ import annotations

from fastapi import APIRouter

from .. import branding

router = APIRouter(prefix="/api/branding", tags=["branding"])


@router.get("")
def get_branding() -> dict:
    return branding.public()
