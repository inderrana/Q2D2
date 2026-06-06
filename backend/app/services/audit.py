"""Audit log writer."""
from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import Request

from .. import db


def log(actor_id: Optional[str], action: str,
        target_id: Optional[str] = None,
        meta: Optional[dict[str, Any]] = None,
        request: Optional[Request] = None) -> None:
    ip = None
    if request is not None:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            ip = fwd.split(",")[0].strip()
        elif request.client:
            ip = request.client.host
    with db.connect() as cx:
        cx.execute(
            "INSERT INTO audit_log (actor_id, action, target_id, meta, ip, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (actor_id, action, target_id,
             json.dumps(meta) if meta else None, ip, db.now()),
        )


def recent(limit: int = 200) -> list[dict]:
    with db.connect() as cx:
        rows = cx.execute(
            "SELECT id, actor_id, action, target_id, meta, ip, created_at "
            "FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        d = db.row_to_dict(r)
        if d.get("meta"):
            try:
                d["meta"] = json.loads(d["meta"])
            except Exception:
                pass
        out.append(d)
    return out
