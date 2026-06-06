"""RAG: file parsing, chunking, Chroma vector store."""
from __future__ import annotations

import re
from pathlib import Path

import chromadb
from chromadb.config import Settings

from .. import db
from ..config import CHROMA_PATH
from . import embeddings, ollama


_client = chromadb.PersistentClient(
    path=CHROMA_PATH, settings=Settings(anonymized_telemetry=False)
)

# Legacy single collection (pre-multi-model). Kept readable so existing
# documents that were embedded before this refactor remain queryable until
# they are re-embedded. New writes never go here.
_LEGACY_COLLECTION_NAME = "docs"


def _collection_for(model: str, dim: int):
    """Return (or create) the Chroma collection that holds vectors for the
    given (model, dim) pair. Different models/dims live in different
    collections because their vector spaces are not comparable."""
    return _client.get_or_create_collection(
        name=embeddings.collection_name(model, dim),
        metadata={"hnsw:space": "cosine", "embed_model": model, "embed_dim": dim},
    )


def _active_collection():
    """Collection that new uploads go into and queries hit."""
    model = embeddings.active_model()
    dim = embeddings.active_dim()
    if dim is None:
        # First call after install — probe lazily so we don't make ollama
        # calls at import time. Callers that need to embed will trigger this.
        raise RuntimeError(
            "Embedding dimension not yet recorded. The first write will set it."
        )
    return _collection_for(model, dim)


def _collection_for_doc(doc_row: dict | None):
    """Pick the collection a specific document's chunks live in.

    Documents written before the multi-model refactor have NULL embed_model;
    those live in the legacy `docs` collection."""
    if not doc_row or not doc_row.get("embed_model") or not doc_row.get("embed_dim"):
        return _client.get_or_create_collection(
            name=_LEGACY_COLLECTION_NAME, metadata={"hnsw:space": "cosine"}
        )
    return _collection_for(doc_row["embed_model"], int(doc_row["embed_dim"]))


# ---- Extraction ----------------------------------------------------------------
class ExtractionError(Exception):
    """Raised when a file cannot be turned into usable text. The message is
    user-facing and should describe *why* (e.g. encrypted, scanned, empty)."""


def _read_pdf(path: Path) -> str:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError
    try:
        reader = PdfReader(str(path))
    except PdfReadError as e:
        raise ExtractionError(f"PDF is corrupted or not a valid PDF ({e})")
    if getattr(reader, "is_encrypted", False):
        # Try empty password (some PDFs are flagged but unrestricted)
        try:
            if reader.decrypt("") == 0:
                raise ExtractionError("PDF is password-protected. Remove the password and re-upload.")
        except Exception:
            raise ExtractionError("PDF is password-protected. Remove the password and re-upload.")
    pages = []
    for p in reader.pages:
        try:
            pages.append(p.extract_text() or "")
        except Exception:
            pages.append("")
    text = "\n\n".join(pages).strip()
    if not text:
        raise ExtractionError(
            "PDF contains no extractable text (likely a scan or image-only PDF). "
            "Run it through OCR (e.g. `ocrmypdf`) and re-upload."
        )
    return text


def ocr_pdf(src: Path, dst: Path | None = None, lang: str = "eng") -> Path:
    """Run ocrmypdf on src and write a text-layered PDF to dst (defaults to
    src with .ocr.pdf suffix). Raises ExtractionError on failure.
    """
    import shutil
    import subprocess
    if shutil.which("ocrmypdf") is None:
        raise ExtractionError("OCR tooling is not installed on the server.")
    if dst is None:
        dst = src.with_suffix(src.suffix + ".ocr.pdf")
    cmd = [
        "ocrmypdf",
        "--skip-text",          # leave existing text alone
        "--optimize", "0",      # no extra image optimization
        "--quiet",
        "-l", lang,
        str(src), str(dst),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        raise ExtractionError("OCR timed out (>10 min). The PDF may be very large.")
    if proc.returncode != 0:
        # ocrmypdf returns 6 when the input already has text and --skip-text is set
        # — in that case dst still gets written (or src is copied). Treat as success.
        if proc.returncode == 6 and dst.exists():
            return dst
        msg = (proc.stderr or proc.stdout or "").strip().splitlines()[-1] if (proc.stderr or proc.stdout) else "unknown error"
        raise ExtractionError(f"OCR failed: {msg}")
    return dst


def extract_text_with_ocr(path: Path) -> str:
    """Like extract_text, but for PDFs falls back to OCR when no text is found."""
    if path.suffix.lower() != ".pdf":
        return extract_text(path)
    try:
        return _read_pdf(path)
    except ExtractionError as e:
        # Only OCR for the "image-only" case; bubble encryption/corruption errors up.
        if "no extractable text" not in str(e).lower():
            raise
    ocr_path = ocr_pdf(path)
    return _read_pdf(ocr_path)


def _read_docx(path: Path) -> str:
    from docx import Document
    try:
        doc = Document(str(path))
    except Exception as e:
        raise ExtractionError(f"DOCX is corrupted or unreadable ({e})")
    text = "\n".join(p.text for p in doc.paragraphs).strip()
    if not text:
        raise ExtractionError("DOCX contains no readable paragraphs (only images/tables?).")
    return text


def _read_xlsx(path: Path) -> str:
    """Rich text dump of an .xlsx workbook.

    For each sheet we produce:
      - a markdown header with sheet name + dimensions
      - the first non-empty row treated as column headers (when plausible)
      - a TSV-style row dump (values, with formulas inlined as `=FORMULA → value`)
      - a Formulas section listing every cell that contains a formula
      - any defined names (named ranges) at the workbook level

    Cells / rows are capped per sheet to keep large workbooks tractable.
    """
    from openpyxl import load_workbook
    from openpyxl.utils import get_column_letter

    MAX_ROWS_PER_SHEET = 5000
    MAX_COLS_PER_SHEET = 200
    MAX_FORMULAS_PER_SHEET = 500

    try:
        wb_f = load_workbook(str(path), data_only=False)  # formulas
        wb_v = load_workbook(str(path), data_only=True)   # cached results
    except Exception as e:
        raise ExtractionError(f"Excel file is corrupted or unreadable ({e})")

    out: list[str] = []
    out.append(f"# Workbook: {path.name}")
    out.append(f"Sheets: {', '.join(wb_f.sheetnames)}")

    # Workbook-level defined names (named ranges)
    try:
        names = list(wb_f.defined_names)
        if names:
            out.append("")
            out.append("## Defined names")
            for name in names:
                try:
                    dn = wb_f.defined_names[name]
                    refs = ", ".join(d[0] + "!" + d[1] for d in dn.destinations)
                    out.append(f"- `{name}` → {refs}")
                except Exception:
                    out.append(f"- `{name}`")
    except Exception:
        pass

    def _fmt(v) -> str:
        if v is None:
            return ""
        if isinstance(v, float):
            # Trim trailing zeros for cleaner output
            if v.is_integer():
                return str(int(v))
            return f"{v:g}"
        return str(v)

    for sheet_name in wb_f.sheetnames:
        ws_f = wb_f[sheet_name]
        ws_v = wb_v[sheet_name]
        max_row = min(ws_f.max_row or 0, MAX_ROWS_PER_SHEET)
        max_col = min(ws_f.max_column or 0, MAX_COLS_PER_SHEET)

        out.append("")
        out.append(f"## Sheet: {sheet_name}")
        out.append(f"Dimensions: {ws_f.max_row or 0} rows × {ws_f.max_column or 0} cols"
                   + ("  (truncated)" if (ws_f.max_row or 0) > MAX_ROWS_PER_SHEET
                                       or (ws_f.max_column or 0) > MAX_COLS_PER_SHEET else ""))

        # Merged cells summary
        try:
            merges = [str(m) for m in ws_f.merged_cells.ranges]
            if merges:
                shown = ", ".join(merges[:20])
                more = f" (+{len(merges) - 20} more)" if len(merges) > 20 else ""
                out.append(f"Merged ranges: {shown}{more}")
        except Exception:
            pass

        if max_row == 0 or max_col == 0:
            out.append("(empty)")
            continue

        formulas: list[str] = []
        rows_text: list[str] = []
        header: list[str] | None = None

        for r in range(1, max_row + 1):
            row_cells: list[str] = []
            any_value = False
            for c in range(1, max_col + 1):
                cell_f = ws_f.cell(row=r, column=c)
                cell_v = ws_v.cell(row=r, column=c)
                fv = cell_f.value
                vv = cell_v.value
                # Detect formula
                if isinstance(fv, str) and fv.startswith("="):
                    coord = f"{get_column_letter(c)}{r}"
                    if len(formulas) < MAX_FORMULAS_PER_SHEET:
                        formulas.append(f"{coord}: {fv}  →  {_fmt(vv)}")
                    text = f"{fv} → {_fmt(vv)}"
                else:
                    text = _fmt(vv if vv is not None else fv)
                if text:
                    any_value = True
                row_cells.append(text)
            if any_value:
                if header is None and any(c.strip() for c in row_cells):
                    header = row_cells[:]
                rows_text.append("\t".join(row_cells))

        if header is not None and rows_text:
            # Emit a header line followed by the rest as TSV. The first row
            # appears in both the header listing and the table for clarity.
            out.append(f"Columns: {', '.join(h for h in header if h)}")
            out.append("")
            out.append("\t".join(header))
            for row in rows_text[1:]:
                out.append(row)
        else:
            out.extend(rows_text)

        if formulas:
            out.append("")
            out.append(f"### Formulas in {sheet_name} ({len(formulas)}"
                       + (f"+, truncated to {MAX_FORMULAS_PER_SHEET}" if len(formulas) >= MAX_FORMULAS_PER_SHEET else "")
                       + ")")
            out.extend(formulas)

    text = "\n".join(out).strip()
    if not text or text.endswith(":"):
        raise ExtractionError("Excel workbook contains no readable cells.")
    return text


def _read_xls(path: Path) -> str:
    """Legacy .xls via xlrd (xlrd 2.x is .xls-only)."""
    import xlrd
    try:
        book = xlrd.open_workbook(str(path), formatting_info=False)
    except Exception as e:
        raise ExtractionError(f"Legacy XLS file is corrupted or unreadable ({e})")
    out: list[str] = [f"# Workbook: {path.name}",
                      f"Sheets: {', '.join(book.sheet_names())}"]
    for sh in book.sheets():
        out.append("")
        out.append(f"## Sheet: {sh.name}")
        out.append(f"Dimensions: {sh.nrows} rows × {sh.ncols} cols")
        header_done = False
        for r in range(sh.nrows):
            cells = []
            for c in range(sh.ncols):
                v = sh.cell_value(r, c)
                if isinstance(v, float) and v.is_integer():
                    v = int(v)
                cells.append("" if v == "" else str(v))
            if not any(s.strip() for s in cells):
                continue
            if not header_done:
                out.append(f"Columns: {', '.join(s for s in cells if s)}")
                out.append("")
                header_done = True
            out.append("\t".join(cells))
    text = "\n".join(out).strip()
    if not text:
        raise ExtractionError("XLS workbook contains no readable cells.")
    return text


def _read_pptx(path: Path) -> str:
    from pptx import Presentation
    try:
        prs = Presentation(str(path))
    except Exception as e:
        raise ExtractionError(f"PPTX is corrupted or unreadable ({e})")
    out: list[str] = []
    for i, slide in enumerate(prs.slides, 1):
        out.append(f"# Slide {i}")
        for shape in slide.shapes:
            if shape.has_text_frame:
                for p in shape.text_frame.paragraphs:
                    line = "".join(run.text for run in p.runs).strip()
                    if line:
                        out.append(line)
            elif getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    cells = [c.text.strip() for c in row.cells]
                    if any(cells):
                        out.append("\t".join(cells))
        # Speaker notes
        try:
            notes = (slide.notes_slide.notes_text_frame.text or "").strip() if slide.has_notes_slide else ""
            if notes:
                out.append(f"(notes) {notes}")
        except Exception:
            pass
        out.append("")
    text = "\n".join(out).strip()
    if not text:
        raise ExtractionError("PPTX contains no readable text.")
    return text


def _read_rtf(path: Path) -> str:
    from striprtf.striprtf import rtf_to_text
    try:
        text = rtf_to_text(path.read_text(encoding="utf-8", errors="ignore")).strip()
    except Exception as e:
        raise ExtractionError(f"RTF is corrupted or unreadable ({e})")
    if not text:
        raise ExtractionError("RTF contains no readable text.")
    return text


def _read_odf(path: Path) -> str:
    """odt / ods / odp via odfpy."""
    from odf.opendocument import load
    from odf.text import P, H
    from odf.table import Table, TableRow, TableCell
    try:
        doc = load(str(path))
    except Exception as e:
        raise ExtractionError(f"OpenDocument file is corrupted or unreadable ({e})")

    def _node_text(node) -> str:
        return "".join(
            t.data for t in node.childNodes if t.nodeType == 3
        ) + "".join(_node_text(c) for c in node.childNodes if c.nodeType == 1)

    parts: list[str] = []
    # Paragraphs and headings
    for el in doc.getElementsByType(H) + doc.getElementsByType(P):
        s = _node_text(el).strip()
        if s:
            parts.append(s)
    # Tables (spreadsheets)
    for tbl in doc.getElementsByType(Table):
        parts.append(f"# Table: {tbl.getAttribute('name') or ''}")
        for row in tbl.getElementsByType(TableRow):
            cells = [_node_text(c).strip() for c in row.getElementsByType(TableCell)]
            if any(cells):
                parts.append("\t".join(cells))
    text = "\n".join(parts).strip()
    if not text:
        raise ExtractionError("OpenDocument file contains no readable text.")
    return text


def _read_epub(path: Path) -> str:
    import ebooklib
    from ebooklib import epub
    from bs4 import BeautifulSoup
    try:
        book = epub.read_epub(str(path))
    except Exception as e:
        raise ExtractionError(f"EPUB is corrupted or unreadable ({e})")
    parts: list[str] = []
    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        soup = BeautifulSoup(item.get_content(), "html.parser")
        s = soup.get_text("\n", strip=True)
        if s:
            parts.append(s)
    text = "\n\n".join(parts).strip()
    if not text:
        raise ExtractionError("EPUB contains no readable text.")
    return text


def _read_html(path: Path) -> str:
    from bs4 import BeautifulSoup
    raw = path.read_text(encoding="utf-8", errors="ignore")
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    if not text:
        raise ExtractionError("HTML contains no readable text.")
    return text


def _read_email(path: Path) -> str:
    """.eml plain-text email."""
    from email import policy
    from email.parser import BytesParser
    try:
        msg = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
    except Exception as e:
        raise ExtractionError(f"Email file is corrupted or unreadable ({e})")
    headers = []
    for h in ("From", "To", "Cc", "Subject", "Date"):
        v = msg.get(h)
        if v:
            headers.append(f"{h}: {v}")
    body = ""
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is not None:
            content = part.get_content()
            if part.get_content_type() == "text/html":
                from bs4 import BeautifulSoup
                content = BeautifulSoup(content, "html.parser").get_text("\n", strip=True)
            body = content.strip()
    except Exception:
        body = ""
    text = ("\n".join(headers) + "\n\n" + body).strip()
    if not text:
        raise ExtractionError("Email contains no readable text.")
    return text


def _read_msg(path: Path) -> str:
    """Outlook .msg via extract-msg."""
    import extract_msg
    try:
        m = extract_msg.Message(str(path))
    except Exception as e:
        raise ExtractionError(f"Outlook .msg is corrupted or unreadable ({e})")
    headers = []
    for h, v in (("From", m.sender), ("To", m.to), ("Cc", m.cc),
                 ("Subject", m.subject), ("Date", str(m.date or ""))):
        if v:
            headers.append(f"{h}: {v}")
    body = (m.body or "").strip()
    text = ("\n".join(headers) + "\n\n" + body).strip()
    if not text:
        raise ExtractionError("Outlook .msg contains no readable text.")
    return text


def _read_ipynb(path: Path) -> str:
    import json as _json
    try:
        nb = _json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception as e:
        raise ExtractionError(f"Notebook is corrupted or unreadable ({e})")
    parts: list[str] = []
    for cell in nb.get("cells", []):
        src = cell.get("source", "")
        if isinstance(src, list):
            src = "".join(src)
        src = (src or "").strip()
        if not src:
            continue
        if cell.get("cell_type") == "code":
            parts.append(f"```python\n{src}\n```")
        else:
            parts.append(src)
    text = "\n\n".join(parts).strip()
    if not text:
        raise ExtractionError("Notebook contains no readable cells.")
    return text


_TEXT_EXTS = {".txt", ".md", ".markdown", ".csv", ".tsv", ".log", ".json",
              ".yaml", ".yml", ".py", ".js", ".ts", ".tsx", ".jsx",
              ".css", ".scss", ".rs", ".go", ".java", ".c", ".cc", ".cpp",
              ".h", ".hpp", ".rb", ".php", ".sh", ".bash", ".zsh",
              ".toml", ".ini", ".cfg", ".env", ".xml", ".sql",
              ".swift", ".kt", ".m", ".mm", ".dart", ".lua", ".r", ".jl",
              ".tex", ".srt", ".vtt"}


_HTML_EXTS = {".html", ".htm", ".xhtml"}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    if size == 0:
        raise ExtractionError("File is empty (0 bytes).")
    if suffix == ".pdf":
        return _read_pdf(path)
    if suffix == ".docx":
        return _read_docx(path)
    if suffix == ".xlsx":
        return _read_xlsx(path)
    if suffix == ".xls":
        return _read_xls(path)
    if suffix == ".pptx":
        return _read_pptx(path)
    if suffix == ".rtf":
        return _read_rtf(path)
    if suffix in {".odt", ".ods", ".odp"}:
        return _read_odf(path)
    if suffix == ".epub":
        return _read_epub(path)
    if suffix == ".ipynb":
        return _read_ipynb(path)
    if suffix == ".eml":
        return _read_email(path)
    if suffix == ".msg":
        return _read_msg(path)
    if suffix in _HTML_EXTS:
        return _read_html(path)
    if suffix in _IMAGE_EXTS:
        return _ocr_image(path)
    if suffix in _TEXT_EXTS:
        text = path.read_text(encoding="utf-8", errors="ignore").strip()
        if not text:
            raise ExtractionError("File contains no readable text.")
        return text
    # Unknown extension — best-effort utf-8 read.
    try:
        text = path.read_text(encoding="utf-8", errors="ignore").strip()
    except Exception as e:
        raise ExtractionError(f"Cannot read file ({e}).")
    if not text:
        raise ExtractionError(
            f"Unsupported file type ({suffix or 'no extension'}). "
            "Supported: PDF, DOCX, XLSX, XLS, PPTX, ODT/ODS/ODP, RTF, EPUB, "
            "HTML, EML/MSG, images (with OCR), code, and plain-text formats."
        )
    return text


def _ocr_image(path: Path) -> str:
    """Run tesseract directly on an image and return its text."""
    import shutil
    import subprocess
    if shutil.which("tesseract") is None:
        raise ExtractionError("OCR tooling (tesseract) is not installed on the server.")
    try:
        proc = subprocess.run(
            ["tesseract", str(path), "-", "-l", "eng"],
            capture_output=True, text=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        raise ExtractionError("OCR timed out (>5 min) on image.")
    if proc.returncode != 0:
        msg = (proc.stderr or "").strip().splitlines()[-1] if proc.stderr else "unknown error"
        raise ExtractionError(f"Image OCR failed: {msg}")
    text = (proc.stdout or "").strip()
    if not text:
        raise ExtractionError("Image OCR produced no text (low contrast or no text in image).")
    return text


# ---- Chunking ------------------------------------------------------------------
def chunk_text(text: str, chunk_size: int = 900, overlap: int = 150) -> list[str]:
    text = re.sub(r"\s+\n", "\n", text or "").strip()
    if not text:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for p in paragraphs:
        if len(buf) + len(p) + 2 <= chunk_size:
            buf = f"{buf}\n\n{p}".strip()
        else:
            if buf:
                chunks.append(buf)
            if len(p) <= chunk_size:
                buf = p
            else:
                for i in range(0, len(p), chunk_size - overlap):
                    chunks.append(p[i:i + chunk_size])
                buf = ""
    if buf:
        chunks.append(buf)
    if overlap > 0 and len(chunks) > 1:
        out = [chunks[0]]
        for i in range(1, len(chunks)):
            tail = chunks[i - 1][-overlap:]
            out.append((tail + " " + chunks[i]).strip())
        chunks = out
    return chunks


# ---- Index API -----------------------------------------------------------------
async def add_document(doc_id: str, user_id: str, name: str, text: str) -> int:
    chunks = chunk_text(text)
    if not chunks:
        return 0
    model = embeddings.active_model()
    embs = await ollama.embed(chunks, model=model)
    # Discover dim from the first vector we actually got back. On a fresh
    # install this is the only place that records the dimension; afterwards
    # `embeddings.active_dim()` returns it directly.
    dim = len(embs[0]) if embs and embs[0] else 0
    if embeddings.active_dim() is None and dim > 0:
        embeddings.set_active(model, dim)
    coll = _collection_for(model, dim or embeddings.active_dim() or 0)
    ids = [f"{doc_id}:{i}" for i in range(len(chunks))]
    metadatas = [{"doc_id": doc_id, "user_id": user_id, "name": name, "chunk": i}
                 for i in range(len(chunks))]
    coll.add(ids=ids, documents=chunks, embeddings=embs, metadatas=metadatas)
    # Stamp the document row so we know which collection to query / re-embed.
    with db.connect() as cx:
        cx.execute(
            "UPDATE documents SET embed_model=?, embed_dim=? WHERE id=?",
            (model, dim, doc_id),
        )
    return len(chunks)


def _doc_row(doc_id: str) -> dict | None:
    with db.connect() as cx:
        row = cx.execute(
            "SELECT id, embed_model, embed_dim FROM documents WHERE id=?", (doc_id,)
        ).fetchone()
    return db.row_to_dict(row) if row else None


def delete_document(doc_id: str) -> None:
    coll = _collection_for_doc(_doc_row(doc_id))
    coll.delete(where={"doc_id": doc_id})


def get_full_documents(doc_ids: list[str]) -> list[dict]:
    """Return every chunk of the given documents, in order, joined per
    document. Result: list of {doc_id, name, text} — one entry per doc.
    Used when the chat layer wants to feed entire documents to the model
    rather than top-k embedding hits (e.g. small folder-scoped queries
    where comprehensive context beats relevance ranking)."""
    if not doc_ids:
        return []
    # Documents may live in different collections (different embed models).
    # Group by collection and fetch each separately.
    rows_by_doc: dict[str, dict] = {}
    with db.connect() as cx:
        for r in cx.execute(
            f"SELECT id, embed_model, embed_dim FROM documents "
            f"WHERE id IN ({','.join('?' * len(doc_ids))})", tuple(doc_ids)
        ).fetchall():
            rows_by_doc[r["id"]] = db.row_to_dict(r)

    by_coll: dict[str, list[str]] = {}
    for did in doc_ids:
        row = rows_by_doc.get(did)
        if row and row.get("embed_model") and row.get("embed_dim"):
            key = embeddings.collection_name(row["embed_model"], int(row["embed_dim"]))
        else:
            key = _LEGACY_COLLECTION_NAME
        by_coll.setdefault(key, []).append(did)

    by_doc: dict[str, dict] = {}
    for coll_name, dids in by_coll.items():
        if coll_name == _LEGACY_COLLECTION_NAME:
            coll = _client.get_or_create_collection(
                name=_LEGACY_COLLECTION_NAME, metadata={"hnsw:space": "cosine"}
            )
        else:
            # rows_by_doc has the model/dim for one of these ids
            sample = next(rows_by_doc[d] for d in dids if d in rows_by_doc)
            coll = _collection_for(sample["embed_model"], int(sample["embed_dim"]))
        res = coll.get(where={"doc_id": {"$in": dids}})
        docs = res.get("documents") or []
        metas = res.get("metadatas") or []
        for d, m in zip(docs, metas):
            did = m.get("doc_id")
            if not did:
                continue
            bucket = by_doc.setdefault(did, {"doc_id": did,
                                             "name": m.get("name"),
                                             "chunks": []})
            bucket["chunks"].append((int(m.get("chunk", 0)), d))

    out: list[dict] = []
    for did in doc_ids:
        b = by_doc.get(did)
        if not b:
            continue
        b["chunks"].sort(key=lambda x: x[0])
        text = "\n\n".join(c[1] for c in b["chunks"])
        out.append({"doc_id": did, "name": b["name"], "text": text})
    return out


async def query(text: str, user_id: str, k: int = 4,
                doc_ids: list[str] | None = None) -> list[dict]:
    model = embeddings.active_model()
    dim = embeddings.active_dim()
    if dim is None:
        return []
    coll = _collection_for(model, dim)
    if coll.count() == 0:
        return []
    emb = await ollama.embed([text], model=model)
    where: dict = {"user_id": user_id}
    if doc_ids:
        where = {"$and": [{"user_id": user_id}, {"doc_id": {"$in": doc_ids}}]}
    res = coll.query(query_embeddings=emb, n_results=k, where=where)
    hits = []
    for d, m, dist in zip(
        res.get("documents", [[]])[0],
        res.get("metadatas", [[]])[0],
        res.get("distances", [[]])[0],
    ):
        hits.append({"text": d, "meta": m, "score": 1.0 - float(dist)})
    return hits


# ---- Re-embed --------------------------------------------------------------

async def reembed_document(doc_id: str, *, old_model: str = "") -> int:
    """Move a single document from its current collection into the
    currently-active embedding model's collection. Returns the number of
    chunks moved. Used by the admin "switch embedding model" flow."""
    with db.connect() as cx:
        row = cx.execute(
            "SELECT id, embed_model, embed_dim FROM documents WHERE id=?",
            (doc_id,),
        ).fetchone()
    if not row:
        return 0
    row = db.row_to_dict(row)
    new_model = embeddings.active_model()
    new_dim = embeddings.active_dim()
    if new_dim is None:
        # Need to discover it on the first chunk; do that inside the loop.
        new_dim = 0

    old_coll = _collection_for_doc(row)
    data = old_coll.get(where={"doc_id": doc_id})
    ids = data.get("ids") or []
    docs = data.get("documents") or []
    metas = data.get("metadatas") or []
    if not docs:
        # Nothing in the old collection — just stamp the row so queries hit
        # the right (empty) place going forward.
        with db.connect() as cx:
            cx.execute(
                "UPDATE documents SET embed_model=?, embed_dim=? WHERE id=?",
                (new_model, new_dim or 0, doc_id),
            )
        return 0

    new_embs = await ollama.embed(docs, model=new_model)
    if not new_dim and new_embs and new_embs[0]:
        new_dim = len(new_embs[0])
        embeddings.set_active(new_model, new_dim)
    new_coll = _collection_for(new_model, new_dim)
    # Re-use the same chunk ids — they're scoped per doc_id so collisions
    # only happen if we'd double-add, which we prevent by upserting (Chroma
    # `add` rejects dups; `upsert` overwrites).
    new_coll.upsert(ids=ids, documents=docs, embeddings=new_embs, metadatas=metas)
    # Drop from old collection only after successful write to new.
    try:
        old_coll.delete(ids=ids)
    except Exception:
        pass
    with db.connect() as cx:
        cx.execute(
            "UPDATE documents SET embed_model=?, embed_dim=? WHERE id=?",
            (new_model, new_dim, doc_id),
        )
    return len(docs)
