"""Shared test fixtures.

Each test gets an isolated DATA_DIR (tmpdir) so the SQLite + Chroma stores are
fresh. We also force a known JWT_SECRET / ENCRYPTION_KEY for reproducibility.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("JWT_SECRET", "test-secret-please-ignore-32chars-min")
    monkeypatch.setenv("ENCRYPTION_KEY", "")  # derive from JWT_SECRET
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.setenv("ALLOW_SIGNUP", "1")

    # Force a clean import of the app modules with the new env.
    for mod in list(sys.modules):
        if mod.startswith("app") or mod == "app":
            del sys.modules[mod]

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from fastapi.testclient import TestClient  # noqa: WPS433
    from app.main import app  # noqa: WPS433

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def auth_client(app_client):
    """Registers an admin user and returns the authenticated TestClient."""
    r = app_client.post(
        "/api/auth/register",
        json={"username": "alice", "password": "correct horse battery staple",
              "display_name": "Alice"},
        headers={"X-Requested-With": "fetch"},
    )
    assert r.status_code == 200, r.text
    return app_client
