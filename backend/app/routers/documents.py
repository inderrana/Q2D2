"""Document upload, URL ingest, listing, deletion. Supports folders.

The router is intentionally thin — all business logic lives in
`app.services.documents`. Heavy work (extraction + embedding) runs in a
FastAPI BackgroundTask; clients poll `GET /api/documents/{id}` for status.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import (
    APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile,
)

from .. import db
from ..config import MAX_UPLOAD_BYTES, UPLOAD_DIR
from ..core.ssrf import assert_safe_url
from ..deps import current_user
from ..services import documents as docs_svc, search as search_svc


router = APIRouter(prefix="/api/documents", tags=["documents"])

_UPLOAD_CHUNK = 1024 * 1024  # 1 MiB


async def _read_capped(upload: UploadFile, cap: int) -> bytes:
    """Stream-read an UploadFile, raising 413 once `cap` bytes are exceeded."""
    buf = bytearray()
    while True:
        chunk = await upload.read(_UPLOAD_CHUNK)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > cap:
            raise HTTPException(413, f"file too large (max {cap // (1024 * 1024)} MB)")
    return bytes(buf)


# ----- endpoints -------------------------------------------------------

@router.get("")
def list_documents(
    user: dict = Depends(current_user),
    limit: int = 200,
    before: int | None = None,
):
    return {
        "folders": docs_svc.list_folders(user["id"]),
        "documents": docs_svc.list_user_documents(user["id"], limit=limit, before=before),
    }


@router.get("/{doc_id}")
def get_document(doc_id: str, user: dict = Depends(current_user)):
    """Poll endpoint for clients waiting on background indexing."""
    doc = docs_svc.get_doc(user["id"], doc_id)
    if not doc:
        raise HTTPException(404, "not found")
    return doc


@router.post("/folders")
def create_folder(payload: dict, user: dict = Depends(current_user)):
    path = docs_svc.create_folder(user["id"], (payload or {}).get("path"))
    return {"path": path}


@router.patch("/folders")
def rename_folder(payload: dict, user: dict = Depends(current_user)):
    return docs_svc.rename_folder(
        user["id"], (payload or {}).get("from"), (payload or {}).get("to"),
    )


@router.delete("/folders")
def delete_folder(path: str, user: dict = Depends(current_user)):
    n = docs_svc.delete_folder(user["id"], path)
    return {"ok": True, "deleted_docs": n}


@router.post("/upload")
async def upload(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    folder: str | None = Form(default=""),
    ocr: bool = Form(default=True),  # accepted for back-compat; OCR is auto
    chat_id: str | None = Form(default=None),
    user: dict = Depends(current_user),
):
    folder = docs_svc.clean_folder(folder)
    if chat_id:
        with db.connect() as cx:
            owned = cx.execute(
                "SELECT id FROM chats WHERE id=? AND user_id=?", (chat_id, user["id"])
            ).fetchone()
        if not owned:
            raise HTTPException(404, "chat not found")
        folder = ""  # chat-scoped docs don't live in a folder

    raw_name = file.filename or "file"
    # Browsers using webkitdirectory put the relative path in `filename`; keep
    # only the basename for storage, let the folder field carry the directory.
    safe_name = Path(raw_name.replace("\\", "/")).name or "file"
    data = await _read_capped(file, MAX_UPLOAD_BYTES)
    if not data:
        raise HTTPException(400, "File is empty (0 bytes).")

    doc_id = db.new_id()
    dest = Path(UPLOAD_DIR) / f"{doc_id}_{safe_name}"
    dest.write_bytes(data)

    docs_svc.insert_pending(
        doc_id=doc_id, user_id=user["id"], name=safe_name, source="upload",
        size=len(data), folder=folder, chat_id=chat_id, status="indexing",
    )
    background.add_task(docs_svc.index_file, doc_id, user["id"], safe_name, dest, ocr=ocr)

    return {
        "id": doc_id, "name": safe_name, "bytes": len(data), "chunks": 0,
        "folder": folder, "chat_id": chat_id, "status": "indexing",
    }


@router.post("/url")
async def ingest_url(
    payload: dict,
    background: BackgroundTasks,
    user: dict = Depends(current_user),
):
    url = (payload or {}).get("url")
    folder = docs_svc.clean_folder((payload or {}).get("folder"))
    safe_url = assert_safe_url(url)
    text = await search_svc.fetch_page(safe_url)
    if not text:
        raise HTTPException(400, "could not fetch/extract page")

    doc_id = db.new_id()
    docs_svc.insert_pending(
        doc_id=doc_id, user_id=user["id"], name=safe_url, source=safe_url,
        size=len(text), folder=folder, chat_id=None, status="indexing",
    )
    background.add_task(docs_svc.index_text, doc_id, user["id"], safe_url, text)

    return {
        "id": doc_id, "name": safe_url, "bytes": len(text), "chunks": 0,
        "folder": folder, "status": "indexing",
    }


@router.patch("/{doc_id}")
def update_doc(doc_id: str, payload: dict, user: dict = Depends(current_user)):
    """Rename or move a single document."""
    payload = payload or {}
    docs_svc.update_doc(
        user["id"], doc_id,
        name=payload.get("name") if "name" in payload else None,
        folder=payload["folder"] if "folder" in payload else None,
    )
    return {"ok": True}


@router.delete("/{doc_id}")
def delete_doc(doc_id: str, user: dict = Depends(current_user)):
    docs_svc.delete_doc(user["id"], doc_id)
    return {"ok": True}


@router.post("/{doc_id}/reindex")
def reindex_doc(
    doc_id: str,
    background: BackgroundTasks,
    user: dict = Depends(current_user),
):
    """Retry text extraction + indexing for an existing document.

    Useful for `status='error'` docs (typically image-only PDFs that need
    OCR). The original upload must still be on disk.
    """
    doc = docs_svc.get_doc(user["id"], doc_id)
    if not doc:
        raise HTTPException(404, "not found")
    src = docs_svc.find_upload_path(doc_id)
    if src is None:
        raise HTTPException(
            410,
            "Original file is no longer on disk. Please re-upload the document.",
        )
    docs_svc.mark_indexing(doc_id)
    background.add_task(
        docs_svc.index_file, doc_id, user["id"], doc["name"], src, ocr=True,
    )
    return {"ok": True, "id": doc_id, "status": "indexing"}


# Re-export so existing callers (`from .documents import purge_chat_documents`)
# keep working without touching them.
purge_chat_documents = docs_svc.purge_chat_documents
