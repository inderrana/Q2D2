"""Chat CRUD."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException

from .. import db
from ..config import DEFAULT_MODEL
from ..deps import current_user
from ..schemas import ChatCreate, ChatRename
from ..services import chat as chat_svc, users
from . import documents as documents_router


router = APIRouter(prefix="/api/chats", tags=["chats"])


@router.get("")
def list_chats(
    user: dict = Depends(current_user),
    limit: int = 50,
    before: int | None = None,
):
    limit = max(1, min(limit, 200))
    sql = ("SELECT id, title, model, created_at, updated_at FROM chats "
           "WHERE user_id=?")
    args: list = [user["id"]]
    if before is not None:
        sql += " AND updated_at < ?"
        args.append(int(before))
    sql += " ORDER BY updated_at DESC LIMIT ?"
    args.append(limit)
    with db.connect() as cx:
        rows = cx.execute(sql, tuple(args)).fetchall()
    return [db.row_to_dict(r) for r in rows]


@router.post("")
def create_chat(payload: ChatCreate, user: dict = Depends(current_user)):
    cid = db.new_id()
    now = db.now()
    settings = users.get_settings(user["id"])
    model = payload.model or settings.get("default_model") or DEFAULT_MODEL
    with db.connect() as cx:
        cx.execute(
            "INSERT INTO chats (id, user_id, title, model, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (cid, user["id"], payload.title or "New chat", model, now, now),
        )
    return {"id": cid, "title": payload.title or "New chat", "model": model}


@router.get("/{chat_id}")
def get_chat(chat_id: str, user: dict = Depends(current_user)):
    with db.connect() as cx:
        chat = cx.execute(
            "SELECT * FROM chats WHERE id=? AND user_id=?", (chat_id, user["id"]),
        ).fetchone()
        if not chat:
            raise HTTPException(404, "chat not found")
        msgs = cx.execute(
            "SELECT id, role, content, model, tokens_in, tokens_out, duration_ms, meta, created_at "
            "FROM messages WHERE chat_id=? ORDER BY created_at ASC",
            (chat_id,),
        ).fetchall()
    return {
        "chat": db.row_to_dict(chat),
        "messages": [
            {**db.row_to_dict(m),
             "meta": json.loads(m["meta"]) if m["meta"] else None}
            for m in msgs
        ],
    }


@router.patch("/{chat_id}")
def rename_chat(chat_id: str, payload: ChatRename, user: dict = Depends(current_user)):
    with db.connect() as cx:
        cx.execute(
            "UPDATE chats SET title=?, updated_at=? WHERE id=? AND user_id=?",
            (payload.title, db.now(), chat_id, user["id"]),
        )
    return {"ok": True}


@router.delete("/{chat_id}")
def delete_chat(chat_id: str, user: dict = Depends(current_user)):
    with db.connect() as cx:
        owned = cx.execute(
            "SELECT id FROM chats WHERE id=? AND user_id=?", (chat_id, user["id"])
        ).fetchone()
        if not owned:
            return {"ok": True}
    # Purge attached docs (files + chroma) before SQL cascade removes their rows.
    documents_router.purge_chat_documents(chat_id)
    with db.connect() as cx:
        cx.execute("DELETE FROM chats WHERE id=? AND user_id=?", (chat_id, user["id"]))
    return {"ok": True}


@router.get("/{chat_id}/documents")
def list_chat_documents(chat_id: str, user: dict = Depends(current_user)):
    with db.connect() as cx:
        owned = cx.execute(
            "SELECT id FROM chats WHERE id=? AND user_id=?", (chat_id, user["id"])
        ).fetchone()
        if not owned:
            raise HTTPException(404, "chat not found")
        rows = cx.execute(
            "SELECT id, name, bytes, chunks, created_at FROM documents "
            "WHERE chat_id=? AND user_id=? ORDER BY created_at ASC",
            (chat_id, user["id"]),
        ).fetchall()
    return {"documents": [db.row_to_dict(r) for r in rows]}


@router.post("/{chat_id}/title")
async def auto_title(chat_id: str, user: dict = Depends(current_user)):
    title = await chat_svc.auto_title(chat_id, user["id"])
    return {"title": title}
