"""User CRUD."""
from __future__ import annotations

import json
import time
from typing import Optional

from fastapi import HTTPException

from .. import db
from ..config import MIN_PASSWORD_LEN
from ..core import crypto
from ..core.security import hash_password


DEFAULT_SETTINGS = {
    "default_model": None,
    "temperature": 0.7,
    "use_rag_default": True,
    "use_web_default": True,
    "rag_top_k": 4,
    "web_top_k": 4,
    "web_chars_per_result": 2200,
    "max_history_messages": 60,
    "ref_chat_turns": 8,
    # When True, capable models (those with the `tools` capability) drive
    # web/RAG retrieval themselves via Ollama tool calls instead of running
    # the deterministic classifier→rewriter→search pipeline. Falls back to
    # the legacy path automatically for models without tool support.
    "tool_calling": True,
    "theme": "dark",
    "accent": None,                      # hex color e.g. #0066cc; None = system default
    "sidebar_color": None,               # hex color for sidebar background; None = theme default
    "persona_extra": None,
    # Web search provider configuration (per-user)
    "search_provider": "searxng",         # tavily | searxng | brave | duckduckgo
    "tavily_api_key": None,              # encrypted at rest; falls back to env TAVILY_API_KEY
    "brave_api_key": None,               # encrypted at rest; falls back to env BRAVE_API_KEY
    "searxng_url": None,                 # falls back to env SEARXNG_URL
}

# Settings keys whose values are encrypted before they hit disk.
_ENCRYPTED_KEYS = {"tavily_api_key", "brave_api_key"}


def count() -> int:
    with db.connect() as cx:
        return cx.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]


def get_user(user_id: str) -> Optional[dict]:
    with db.connect() as cx:
        row = cx.execute(
            "SELECT id, username, display_name, email, is_admin, avatar, created_at FROM users WHERE id=?",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    return {**db.row_to_dict(row), "is_admin": bool(row["is_admin"])}


def get_by_username(username: str) -> Optional[dict]:
    with db.connect() as cx:
        row = cx.execute(
            "SELECT id, username, display_name, email, password_hash, is_admin, created_at "
            "FROM users WHERE username=?",
            (username.strip().lower(),),
        ).fetchone()
    return db.row_to_dict(row) if row else None


def get_by_email(email: str) -> Optional[dict]:
    with db.connect() as cx:
        row = cx.execute(
            "SELECT id, username, display_name, email, password_hash, is_admin, created_at "
            "FROM users WHERE email=?",
            (email.strip().lower(),),
        ).fetchone()
    return db.row_to_dict(row) if row else None


def create_user(username: str, password: str,
                display_name: str | None = None,
                email: str | None = None) -> dict:
    username = (username or "").strip().lower()
    if len(username) < 2:
        raise HTTPException(400, "username must be at least 2 chars")
    if not username.replace("_", "").replace("-", "").replace(".", "").isalnum():
        raise HTTPException(400, "username may contain letters, digits, . _ -")
    if len(password or "") < MIN_PASSWORD_LEN:
        raise HTTPException(400, f"password must be at least {MIN_PASSWORD_LEN} chars")
    
    # Email validation: email is mandatory and must be unique
    email = (email or "").strip().lower() if email else None
    if not email:
        raise HTTPException(400, "email is required")
    if "@" not in email or "." not in email.split("@")[1]:
        raise HTTPException(400, "invalid email format")

    uid = db.new_id()
    now = db.now()
    # Race-free first-user-becomes-admin: count + insert in one IMMEDIATE txn.
    with db.connect() as cx:
        cx.execute("BEGIN IMMEDIATE")
        is_first = cx.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] == 0
        if cx.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone():
            raise HTTPException(409, "username already taken")
        if cx.execute("SELECT id FROM users WHERE LOWER(email)=?", (email,)).fetchone():
            raise HTTPException(409, "email already in use")
        cx.execute(
            "INSERT INTO users (id, username, display_name, email, password_hash, is_admin, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (uid, username, display_name or username, email, hash_password(password),
             1 if is_first else 0, now),
        )
        cx.execute(
            "INSERT INTO settings (user_id, data) VALUES (?, ?)",
            (uid, json.dumps(DEFAULT_SETTINGS)),
        )
    user = get_user(uid)
    assert user is not None
    return user


def get_settings(user_id: str) -> dict:
    with db.connect() as cx:
        row = cx.execute("SELECT data FROM settings WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        return DEFAULT_SETTINGS.copy()
    data = json.loads(row["data"])
    merged = {**DEFAULT_SETTINGS, **data}
    # Decrypt secret fields for in-process use.
    for k in _ENCRYPTED_KEYS:
        if merged.get(k):
            merged[k] = crypto.decrypt(merged[k])
    return merged


def get_settings_public(user_id: str) -> dict:
    """Same as get_settings but masks secret fields for safe round-trip to UI."""
    s = get_settings(user_id)
    for k in _ENCRYPTED_KEYS:
        if s.get(k):
            s[k] = crypto.mask(s[k])
    return s


def update_settings(user_id: str, patch: dict) -> dict:
    current_raw = _raw_settings(user_id)
    merged = {**current_raw, **{k: v for k, v in patch.items() if v is not None}}
    # Encrypt secret fields before persisting. A masked value (• prefix) means
    # "don't change"; preserve the existing ciphertext.
    for k in _ENCRYPTED_KEYS:
        v = merged.get(k)
        if v is None or v == "":
            merged[k] = None
        elif isinstance(v, str) and v.startswith("\u2022"):
            merged[k] = current_raw.get(k)
        else:
            merged[k] = crypto.encrypt(v)
    with db.connect() as cx:
        cx.execute(
            "INSERT INTO settings (user_id, data) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET data=excluded.data",
            (user_id, json.dumps(merged)),
        )
    return get_settings_public(user_id)


def _raw_settings(user_id: str) -> dict:
    with db.connect() as cx:
        row = cx.execute("SELECT data FROM settings WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        return DEFAULT_SETTINGS.copy()
    return {**DEFAULT_SETTINGS, **json.loads(row["data"])}


def change_password(user_id: str, new_password: str) -> None:
    if len(new_password) < MIN_PASSWORD_LEN:
        raise HTTPException(400, f"password must be at least {MIN_PASSWORD_LEN} chars")
    with db.connect() as cx:
        cx.execute(
            "UPDATE users SET password_hash=?, min_iat=? WHERE id=?",
            (hash_password(new_password), int(time.time()), user_id),
        )


def get_min_iat(user_id: str) -> int:
    with db.connect() as cx:
        row = cx.execute("SELECT min_iat FROM users WHERE id=?", (user_id,)).fetchone()
    return int(row["min_iat"]) if row and row["min_iat"] is not None else 0


def bump_min_iat(user_id: str) -> None:
    with db.connect() as cx:
        cx.execute("UPDATE users SET min_iat=? WHERE id=?", (int(time.time()), user_id))


def update_profile(user_id: str, display_name: str | None = None, email: str | None = None) -> dict:
    with db.connect() as cx:
        if display_name is not None:
            cx.execute("UPDATE users SET display_name=? WHERE id=?", (display_name, user_id))
        if email is not None:
            cx.execute("UPDATE users SET email=? WHERE id=?", (email, user_id))
    user = get_user(user_id)
    assert user is not None
    return user


def set_avatar(user_id: str, avatar: str | None) -> dict:
    with db.connect() as cx:
        cx.execute("UPDATE users SET avatar=? WHERE id=?", (avatar, user_id))
    user = get_user(user_id)
    assert user is not None
    return user


def change_username(user_id: str, new_username: str) -> dict:
    new_username = (new_username or "").strip().lower()
    if len(new_username) < 2:
        raise HTTPException(400, "username must be at least 2 chars")
    if not new_username.replace("_", "").replace("-", "").replace(".", "").isalnum():
        raise HTTPException(400, "username may contain letters, digits, . _ -")
    with db.connect() as cx:
        existing = cx.execute(
            "SELECT id FROM users WHERE username=? AND id<>?", (new_username, user_id)
        ).fetchone()
        if existing:
            raise HTTPException(409, "username already taken")
        cx.execute("UPDATE users SET username=? WHERE id=?", (new_username, user_id))
    user = get_user(user_id)
    assert user is not None
    return user


def admin_count() -> int:
    with db.connect() as cx:
        return cx.execute("SELECT COUNT(*) AS c FROM users WHERE is_admin=1").fetchone()["c"]


def set_admin(user_id: str, is_admin: bool) -> dict:
    with db.connect() as cx:
        cx.execute("UPDATE users SET is_admin=? WHERE id=?", (1 if is_admin else 0, user_id))
    user = get_user(user_id)
    assert user is not None
    return user


def list_users_with_stats() -> list[dict]:
    """List all users with chat/message/token counts and last activity."""
    with db.connect() as cx:
        rows = cx.execute(
            """
            SELECT u.id, u.username, u.display_name, u.email, u.is_admin, u.avatar, u.created_at,
                   (SELECT COUNT(*) FROM chats c WHERE c.user_id = u.id) AS chats,
                   (SELECT COUNT(*) FROM messages m
                      JOIN chats c2 ON c2.id = m.chat_id WHERE c2.user_id = u.id) AS messages,
                   (SELECT COALESCE(SUM(m.tokens_in), 0) FROM messages m
                      JOIN chats c3 ON c3.id = m.chat_id WHERE c3.user_id = u.id) AS tokens_in,
                   (SELECT COALESCE(SUM(m.tokens_out), 0) FROM messages m
                      JOIN chats c4 ON c4.id = m.chat_id WHERE c4.user_id = u.id) AS tokens_out,
                   (SELECT COUNT(*) FROM documents d WHERE d.user_id = u.id) AS documents,
                   (SELECT MAX(m.created_at) FROM messages m
                      JOIN chats c5 ON c5.id = m.chat_id WHERE c5.user_id = u.id) AS last_active
            FROM users u
            ORDER BY u.created_at ASC
            """
        ).fetchall()
    out = []
    for r in rows:
        d = db.row_to_dict(r)
        d["is_admin"] = bool(d["is_admin"])
        out.append(d)
    return out


def delete_user(user_id: str) -> None:
    """Hard delete a user and all associated data (chats, messages, documents, settings).

    SQL deletions run in one transaction. Filesystem + Chroma cleanup happen
    afterwards (failures there are recoverable; SQL inconsistency is not).
    """
    from pathlib import Path
    from ..config import UPLOAD_DIR
    from . import rag

    # Collect doc ids + avatar before SQL cascade so we can clean disk after.
    with db.connect() as cx:
        cx.execute("BEGIN IMMEDIATE")
        doc_rows = cx.execute(
            "SELECT id FROM documents WHERE user_id=?", (user_id,)
        ).fetchall()
        avatar_row = cx.execute(
            "SELECT avatar FROM users WHERE id=?", (user_id,)
        ).fetchone()
        doc_ids = [r["id"] for r in doc_rows]
        # SQL deletion (chats + messages cascade via FK; clean others manually).
        cx.execute("DELETE FROM web_searches WHERE user_id=?", (user_id,))
        cx.execute("DELETE FROM doc_folders  WHERE user_id=?", (user_id,))
        cx.execute("DELETE FROM documents    WHERE user_id=?", (user_id,))
        cx.execute("DELETE FROM settings     WHERE user_id=?", (user_id,))
        cx.execute("DELETE FROM chats        WHERE user_id=?", (user_id,))
        cx.execute("DELETE FROM users        WHERE id=?",      (user_id,))

    # Best-effort post-commit cleanup
    for did in doc_ids:
        try:
            rag.delete_document(did)
        except Exception:
            pass
        for p in Path(UPLOAD_DIR).glob(f"{did}_*"):
            try:
                p.unlink()
            except Exception:
                pass
    if avatar_row and avatar_row["avatar"]:
        try:
            (Path(UPLOAD_DIR) / "avatars" / avatar_row["avatar"]).unlink()
        except Exception:
            pass

