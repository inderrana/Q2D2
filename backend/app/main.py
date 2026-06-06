"""FastAPI app factory."""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import branding, db
from .core import middleware as security_mw
from .routers import (
    admin as admin_router,
    auth as auth_router,
    branding as branding_router,
    chats as chats_router,
    documents as docs_router,
    messages as msgs_router,
    models as models_router,
    search as search_router,
    settings as settings_router,
    usage as usage_router,
)
from .services import ollama, search as search_svc

STATIC_DIR = Path(__file__).parent / "static"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def _render_template(filename: str) -> HTMLResponse:
    raw = (STATIC_DIR / filename).read_text(encoding="utf-8")
    return HTMLResponse(branding.render_html(raw))


def _migrate_embed_default() -> None:
    """One-time backfill: if there are documents on disk but no active
    embedding model recorded, lock in `nomic-embed-text` @ 768 so the
    legacy `docs` Chroma collection stays queryable. Without this, an
    existing install would silently switch to the new branding default
    on first boot and orphan every previously-indexed document until an
    admin runs a re-embed."""
    with db.connect() as cx:
        has_setting = cx.execute(
            "SELECT 1 FROM app_settings WHERE key = 'embed_model'"
        ).fetchone()
        if has_setting:
            return
        has_docs = cx.execute(
            "SELECT 1 FROM documents WHERE chunks > 0 LIMIT 1"
        ).fetchone()
        if not has_docs:
            return
        now = db.now()
        cx.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
            ("embed_model", "nomic-embed-text", now),
        )
        cx.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
            ("embed_dim", "768", now),
        )


def create_app() -> FastAPI:
    db.init_db()
    _migrate_embed_default()
    brand = branding.load()
    app = FastAPI(title=brand["product"]["name"], version=brand["product"]["version"])

    # Middleware (order: outermost wraps innermost; install adds in reverse)
    security_mw.install(app)

    # API routers
    app.include_router(auth_router.router)
    app.include_router(branding_router.router)
    app.include_router(chats_router.router)
    app.include_router(msgs_router.router)
    app.include_router(docs_router.router)
    app.include_router(models_router.router)
    app.include_router(search_router.router)
    app.include_router(settings_router.router)
    app.include_router(usage_router.router)
    app.include_router(admin_router.router)

    @app.get("/healthz")
    async def healthz():
        try:
            await ollama.list_models()
            return {"ok": True, "web_search": search_svc.is_configured()}
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=503)

    # Static UI
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    def index():
        return _render_template("index.html")

    @app.get("/login")
    def login_page():
        return _render_template("login.html")

    return app


app = create_app()
