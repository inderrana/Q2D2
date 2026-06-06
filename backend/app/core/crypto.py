"""Symmetric encryption for at-rest user secrets (per-user API keys).

The master key comes from the ENCRYPTION_KEY env var (provisioned by run.sh
from `pass`). On dev machines without one, we derive a stable per-install key
from JWT_SECRET so the app still runs — but log a warning, since rotating
JWT_SECRET would then orphan the encrypted blobs.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from ..config import JWT_SECRET

log = logging.getLogger(__name__)

_PREFIX = "enc1:"  # versioned prefix so we can rotate algorithms later


def _master_key() -> bytes:
    raw = os.getenv("ENCRYPTION_KEY", "").strip()
    if raw:
        try:
            # Validate: Fernet keys are 32 url-safe base64 bytes.
            Fernet(raw.encode())
            return raw.encode()
        except Exception:
            log.warning("ENCRYPTION_KEY is malformed; falling back to JWT_SECRET-derived key")
    digest = hashlib.sha256(JWT_SECRET.encode()).digest()
    return base64.urlsafe_b64encode(digest)


_fernet: Optional[Fernet] = None


def _f() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_master_key())
    return _fernet


def encrypt(plaintext: Optional[str]) -> Optional[str]:
    """Return an opaque, versioned ciphertext string (or None for None/empty)."""
    if plaintext is None or plaintext == "":
        return plaintext
    if isinstance(plaintext, str) and plaintext.startswith(_PREFIX):
        return plaintext  # already encrypted
    token = _f().encrypt(plaintext.encode("utf-8"))
    return _PREFIX + token.decode("ascii")


def decrypt(value: Optional[str]) -> Optional[str]:
    """Inverse of `encrypt`. Returns plaintext or None on failure."""
    if not value:
        return value
    if not isinstance(value, str) or not value.startswith(_PREFIX):
        # Legacy plaintext value — return as-is so existing data still works.
        return value
    raw = value[len(_PREFIX):].encode("ascii")
    try:
        return _f().decrypt(raw).decode("utf-8")
    except InvalidToken:
        log.warning("decrypt: invalid token (key rotation or corruption)")
        return None


def mask(value: Optional[str], keep: int = 4) -> Optional[str]:
    """Return a display-safe masked version: last `keep` chars + leading dots."""
    if not value:
        return value
    plain = decrypt(value) if (isinstance(value, str) and value.startswith(_PREFIX)) else value
    if not plain:
        return None
    if len(plain) <= keep:
        return "•" * len(plain)
    return "•" * 4 + plain[-keep:]
