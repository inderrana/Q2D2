# R2D2 — Local RAG UI for Ollama

A self-hosted chat front-end for your local Ollama, with document RAG, optional
web search, multi-user accounts, white-label branding, and at-rest encryption
for stored API keys. Everything stays on your machine.

## Stack

- **Backend**: FastAPI · ChromaDB (vectors) · SQLite (WAL) · Alembic ·
  Argon2 + JWT auth · Trafilatura · pypdf / python-docx / openpyxl / xlrd /
  python-pptx / striprtf / odfpy / ebooklib / extract-msg · Tesseract OCR
- **Frontend**: vanilla JS + self-hosted marked / DOMPurify / highlight.js
  (no CDN) · CSP-locked down (`script-src 'self'`)
- **Reverse proxy / TLS**: Caddy with its internal CA (auto-issues a cert for
  `r2d2.localhost`; no ACME, no public DNS)
- **Web search**: pluggable providers — Tavily, Brave, DuckDuckGo, or a
  bundled **self-hosted SearXNG** (default, fully private), with Valkey as
  the limiter backend
- **Ollama**: configurable; `run.sh` auto-attaches an existing `ollama` container
  to the compose network when present, otherwise falls back to `host.docker.internal`

## Run

```bash
./run.sh up -d --build      # wraps `docker compose` and pulls secrets from `pass`
./run.sh trust-ca           # install Caddy's local root CA into system + Firefox trust
# open https://r2d2.localhost           (HTTPS via Caddy's local CA)
# or    http://r2d2.localhost            (auto-redirects to HTTPS)
```

Other handy `run.sh` verbs: any `docker compose` subcommand (`build`, `down`,
`logs`, `restart`, `ps`, …) plus the custom `trust-ca`.

> The app itself isn't published on a host port — Caddy is the only entry
> point. To bypass Caddy for debugging, `docker exec r2d2 curl localhost:8088`
> or temporarily add `ports: - "8088:8088"` back to the `r2d2` service.

Ports are 8080 / 8443 (not 80 / 443) because the stack is designed to work on
rootless Docker where unprivileged port binding is restricted. If you're on
rootful Docker and prefer bare 443, change the publish lines in
`docker-compose.yml` and the listeners in `Caddyfile`.

### Live-editing the frontend

The production image bakes static assets in. For iteration without rebuilds,
use the dev override:

```bash
COMPOSE_FILE=docker-compose.yml:docker-compose.dev.yml ./run.sh up -d r2d2
```

This bind-mounts `backend/app/static/` over the baked copy; reload the
browser to see CSS/JS/HTML changes.

Other handy `run.sh` verbs: `build`, `down`, `logs`, `restart`, `recreate`, `ps`.

> ⚠️ **Static assets are baked into the image.** After editing anything under
> `backend/app/static/` (CSS/JS/HTML) you must rebuild:
> `./run.sh build r2d2 && ./run.sh up -d --force-recreate r2d2`.

`run.sh` reads the following from [pass(1)](https://www.passwordstore.org/):

- `r2d2/jwt-secret`        (auto-generated on first run)
- `r2d2/encryption-key`    (auto-generated on first run, Fernet key)
- `r2d2/searxng-secret`    (auto-generated on first run, SearXNG `secret_key`)
- `r2d2/tavily`            (you `pass insert` it; optional)
- `r2d2/brave`             (you `pass insert` it; optional)

Anything else (DB path, branding overrides, port) goes through plain env vars
set in `.env` or `docker-compose.yml`. **No secrets live in the repo.**

## Features

### Chat
- Streaming responses, markdown + syntax highlighting + per-block copy buttons
- Multi-chat sidebar with auto-generated titles, rename, delete, search
- Per-message toggles for 📚 Docs (RAG) and 🌐 Web search
- Inline citations `[n]` with a collapsible sources panel under each answer

### Knowledge base
- Drag-and-drop / folder upload of PDF, DOCX, ODT, RTF, XLSX/XLS/CSV, PPTX,
  HTML, MD, TXT, JSON/YAML, EPUB, MSG, and source code
- Images and scanned PDFs route through Tesseract OCR on demand
- Per-document indexing runs in a background task; the UI polls a status badge
  until the document is ready
- URL ingest with SSRF guard (rejects `127.0.0.1`, `169.254.169.254`, RFC 1918, …)

### Multi-user
- First registered user becomes admin
- Admin panel: role changes, deletion (last-admin protected), usage stats
- Audit log of auth + admin events
- Per-user settings: default model, persona extras, web-search provider,
  API keys (encrypted at rest, round-trip masked)

### Theming & branding
- Three built-in themes: **Dark**, **Light**, **Slate** (neutral grey) — cycle
  from the header toggle
- Per-user **accent color** (16 presets + custom hex), with automatic
  derivation of `primary-container` / `on-primary-container` against the
  active surface
- Per-user **sidebar tone** (8 presets + native color picker) with
  WCAG-luminance-derived foreground, hover/state layers, and borders
- White-label via [`branding.toml`](branding.toml) (hot-reloads on file
  change) or `BRAND_*` env overrides:
  - `[theme] sidebar_color` — default sidebar tone for users who haven't picked
  - `[theme] slate_base` — base hex for the Slate theme; a 5-step surface
    ramp and readable text colors are auto-derived
- `/api/branding` exposes the public subset for the frontend

### UX
- Drag-resizable sidebar; collapse to icon rail
- Hamburger app-bar button hides when the sidebar is expanded on desktop
- Mobile breakpoint at 901px

## Security highlights

- TLS terminated by Caddy with its internal CA; the app itself only listens
  on plain HTTP behind the proxy (and on `:8088` for direct access)
- Session JWTs include `iss`/`jti`/`iat` and are revocable per user via a
  `min_iat` bump (logout / password change / role change kills other tokens)
- Cookies: `HttpOnly`, `SameSite=Lax`, `Secure` when `COOKIE_SECURE=1`
- CSRF: state-changing `/api/*` requires `X-Requested-With: fetch`
- Rate limits: per-IP × path bucket (login, register, chat, upload, url-ingest)
- Security headers: CSP (`script-src 'self'`), HSTS, nosniff, frame-deny,
  Referrer-Policy, Permissions-Policy
- DOMPurify hook enforces `rel="noopener noreferrer"` on every
  `target="_blank"` link
- Upload size cap (50 MB by default), magic-byte sniffed avatars
- Argon2id password hashing, 12-char minimum
- SearXNG runs with `cap_drop: ALL` (only `CHOWN/SETGID/SETUID` kept), is not
  published on any host port, has its botdetection limiter enabled (backed by
  Valkey), and whitelists docker private subnets only

## Env

| Var | Default | Notes |
|---|---|---|
| `OLLAMA_URL` | `http://ollama:11434` | falls back to `host.docker.internal:11434` |
| `SEARXNG_URL` | `http://searxng:8080` | bundled SearXNG; override to point elsewhere |
| `DEFAULT_MODEL` | `gemma3:4b` | |
| `EMBED_MODEL` | `nomic-embed-text` | |
| `JWT_SECRET` | *(required)* | set by `run.sh` from `pass` |
| `ENCRYPTION_KEY` | *(required)* | Fernet base64; autogen + stored in `pass` by `run.sh` |
| `SEARXNG_SECRET` | *(required for SearXNG)* | autogen + stored in `pass` by `run.sh` |
| `COOKIE_SECURE` | `0` | set `1` behind HTTPS (Caddy in this stack) |
| `ALLOW_SIGNUP` | `0` | first user always allowed |
| `MAX_UPLOAD_BYTES` | `52428800` | 50 MB |
| `MAX_FETCH_BYTES` | `8388608` | 8 MB URL ingest cap |
| `BRANDING_FILE` | `/app/branding.toml` | |
| `BRAND_PRIMARY` / `BRAND_ACCENT` | — | hex; override the brand palette |
| `BRAND_SIDEBAR_COLOR` | — | hex; default sidebar tone (per-user picker wins) |
| `BRAND_SLATE_BASE` | — | hex; base for the Slate theme's auto-derived ramp |

## Development

```bash
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
DATA_DIR=./.data UPLOAD_DIR=./.data/uploads JWT_SECRET=dev-secret \
    uvicorn app.main:app --reload --port 8088
```

Run the test suite:

```bash
cd backend && pytest
```

Schema migrations: the current schema is bootstrapped by `app.db.init_db()`;
future changes go through Alembic (`backend/alembic/`).

## Repo layout

```
backend/
  app/
    main.py            FastAPI entrypoint, middleware, routers
    branding.py        branding.toml loader + env overrides + hot reload
    routers/           auth, chat, docs, settings, admin, branding, …
    services/          rag, search/ (tavily/brave/ddg/searxng), users, files, …
    static/            frontend (index.html, app.js, styles.css, vendor/)
  alembic/             migrations
  tests/               pytest suite
branding.toml          white-label config (mounted into the container)
Caddyfile              reverse proxy + local-CA TLS for r2d2.localhost
searxng/settings.yml   self-hosted SearXNG config (JSON format enabled)
searxng/limiter.toml   botdetection / limiter config (docker subnets whitelisted)
docker-compose.yml     r2d2 + caddy + searxng + valkey services
docker-compose.dev.yml dev override: bind-mount static assets for live edits
run.sh                 pass-aware wrapper around docker compose (+ trust-ca)
.env.example           non-secret env template
```

## License

See repo for licensing terms.
