"""Auth endpoints: register, login, logout, me."""
from __future__ import annotations

import logging
from fastapi import APIRouter, Depends, HTTPException, Request, Response

log = logging.getLogger(__name__)

from ..config import ALLOW_SIGNUP
from ..core.security import (
    clear_session_cookie, make_token, set_session_cookie, verify_password,
)
from ..deps import current_user, optional_user
from ..schemas import LoginIn, RegisterIn, UserOut
from ..services import audit, users


router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/status")
def status():
    return {
        "user_count": users.count(),
        "signup_open": ALLOW_SIGNUP or users.count() == 0,
    }


@router.post("/register", response_model=UserOut)
def register(payload: RegisterIn, request: Request, response: Response):
    log.info(f"[REGISTER] Attempt: username={payload.username} email={payload.email} from {request.client.host if request.client else 'unknown'}")
    if users.count() > 0 and not ALLOW_SIGNUP:
        log.warning(f"[REGISTER] FAILED: Signup disabled, user_count={users.count()}")
        raise HTTPException(403, "signup is disabled")
    
    if not payload.email or not payload.email.strip():
        raise HTTPException(400, "email is required")
    
    try:
        user = users.create_user(
            username=payload.username,
            password=payload.password,
            display_name=payload.display_name,
            email=payload.email,
        )
        log.info(f"[REGISTER] SUCCESS: Created user {user['username']} with id {user['id']}")
    except HTTPException as e:
        log.warning(f"[REGISTER] FAILED: {e.detail}")
        raise
    
    set_session_cookie(response, make_token(user["id"]), request=request)
    audit.log(user["id"], "auth.register", user["id"],
              {"username": user["username"], "is_admin": user["is_admin"]},
              request=request)
    return user


@router.post("/login", response_model=UserOut)
def login(payload: LoginIn, request: Request, response: Response):
    username_or_email = payload.username_or_email.strip()
    print(f"\n=== LOGIN ATTEMPT: {username_or_email} ===", flush=True)
    log.info(f"[LOGIN] Attempt: username_or_email={username_or_email} from {request.client.host if request.client else 'unknown'}")
    
    # Try to find user by username or email
    row = None
    if "@" in username_or_email:
        # Likely an email
        row = users.get_by_email(username_or_email)
        if not row:
            # Try as username as fallback
            row = users.get_by_username(username_or_email)
    else:
        # Try as username first
        row = users.get_by_username(username_or_email)
        if not row:
            # Try as email as fallback
            row = users.get_by_email(username_or_email)
    
    log.info(f"[LOGIN] DB lookup result: user_found={row is not None}")
    if row:
        log.info(f"[LOGIN] Found user: {row.get('username')}")
    
    if not row or not verify_password(payload.password, row["password_hash"]):
        log.warning(f"[LOGIN] FAILED: Invalid credentials for {username_or_email}")
        audit.log(None, "auth.login_failed", None,
                  {"username": (username_or_email or "")[:64]}, request=request)
        raise HTTPException(401, "invalid credentials")
    
    log.info(f"[LOGIN] SUCCESS: {row['username']}")
    set_session_cookie(response, make_token(row["id"]), request=request)
    user = users.get_user(row["id"])
    assert user is not None
    audit.log(user["id"], "auth.login", user["id"], None, request=request)
    return user


@router.post("/logout")
def logout(request: Request, response: Response, user: dict | None = Depends(optional_user)):
    if user:
        users.bump_min_iat(user["id"])  # invalidate any other live tokens
    clear_session_cookie(response, request=request)
    return {"ok": True}


@router.get("/me", response_model=UserOut)
def me(user: dict = Depends(current_user)):
    return user
