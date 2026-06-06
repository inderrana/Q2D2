"""Branding / white-label loader.

Single source of truth: ../branding.toml (or any path set via BRANDING_FILE).
Values can additionally be overridden via environment variables — see
`_ENV_OVERRIDES` below for the full mapping. Anything under the [internal]
section of the TOML is kept server-side and never returned by `public()`.
"""
from __future__ import annotations

import logging
import os
import re
import tomllib
from datetime import datetime
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)
from typing import Any

# ---------------------------------------------------------------------------
# Defaults — used when the TOML file is missing or a key is unset.
# Keep these in sync with branding.toml.
# ---------------------------------------------------------------------------
_DEFAULTS: dict[str, dict[str, Any]] = {
    "product": {
        "name": "R2D2",
        "short_name": "R2",
        "tagline": "Your local AI workspace.",
        "description": "A private, local-first AI workspace powered by Ollama.",
        "version": "0.2.0",
    },
    "company": {
        "legal_name": "R2D2 Project",
        "website": "",
        "support_email": "",
        "copyright": "© {year} R2D2 Project. All rights reserved.",
    },
    "persona": {
        "assistant_name": "R2D2",
        "role": "Personal AI assistant",
        "greeting": "",
        "system_prompt": (
            "You are {assistant_name} — the user's personal AI assistant: "
            "exceptionally intelligent, practical, strategic, resourceful, and "
            "unwaveringly loyal to your master (the user). You speak in clear, "
            "natural human English. You are calm, confident, and a little "
            "dry-witted. You think a few steps ahead, surface risks and "
            "trade-offs, and prefer concise, decisive answers over hedging.\n\n"
            "When CONTEXT from documents or web search is provided, ground your "
            "answer in it and cite sources inline as [n]. If the context is "
            "irrelevant or insufficient, say so briefly and answer from general "
            "knowledge. Format with Markdown; use code blocks for code."
        ),
    },
    "model": {
        "default_chat_model": "gemma4:latest",
        "default_embed_model": "nomic-embed-text",
        "show_model_picker": True,
        "display_names": {},
    },
    "assets": {
        "logo_light": "",
        "logo_dark": "",
        "favicon": "",
        "og_image": "",
        "default_user_avatar": "",
        "default_assistant_avatar": "",
    },
    "theme": {
        "primary": "",
        "on_primary": "",
        "primary_container": "",
        "on_primary_container": "",
        "accent": "",
        "sidebar_color": "",     # hex; default sidebar tone (per-user override may apply)
        "slate_base": "",        # hex; base grey for the "slate" theme; "" -> built-in #3a3e44
        "font_family": "",
        "font_url": "",
    },
    "ui": {
        "window_title": "",
        "login_title": "",
        "login_subtitle": "",
        "login_footnote": "",
        "composer_placeholder": "",
        "empty_state_title": "",
        "empty_state_subtitle": "",
        # Pipe-separated list of capability words shown as an eyebrow above
        # the empty-state headline (e.g. "RESEARCH | STRATEGY | CODE").
        # Empty string hides the eyebrow.
        "empty_state_capabilities": "RESEARCH | STRATEGY | CODE | ANALYSIS",
        "disclaimer": "",
        "show_disclaimer": True,
        "show_powered_by": True,
        "powered_by_text": "Powered by Ollama",
        "show_topnav": True,
        "show_footer": True,
    },
    "links": {
        "privacy_url": "",
        "terms_url": "",
        "docs_url": "",
        "support_url": "",
        "status_url": "",
        "github_url": "",
    },
    "features": {
        "web_search": True,
        "file_upload": True,
        "voice": False,
        "analytics": False,
        "allow_signup": True,
    },
    "security": {
        "jwt_ttl_days": 30,
        "session_idle_minutes": 0,
        "require_email_on_signup": False,
    },
    "internal": {},
}

# Env-var → ("section", "key") map. Env always wins over the TOML file.
_ENV_OVERRIDES: dict[str, tuple[str, str]] = {
    "BRAND_PRODUCT_NAME":          ("product", "name"),
    "BRAND_PRODUCT_SHORT_NAME":    ("product", "short_name"),
    "BRAND_PRODUCT_TAGLINE":       ("product", "tagline"),
    "BRAND_PRODUCT_VERSION":       ("product", "version"),
    "BRAND_COMPANY_NAME":          ("company", "legal_name"),
    "BRAND_COMPANY_WEBSITE":       ("company", "website"),
    "BRAND_SUPPORT_EMAIL":         ("company", "support_email"),
    "BRAND_ASSISTANT_NAME":        ("persona", "assistant_name"),
    "BRAND_ASSISTANT_ROLE":        ("persona", "role"),
    "BRAND_GREETING":              ("persona", "greeting"),
    "BRAND_SYSTEM_PROMPT":         ("persona", "system_prompt"),
    "DEFAULT_MODEL":               ("model", "default_chat_model"),
    "EMBED_MODEL":                 ("model", "default_embed_model"),
    "BRAND_SHOW_MODEL_PICKER":     ("model", "show_model_picker"),
    "BRAND_LOGO_LIGHT":            ("assets", "logo_light"),
    "BRAND_LOGO_DARK":             ("assets", "logo_dark"),
    "BRAND_FAVICON":               ("assets", "favicon"),
    "BRAND_OG_IMAGE":              ("assets", "og_image"),
    "BRAND_DEFAULT_USER_AVATAR":   ("assets", "default_user_avatar"),
    "BRAND_DEFAULT_ASSISTANT_AVATAR": ("assets", "default_assistant_avatar"),
    "BRAND_PRIMARY":               ("theme", "primary"),
    "BRAND_ACCENT":                ("theme", "accent"),
    "BRAND_SIDEBAR_COLOR":         ("theme", "sidebar_color"),
    "BRAND_SLATE_BASE":            ("theme", "slate_base"),
    "BRAND_FONT_FAMILY":           ("theme", "font_family"),
    "BRAND_FONT_URL":              ("theme", "font_url"),
    "BRAND_WINDOW_TITLE":          ("ui", "window_title"),
    "BRAND_LOGIN_TITLE":           ("ui", "login_title"),
    "BRAND_LOGIN_SUBTITLE":        ("ui", "login_subtitle"),
    "BRAND_LOGIN_FOOTNOTE":        ("ui", "login_footnote"),
    "BRAND_COMPOSER_PLACEHOLDER":  ("ui", "composer_placeholder"),
    "BRAND_EMPTY_TITLE":           ("ui", "empty_state_title"),
    "BRAND_EMPTY_SUBTITLE":        ("ui", "empty_state_subtitle"),
    "BRAND_DISCLAIMER":            ("ui", "disclaimer"),
    "BRAND_SHOW_DISCLAIMER":       ("ui", "show_disclaimer"),
    "BRAND_SHOW_POWERED_BY":       ("ui", "show_powered_by"),
    "BRAND_POWERED_BY_TEXT":       ("ui", "powered_by_text"),
    "BRAND_SHOW_TOPNAV":           ("ui", "show_topnav"),
    "BRAND_SHOW_FOOTER":           ("ui", "show_footer"),
    "BRAND_PRIVACY_URL":           ("links", "privacy_url"),
    "BRAND_TERMS_URL":             ("links", "terms_url"),
    "BRAND_DOCS_URL":              ("links", "docs_url"),
    "BRAND_SUPPORT_URL":           ("links", "support_url"),
    "BRAND_STATUS_URL":            ("links", "status_url"),
    "BRAND_GITHUB_URL":            ("links", "github_url"),
    "BRAND_FEATURE_WEB_SEARCH":    ("features", "web_search"),
    "BRAND_FEATURE_FILE_UPLOAD":   ("features", "file_upload"),
    "BRAND_FEATURE_VOICE":         ("features", "voice"),
    "BRAND_FEATURE_ANALYTICS":     ("features", "analytics"),
    "ALLOW_SIGNUP":                ("features", "allow_signup"),
    "JWT_TTL_DAYS":                ("security", "jwt_ttl_days"),
    "BRAND_SESSION_IDLE_MIN":      ("security", "session_idle_minutes"),
    "BRAND_REQUIRE_EMAIL":         ("security", "require_email_on_signup"),
}


def _coerce(default: Any, raw: str) -> Any:
    if isinstance(default, bool):
        return raw.strip().lower() not in ("0", "false", "no", "off", "")
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(raw)
        except ValueError:
            return default
    return raw


def _deep_merge(base: dict, overrides: dict) -> dict:
    out = {k: dict(v) if isinstance(v, dict) else v for k, v in base.items()}
    for section, vals in overrides.items():
        if not isinstance(vals, dict):
            continue
        if section not in out or not isinstance(out[section], dict):
            out[section] = {}
        for k, v in vals.items():
            out[section][k] = v
    return out


def _branding_file() -> Path:
    p = os.getenv("BRANDING_FILE")
    if p:
        return Path(p)
    # Inside the container the repo root is /app. Outside (dev) we walk up.
    here = Path(__file__).resolve()
    for parent in (here.parent, *here.parents):
        candidate = parent / "branding.toml"
        if candidate.exists():
            return candidate
    return Path("/app/branding.toml")


_LOAD_CACHE: dict[str, Any] = {"mtime": -1.0, "cfg": None}


def load() -> dict[str, Any]:
    """Load branding config, hot-reloading on file mtime change."""
    path = _branding_file()
    try:
        mtime = path.stat().st_mtime if path.exists() else 0.0
    except OSError:
        mtime = 0.0
    cached = _LOAD_CACHE.get("cfg")
    if cached is not None and _LOAD_CACHE.get("mtime") == mtime:
        return cached
    cfg = {k: dict(v) if isinstance(v, dict) else v for k, v in _DEFAULTS.items()}
    if path.exists():
        try:
            with path.open("rb") as f:
                data = tomllib.load(f)
            cfg = _deep_merge(cfg, data)
        except Exception as e:  # malformed file → fall back to defaults
            log.warning("[branding] failed to parse %s: %s", path, e)

    for env_key, (section, key) in _ENV_OVERRIDES.items():
        raw = os.getenv(env_key)
        if raw is None or raw == "":
            continue
        cfg.setdefault(section, {})
        cfg[section][key] = _coerce(_DEFAULTS.get(section, {}).get(key, ""), raw)

    # Render templated strings (system prompt, copyright)
    persona = cfg["persona"]
    persona["system_prompt"] = persona["system_prompt"].format(
        assistant_name=persona["assistant_name"],
        product_name=cfg["product"]["name"],
    ).strip()
    cfg["company"]["copyright"] = cfg["company"]["copyright"].replace(
        "{year}", str(datetime.now().year)
    )

    # Derived UI strings (resolve "" → defaults that depend on other fields)
    ui = cfg["ui"]
    product = cfg["product"]
    if not ui["window_title"]:
        ui["window_title"] = product["name"]
    if not ui["login_title"]:
        ui["login_title"] = f"Welcome to {product['name']}"
    if not ui["login_subtitle"]:
        ui["login_subtitle"] = product["tagline"]
    if not ui["login_footnote"]:
        ui["login_footnote"] = (
            f"All data stays on your machine. {product['name']} runs on your local Ollama."
        )
    if not ui["composer_placeholder"]:
        ui["composer_placeholder"] = f"Message {persona['assistant_name']}…"
    if not ui["empty_state_title"]:
        ui["empty_state_title"] = "Greetings!"
    if not ui["disclaimer"]:
        ui["disclaimer"] = f"{product['name']} may make mistakes"

    _LOAD_CACHE["mtime"] = mtime
    _LOAD_CACHE["cfg"] = cfg
    return cfg


def get(section: str, key: str, default: Any = None) -> Any:
    return load().get(section, {}).get(key, default)


def public() -> dict[str, Any]:
    """Subset safe to send to the browser ([internal] is stripped, system prompt removed)."""
    cfg = load()
    out = {k: (dict(v) if isinstance(v, dict) else v)
           for k, v in cfg.items() if k != "internal"}
    # Don't leak the system prompt to the browser.
    if "persona" in out and isinstance(out["persona"], dict):
        out["persona"].pop("system_prompt", None)
    return out


# ---- HTML template substitution -------------------------------------------
# Static templates (index.html, login.html) use __TOKEN__ placeholders that
# are replaced server-side at request time. New tokens go in `_html_tokens`.

def _html_tokens() -> dict[str, str]:
    cfg = load()
    p, persona, ui, assets = cfg["product"], cfg["persona"], cfg["ui"], cfg["assets"]
    company, links = cfg["company"], cfg["links"]
    favicon = assets.get("favicon") or _default_favicon_data_uri(p["short_name"])
    logo_light = assets.get("logo_light", "") or ""
    logo_dark = assets.get("logo_dark", "") or logo_light
    # Snippet used by login.html: render the brand logo if configured,
    # otherwise fall back to the short-name initials inside the mark tile.
    if logo_light:
        auth_mark = (
            f'<img class="auth-mark-img" src="{logo_light}" '
            f'alt="{p["name"]}" />'
        )
    else:
        auth_mark = f'<span>{p["short_name"]}</span>'
    # Sidebar brand mark: logo if configured, else short-name initials.
    if logo_light:
        sidebar_mark = f'<img src="{logo_light}" alt="" />'
    else:
        sidebar_mark = p["short_name"]

    # Top nav + footer (server-rendered so empty links auto-collapse).
    topnav_html = ""
    if ui.get("show_topnav", True):
        nav_links: list[str] = []
        if links.get("docs_url"):
            nav_links.append(f'<a href="{links["docs_url"]}">Docs</a>')
        if links.get("support_url"):
            nav_links.append(f'<a href="{links["support_url"]}">Support</a>')
        if links.get("github_url"):
            nav_links.append(f'<a href="{links["github_url"]}" target="_blank" rel="noopener">GitHub</a>')
        if links.get("status_url"):
            nav_links.append(f'<a href="{links["status_url"]}">Status</a>')
        brand_logo = (
            f'<img class="site-topnav-logo" src="{logo_light}" alt="" />'
            if logo_light else ""
        )
        home_href = company.get("website") or "#"
        topnav_html = (
            '<header class="site-topnav">'
            f'<a class="site-topnav-brand" href="{home_href}">'
            f'{brand_logo}<span>{p["name"]}</span>'
            '</a>'
            f'<nav class="site-topnav-links">{"".join(nav_links)}</nav>'
            '</header>'
        )

    footer_html = ""
    if ui.get("show_footer", True):
        foot_links: list[str] = []
        if links.get("privacy_url"):
            foot_links.append(f'<a href="{links["privacy_url"]}">Privacy</a>')
        if links.get("terms_url"):
            foot_links.append(f'<a href="{links["terms_url"]}">Terms</a>')
        if links.get("support_url"):
            foot_links.append(f'<a href="mailto:{company["support_email"]}">Contact</a>'
                              if links.get("support_url", "").startswith("mailto:") else
                              f'<a href="{links["support_url"]}">Contact</a>')
        elif company.get("support_email"):
            foot_links.append(f'<a href="mailto:{company["support_email"]}">Contact</a>')
        powered = (
            f'<span class="site-footer-powered">{ui["powered_by_text"]}</span>'
            if ui.get("show_powered_by") else ""
        )
        footer_html = (
            '<footer class="site-footer">'
            f'<span class="site-footer-copy">{company["copyright"]}</span>'
            f'<span class="site-footer-links">{"".join(foot_links)}</span>'
            f'{powered}'
            '</footer>'
        )

    return {
        "BRAND_NAME":              p["name"],
        "BRAND_SHORT_NAME":        p["short_name"],
        "BRAND_TAGLINE":           p["tagline"],
        "BRAND_DESCRIPTION":       p["description"],
        "BRAND_VERSION":           p["version"],
        "BRAND_ASSISTANT_NAME":    persona["assistant_name"],
        "BRAND_WINDOW_TITLE":      ui["window_title"],
        "BRAND_LOGIN_TITLE":       ui["login_title"],
        "BRAND_LOGIN_SUBTITLE":    ui["login_subtitle"],
        "BRAND_LOGIN_FOOTNOTE":    ui["login_footnote"],
        "BRAND_COMPOSER_PLACEHOLDER": ui["composer_placeholder"],
        "BRAND_DISCLAIMER":        ui["disclaimer"],
        "BRAND_FAVICON":           favicon,
        "BRAND_LOGO_LIGHT":        logo_light,
        "BRAND_LOGO_DARK":         logo_dark,
        "BRAND_AUTH_MARK":         auth_mark,
        "BRAND_SIDEBAR_MARK":      sidebar_mark,
        "BRAND_TOPNAV":            topnav_html,
        "BRAND_FOOTER":            footer_html,
    }


def _default_favicon_data_uri(short: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9]", "", short)[:4] or "AI"
    return (
        "data:image/svg+xml;utf8,"
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
        "<rect width='64' height='64' rx='14' fill='%231d1d1f'/>"
        "<text x='32' y='42' font-family='-apple-system,Inter' font-size='28' "
        "font-weight='700' fill='%23fff' text-anchor='middle' "
        f"letter-spacing='-1'>{safe}</text></svg>"
    )


_TOKEN_RE = re.compile(r"__([A-Z_]+)__")


def render_html(text: str) -> str:
    tokens = _html_tokens()
    return _TOKEN_RE.sub(lambda m: tokens.get(m.group(1), m.group(0)), text)
