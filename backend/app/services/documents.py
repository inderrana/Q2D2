"""Document business logic — extraction, indexing, folders, deletion.

Routers should only handle HTTP concerns (auth, parsing, status codes) and
delegate the actual work here. The indexing pipeline can run synchronously
(returning a fully indexed row) or asynchronously via FastAPI BackgroundTasks
(returning a row with status='indexing' that the client polls).
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import HTTPException

from .. import db
from ..config import UPLOAD_DIR
from . import rag

log = logging.getLogger(__name__)


# ----- folder helpers --------------------------------------------------

def clean_folder(raw: str | None) -> str:
    """Normalize folder path: collapse slashes, strip leading/trailing, ban '..'."""
    if not raw:
        return ""
    parts = [p.strip() for p in str(raw).replace("\\", "/").split("/")]
    parts = [p for p in parts if p and p not in (".", "..")]
    return "/".join(parts)


def ancestors(path: str) -> list[str]:
    """All ancestors of a folder path including itself (excluding root '')."""
    if not path:
        return []
    parts = path.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]


def ensure_folder(cx, user_id: str, path: str) -> None:
    """Insert (user_id, ancestor) rows for every ancestor of `path`."""
    for anc in ancestors(path):
        cx.execute(
            "INSERT OR IGNORE INTO doc_folders (user_id, path, created_at) VALUES (?, ?, ?)",
            (user_id, anc, db.now()),
        )


def list_folders(user_id: str) -> list[str]:
    """Union of explicit folders + folders implied by documents, with ancestors."""
    with db.connect() as cx:
        explicit = {r[0] for r in cx.execute(
            "SELECT path FROM doc_folders WHERE user_id=?", (user_id,)
        ).fetchall()}
        from_docs = {r[0] for r in cx.execute(
            "SELECT DISTINCT folder FROM documents WHERE user_id=? AND folder<>''",
            (user_id,),
        ).fetchall()}
    out: set[str] = set()
    for p in explicit | from_docs:
        out.update(ancestors(p))
    return sorted(out)


# ----- listings --------------------------------------------------------

def list_user_documents(user_id: str, *, limit: int = 200,
                        before: int | None = None) -> list[dict]:
    limit = max(1, min(limit, 500))
    sql = ("SELECT id, name, source, bytes, chunks, folder, status, error, created_at "
           "FROM documents WHERE user_id=? AND chat_id IS NULL")
    args: list = [user_id]
    if before is not None:
        sql += " AND created_at < ?"
        args.append(int(before))
    sql += " ORDER BY folder ASC, created_at DESC LIMIT ?"
    args.append(limit)
    with db.connect() as cx:
        rows = cx.execute(sql, tuple(args)).fetchall()
    return [db.row_to_dict(r) for r in rows]


def get_doc(user_id: str, doc_id: str) -> dict | None:
    with db.connect() as cx:
        row = cx.execute(
            "SELECT id, name, source, bytes, chunks, folder, chat_id, status, error, created_at "
            "FROM documents WHERE id=? AND user_id=?",
            (doc_id, user_id),
        ).fetchone()
    return db.row_to_dict(row) if row else None


# ----- folder mutations ------------------------------------------------

def create_folder(user_id: str, path: str) -> str:
    path = clean_folder(path)
    if not path:
        raise HTTPException(400, "path required")
    with db.connect() as cx:
        ensure_folder(cx, user_id, path)
    return path


def rename_folder(user_id: str, src: str, dst: str) -> dict:
    src = clean_folder(src)
    dst = clean_folder(dst)
    if not src or not dst:
        raise HTTPException(400, "from/to required")
    if src == dst:
        return {"ok": True, "from": src, "to": dst}
    with db.connect() as cx:
        cx.execute(
            "UPDATE documents SET folder = ? || SUBSTR(folder, ?) "
            "WHERE user_id=? AND (folder=? OR folder LIKE ?)",
            (dst, len(src) + 1, user_id, src, src + "/%"),
        )
        cx.execute(
            "UPDATE doc_folders SET path = ? || SUBSTR(path, ?) "
            "WHERE user_id=? AND (path=? OR path LIKE ?)",
            (dst, len(src) + 1, user_id, src, src + "/%"),
        )
        ensure_folder(cx, user_id, dst)
    return {"ok": True, "from": src, "to": dst}


def delete_folder(user_id: str, path: str) -> int:
    path = clean_folder(path)
    if not path:
        raise HTTPException(400, "path required")
    with db.connect() as cx:
        rows = cx.execute(
            "SELECT id FROM documents WHERE user_id=? AND (folder=? OR folder LIKE ?)",
            (user_id, path, path + "/%"),
        ).fetchall()
    for r in rows:
        try:
            rag.delete_document(r["id"])
        except Exception:
            log.exception("chroma delete failed for %s", r["id"])
    with db.connect() as cx:
        cx.execute(
            "DELETE FROM documents WHERE user_id=? AND (folder=? OR folder LIKE ?)",
            (user_id, path, path + "/%"),
        )
        cx.execute(
            "DELETE FROM doc_folders WHERE user_id=? AND (path=? OR path LIKE ?)",
            (user_id, path, path + "/%"),
        )
    return len(rows)


# ----- doc mutations ---------------------------------------------------

def update_doc(user_id: str, doc_id: str, *, name: str | None = None,
               folder: str | None = None) -> None:
    with db.connect() as cx:
        owned = cx.execute(
            "SELECT id FROM documents WHERE id=? AND user_id=?", (doc_id, user_id),
        ).fetchone()
    if not owned:
        raise HTTPException(404, "not found")
    sets: list[str] = []
    params: list = []
    if name is not None:
        sets.append("name=?"); params.append(str(name))
    if folder is not None:
        folder = clean_folder(folder)
        sets.append("folder=?"); params.append(folder)
    if not sets:
        return
    params += [doc_id, user_id]
    with db.connect() as cx:
        cx.execute(f"UPDATE documents SET {', '.join(sets)} WHERE id=? AND user_id=?", params)
        if folder:
            ensure_folder(cx, user_id, folder)


def delete_doc(user_id: str, doc_id: str) -> None:
    with db.connect() as cx:
        owned = cx.execute(
            "SELECT id FROM documents WHERE id=? AND user_id=?", (doc_id, user_id),
        ).fetchone()
    if not owned:
        raise HTTPException(404, "not found")
    try:
        rag.delete_document(doc_id)
    except Exception:
        log.exception("chroma delete failed for %s", doc_id)
    for p in Path(UPLOAD_DIR).glob(f"{doc_id}_*"):
        try:
            p.unlink()
        except Exception:
            pass
    with db.connect() as cx:
        cx.execute("DELETE FROM documents WHERE id=?", (doc_id,))


def purge_chat_documents(chat_id: str) -> int:
    """Cascade-delete chroma vectors and uploaded files for every doc attached
    to a chat. Must run *before* the chat row is deleted (FK cascade hides ids)."""
    with db.connect() as cx:
        rows = cx.execute(
            "SELECT id FROM documents WHERE chat_id=?", (chat_id,)
        ).fetchall()
    for r in rows:
        did = r["id"]
        try:
            rag.delete_document(did)
        except Exception:
            log.exception("chroma delete failed for %s", did)
        for p in Path(UPLOAD_DIR).glob(f"{did}_*"):
            try:
                p.unlink()
            except Exception:
                pass
    return len(rows)


# ----- ingestion pipeline ---------------------------------------------

def insert_pending(*, doc_id: str, user_id: str, name: str, source: str,
                   size: int, folder: str, chat_id: str | None,
                   status: str = "indexing") -> None:
    with db.connect() as cx:
        cx.execute(
            "INSERT INTO documents (id, user_id, name, source, bytes, chunks, "
            "folder, chat_id, status, error, created_at) "
            "VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, NULL, ?)",
            (doc_id, user_id, name, source, size, folder, chat_id, status, db.now()),
        )
        if folder:
            ensure_folder(cx, user_id, folder)


def mark_indexed(doc_id: str, chunks: int) -> None:
    with db.connect() as cx:
        cx.execute(
            "UPDATE documents SET status='ready', chunks=?, error=NULL WHERE id=?",
            (chunks, doc_id),
        )


def mark_error(doc_id: str, err: str) -> None:
    with db.connect() as cx:
        cx.execute(
            "UPDATE documents SET status='error', error=? WHERE id=?",
            (err[:500], doc_id),
        )


def mark_indexing(doc_id: str) -> None:
    with db.connect() as cx:
        cx.execute(
            "UPDATE documents SET status='indexing', error=NULL WHERE id=?",
            (doc_id,),
        )


async def index_file(doc_id: str, user_id: str, name: str, file_path: Path,
                     *, ocr: bool = True) -> None:
    """Background task: extract text + push chunks to Chroma; record status.

    OCR fallback is on by default — `extract_text_with_ocr` only invokes
    OCR when normal text extraction yields nothing (image-only PDFs), so
    text PDFs incur no extra cost.
    """
    try:
        extractor = rag.extract_text_with_ocr if ocr else rag.extract_text
        text = extractor(file_path)
        if not text.strip():
            raise rag.ExtractionError("could not extract text from file")
        n = await rag.add_document(doc_id, user_id, name, text)
        mark_indexed(doc_id, n)
    except rag.ExtractionError as e:
        # Keep the file on disk so the user can retry indexing (e.g. after
        # adding more language packs, or via the manual reindex endpoint)
        # without re-uploading. `delete_doc` cleans it up if the user
        # eventually removes the document.
        mark_error(doc_id, str(e))
    except Exception as e:
        log.exception("indexing failed for %s", doc_id)
        mark_error(doc_id, str(e))


def find_upload_path(doc_id: str) -> Path | None:
    """Locate the on-disk upload for a document id, if still present."""
    matches = list(Path(UPLOAD_DIR).glob(f"{doc_id}_*"))
    return matches[0] if matches else None


async def index_text(doc_id: str, user_id: str, name: str, text: str) -> None:
    """Background task for URL ingest (text already extracted)."""
    try:
        if not text.strip():
            raise ValueError("empty page")
        n = await rag.add_document(doc_id, user_id, name, text)
        mark_indexed(doc_id, n)
    except Exception as e:
        log.exception("indexing failed for %s", doc_id)
        mark_error(doc_id, str(e))
