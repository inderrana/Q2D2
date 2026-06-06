"""Sanity check the per-format text extractor.

Doesn't exercise embeddings or Chroma — that requires a running Ollama and a
populated network. We just verify `extract_text` handles each in-tree format
without crashing and produces non-empty output for trivial inputs.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_extract_plaintext(tmp_path, app_client):
    from app.services import rag
    p = _write(tmp_path, "hello.txt", b"hello world\nsecond line")
    out = rag.extract_text(p)
    assert "hello world" in out


def test_extract_markdown(tmp_path, app_client):
    from app.services import rag
    p = _write(tmp_path, "doc.md", b"# Title\n\nSome **bold** text.")
    out = rag.extract_text(p)
    assert "Title" in out and "bold" in out


def test_extract_json(tmp_path, app_client):
    from app.services import rag
    payload = json.dumps({"a": 1, "msg": "needle-in-haystack"})
    p = _write(tmp_path, "blob.json", payload.encode())
    out = rag.extract_text(p)
    assert "needle-in-haystack" in out


def test_extract_csv(tmp_path, app_client):
    from app.services import rag
    p = _write(tmp_path, "data.csv", b"name,city\nAlice,Paris\nBob,Tokyo\n")
    out = rag.extract_text(p)
    assert "Alice" in out and "Tokyo" in out


def test_extract_html(tmp_path, app_client):
    from app.services import rag
    html = b"<html><body><h1>Hi</h1><p>Some <b>page</b> body.</p></body></html>"
    p = _write(tmp_path, "page.html", html)
    out = rag.extract_text(p)
    assert "page body" in out.replace("\n", " ").lower() or "hi" in out.lower()


def test_extract_unknown_extension(tmp_path, app_client):
    from app.services import rag
    p = _write(tmp_path, "weird.xyz", b"\x00\x01\x02 not text")
    with pytest.raises(rag.ExtractionError):
        rag.extract_text(p)
