"""Persona / system-prompt builder. All copy comes from branding.toml."""
from __future__ import annotations

from datetime import datetime, timezone

from .. import branding


def system_prompt() -> str:
    return branding.get("persona", "system_prompt", "")


def _now_line() -> str:
    # Local wall-clock in the container's timezone (set via TZ env var).
    now = datetime.now().astimezone()
    return f"Current date/time: {now.strftime('%A, %B %d, %Y %H:%M %Z')} (UTC offset {now.strftime('%z')})."


def build_system(extra: str | None = None, context: str | None = None) -> str:
    parts = [system_prompt(), _now_line()]
    if extra:
        parts.append(extra.strip())
    if context:
        parts.append(f"CONTEXT:\n{context}")
    return "\n\n".join(p for p in parts if p)
