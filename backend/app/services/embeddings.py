"""Embedding model registry + runtime selection.

Architecture notes (read this before changing how vectors are stored):

- Each embedding model produces vectors in its own dimension and its own
  semantic space. Vectors from different models are **not comparable** —
  you cannot mix them in a single nearest-neighbour query.
- We therefore use one Chroma collection **per (model, dim)** pair, named
  `docs__<sanitized_model>__<dim>`. The "active" model is what new uploads
  embed against and what queries hit. Old collections stick around until
  re-embed completes (or an admin deletes them).
- The active model is stored in the `app_settings` row `embed_model`. On
  first boot we fall back to the branding default (`branding.toml`).
- Changing the active model is an admin operation that triggers a
  re-embed of every existing document. Status is exposed via
  `reembed_status()` so the UI can poll progress.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .. import db
from ..config import EMBED_MODEL as _DEFAULT_EMBED_MODEL
from . import ollama


# --- Curated list ---------------------------------------------------------

# Tier-1: vetted, Ollama-native, broadly recommended. The first entry is the
# UI's default highlight. `dim` is informational — at apply time we always
# probe the model and use the actual response length, so a wrong number here
# does not corrupt the index.
CURATED_EMBED_MODELS: list[dict[str, Any]] = [
    {
        "id": "embeddinggemma",
        "label": "EmbeddingGemma 300M",
        "dim": 768,
        "ctx": 2048,
        "size_mb": 622,
        "license": "Apache-2.0",
        "summary": "Google · fast, CPU-friendly, multilingual. Recommended default.",
        "recommended": True,
    },
    {
        "id": "qwen3-embedding:0.6b",
        "label": "Qwen3-Embedding 0.6B",
        "dim": 1024,
        "ctx": 32768,
        "size_mb": 639,
        "license": "Apache-2.0",
        "summary": "Alibaba · top quality-per-MB, long context, multilingual.",
    },
    {
        "id": "bge-m3",
        "label": "BGE-M3",
        "dim": 1024,
        "ctx": 8192,
        "size_mb": 1200,
        "license": "MIT",
        "summary": "BAAI · hybrid (dense + sparse) ready, 100+ languages.",
    },
    {
        "id": "snowflake-arctic-embed2",
        "label": "Arctic-Embed 2.0 L",
        "dim": 1024,
        "ctx": 8192,
        "size_mb": 1200,
        "license": "Apache-2.0",
        "summary": "Snowflake · strong on long docs, multilingual.",
    },
    {
        "id": "mxbai-embed-large",
        "label": "mxbai-embed-large",
        "dim": 1024,
        "ctx": 512,
        "size_mb": 670,
        "license": "Apache-2.0",
        "summary": "Mixedbread · classic, English-focused, short-context.",
    },
    {
        "id": "nomic-embed-text",
        "label": "Nomic Embed Text v1.5",
        "dim": 768,
        "ctx": 8192,
        "size_mb": 274,
        "license": "Apache-2.0",
        "summary": "Nomic · legacy default, small and fast.",
    },
]


# --- app_settings helpers (key-value) -------------------------------------

def _settings_get(key: str) -> str | None:
    with db.connect() as cx:
        row = cx.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else None


def _settings_set(key: str, value: str) -> None:
    with db.connect() as cx:
        cx.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, db.now()),
        )


# --- Active model -----------------------------------------------------------

def active_model() -> str:
    """The currently-active embedding model id."""
    return _settings_get("embed_model") or _DEFAULT_EMBED_MODEL


def active_dim() -> int | None:
    raw = _settings_get("embed_dim")
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def set_active(model: str, dim: int) -> None:
    _settings_set("embed_model", model)
    _settings_set("embed_dim", str(int(dim)))


# --- Collection naming ------------------------------------------------------

_SAFE = re.compile(r"[^a-z0-9_-]")


def collection_name(model: str, dim: int) -> str:
    """Chroma collection name for a (model, dim) pair.

    Chroma allows 3-512 chars of `[a-zA-Z0-9._-]`, must start/end alnum.
    We lowercase, replace anything else with `_`, and prefix with `docs__`.
    """
    safe = _SAFE.sub("_", model.lower())
    safe = safe.strip("_-") or "default"
    return f"docs__{safe}__{int(dim)}"


# --- Probing / validation ---------------------------------------------------

class EmbedProbeError(Exception):
    """Raised when a model id cannot be validated as an embedding model."""


async def probe_dim(model: str) -> int:
    """Ask Ollama to embed a tiny string and return the vector length.

    Raises EmbedProbeError with a user-facing message if the model is not
    pulled, isn't an embedding model, or Ollama returns an unexpected shape.
    """
    try:
        vecs = await ollama.embed(["probe"], model=model)
    except Exception as e:  # network, 404, etc.
        # ollama.embed raises httpx.HTTPStatusError on non-2xx — surface its
        # body if we can read it.
        msg = str(e)
        if "404" in msg:
            raise EmbedProbeError(
                f"Model '{model}' is not pulled. Pull it first via Settings → Models."
            )
        raise EmbedProbeError(f"Ollama rejected '{model}': {msg}")
    if not vecs or not isinstance(vecs, list) or not vecs[0]:
        raise EmbedProbeError(
            f"'{model}' did not return an embedding. Is it an embedding model?"
        )
    dim = len(vecs[0])
    if dim < 8 or dim > 8192:
        raise EmbedProbeError(
            f"'{model}' returned a vector of length {dim}, which looks wrong."
        )
    return dim


# --- Re-embed job tracking --------------------------------------------------
#
# Single-process FastAPI → an in-memory dict is fine. If we ever go multi-
# worker we'll need to persist this in app_settings. We keep at most one
# job alive at a time; starting a new one while another runs is rejected.

@dataclass
class ReembedStatus:
    state: str = "idle"              # idle | running | done | error
    from_model: str = ""
    to_model: str = ""
    to_dim: int = 0
    total: int = 0                   # documents to process
    done: int = 0                    # documents finished
    chunks_done: int = 0
    started_at: int = 0
    finished_at: int = 0
    error: str = ""
    failed_docs: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "from_model": self.from_model,
            "to_model": self.to_model,
            "to_dim": self.to_dim,
            "total": self.total,
            "done": self.done,
            "chunks_done": self.chunks_done,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "failed_docs": list(self.failed_docs),
        }


_status = ReembedStatus()
_status_lock = asyncio.Lock()
_current_task: asyncio.Task | None = None


def reembed_status() -> dict:
    return _status.as_dict()


async def start_reembed(to_model: str, to_dim: int) -> dict:
    """Kick off a background re-embed of every document.

    Returns the current status snapshot. Rejects with RuntimeError if a job
    is already running.
    """
    global _current_task, _status
    async with _status_lock:
        if _status.state == "running":
            raise RuntimeError("A re-embed job is already running.")
        from_model = active_model()
        # Update active model first so new uploads start using it immediately.
        # Existing docs will be queryable against the OLD collection until
        # their row's embed_model column flips — see rag._collection_for_doc.
        set_active(to_model, to_dim)

        with db.connect() as cx:
            rows = cx.execute(
                "SELECT id, name, user_id, embed_model, chunks "
                "FROM documents WHERE status='ready' AND chunks > 0"
            ).fetchall()
        total = len(rows)
        _status = ReembedStatus(
            state="running",
            from_model=from_model,
            to_model=to_model,
            to_dim=to_dim,
            total=total,
            started_at=db.now(),
        )
        _current_task = asyncio.create_task(_reembed_worker([dict(r) for r in rows]))
    return _status.as_dict()


async def _reembed_worker(docs: list[dict]) -> None:
    from . import rag  # late import to avoid cycle

    for d in docs:
        doc_id = d["id"]
        old_model = d.get("embed_model") or ""
        try:
            n = await rag.reembed_document(doc_id, old_model=old_model)
            _status.chunks_done += n
        except Exception as e:
            _status.failed_docs.append({"id": doc_id, "name": d["name"], "error": str(e)[:200]})
        _status.done += 1

    _status.state = "done"
    _status.finished_at = db.now()
