import os
import secrets

from . import branding as _b

_brand = _b.load()

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://host.docker.internal:11434").rstrip("/")
OLLAMA_CONTAINER = os.getenv("OLLAMA_CONTAINER", "ollama")
DEFAULT_MODEL = _brand["model"]["default_chat_model"]
EMBED_MODEL = _brand["model"]["default_embed_model"]
# Small, fast model used for "utility" calls (query rewriting, intent
# classification, titling, answerability checks). Defaults to a tiny one
# so we don't swap the user's big chat model in/out of VRAM for these
# 1-shot calls. Falls back to the chat model if unset and the chat model
# is what's loaded — see services/chat.py for the resolution logic.
UTILITY_MODEL = os.getenv("UTILITY_MODEL", "qwen3:1.7b-q4_K_M")
DATA_DIR = os.getenv("DATA_DIR", "/data")
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "/uploads")
DB_PATH = os.path.join(DATA_DIR, "r2d2.db")
CHROMA_PATH = os.path.join(DATA_DIR, "chroma")
SECRET_FILE = os.path.join(DATA_DIR, ".jwt_secret")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(CHROMA_PATH, exist_ok=True)


def _load_or_create_secret() -> str:
    env = os.getenv("JWT_SECRET")
    if env:
        return env
    if os.path.exists(SECRET_FILE):
        return open(SECRET_FILE).read().strip()
    secret = secrets.token_urlsafe(48)
    with open(SECRET_FILE, "w") as f:
        f.write(secret)
    os.chmod(SECRET_FILE, 0o600)
    return secret


JWT_SECRET = _load_or_create_secret()
JWT_ALG = "HS256"
JWT_ISSUER = os.getenv("BRAND_PRODUCT_NAME", _brand["product"]["name"])
JWT_TTL_DAYS = int(_brand["security"]["jwt_ttl_days"])
ALLOW_SIGNUP = bool(_brand["features"]["allow_signup"])
MIN_PASSWORD_LEN = int(os.getenv("MIN_PASSWORD_LEN", "12"))
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))
MAX_FETCH_BYTES = int(os.getenv("MAX_FETCH_BYTES", str(8 * 1024 * 1024)))
MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", "60"))
