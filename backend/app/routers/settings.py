"""User settings + profile + password change."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from ..config import UPLOAD_DIR
from ..core.security import verify_password
from ..deps import current_user
from ..schemas import UserSettings
from ..services import audit, users


router = APIRouter(prefix="/api/settings", tags=["settings"])

AVATAR_DIR = Path(UPLOAD_DIR) / "avatars"
AVATAR_DIR.mkdir(parents=True, exist_ok=True)
ALLOWED_AVATAR_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
MAX_AVATAR_BYTES = 4 * 1024 * 1024  # 4 MB


class ProfilePatch(BaseModel):
    username: str | None = None
    display_name: str | None = None
    email: str | None = None


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


@router.get("")
def get_settings(user: dict = Depends(current_user)):
    return users.get_settings_public(user["id"])


@router.patch("")
def patch_settings(payload: UserSettings, user: dict = Depends(current_user)):
    return users.update_settings(user["id"], payload.model_dump(exclude_unset=True))


@router.patch("/profile")
def patch_profile(payload: ProfilePatch, user: dict = Depends(current_user)):
    if payload.username is not None and payload.username.strip().lower() != user["username"]:
        users.change_username(user["id"], payload.username)
    return users.update_profile(user["id"], payload.display_name, payload.email)


@router.post("/password")
def change_password(payload: PasswordChange, request: Request,
                    user: dict = Depends(current_user)):
    row = users.get_by_username(user["username"])
    if not row or not verify_password(payload.current_password, row["password_hash"]):
        audit.log(user["id"], "auth.password_change_failed", user["id"],
                  None, request=request)
        raise HTTPException(401, "current password incorrect")
    users.change_password(user["id"], payload.new_password)
    audit.log(user["id"], "auth.password_changed", user["id"], None, request=request)
    return {"ok": True}


# ---- Avatar -----------------------------------------------------------------

# Magic-byte sniffing avoids accepting renamed executables / SVGs with embedded
# scripts. Only raster formats we re-serve are accepted.
_MAGIC: list[tuple[bytes, str]] = [
    (b"\x89PNG\r\n\x1a\n",        ".png"),
    (b"\xff\xd8\xff",              ".jpg"),
    (b"GIF87a",                    ".gif"),
    (b"GIF89a",                    ".gif"),
]


def _sniff(data: bytes) -> str | None:
    for sig, ext in _MAGIC:
        if data.startswith(sig):
            return ext
    if len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


@router.post("/avatar")
async def upload_avatar(
    file: UploadFile = File(...),
    user: dict = Depends(current_user),
):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_AVATAR_EXT:
        raise HTTPException(400, f"unsupported image type ({suffix or 'unknown'})")
    data = await file.read()
    if len(data) > MAX_AVATAR_BYTES:
        raise HTTPException(413, "image too large (max 4 MB)")
    if len(data) < 16:
        raise HTTPException(400, "empty image")
    sniffed = _sniff(data)
    if not sniffed:
        raise HTTPException(400, "file is not a recognized PNG/JPEG/GIF/WebP image")
    # Trust the magic-byte result over the user-supplied extension.
    suffix = sniffed
    # Remove any prior avatar with a different extension
    for ext in ALLOWED_AVATAR_EXT:
        p = AVATAR_DIR / f"{user['id']}{ext}"
        if p.exists():
            try: p.unlink()
            except Exception: pass
    dest = AVATAR_DIR / f"{user['id']}{suffix}"
    dest.write_bytes(data)
    rel = f"{user['id']}{suffix}"
    return users.set_avatar(user["id"], rel)


@router.delete("/avatar")
def delete_avatar(user: dict = Depends(current_user)):
    for ext in ALLOWED_AVATAR_EXT:
        p = AVATAR_DIR / f"{user['id']}{ext}"
        if p.exists():
            try: p.unlink()
            except Exception: pass
    return users.set_avatar(user["id"], None)


@router.get("/avatar/{user_id}")
def get_avatar(user_id: str, user: dict = Depends(current_user)):
    """Avatars are only readable by the owner or by an admin (no IDOR)."""
    if user_id != user["id"] and not user.get("is_admin"):
        return Response(status_code=404)
    for ext in ALLOWED_AVATAR_EXT:
        p = AVATAR_DIR / f"{user_id}{ext}"
        if p.exists():
            return FileResponse(str(p), headers={"Cache-Control": "private, max-age=300"})
    return Response(status_code=404)

