import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any

from .config import DB_PATH


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    display_name TEXT,
    email TEXT,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    avatar TEXT,
    min_iat INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS chats (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    title TEXT NOT NULL,
    model TEXT,
    summary TEXT,
    summary_until INTEGER NOT NULL DEFAULT 0,
    entities TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    model TEXT,
    tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    duration_ms INTEGER DEFAULT 0,
    meta TEXT,
    created_at INTEGER NOT NULL,
    FOREIGN KEY(chat_id) REFERENCES chats(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    source TEXT,
    bytes INTEGER DEFAULT 0,
    chunks INTEGER NOT NULL DEFAULT 0,
    folder TEXT NOT NULL DEFAULT '',
    chat_id TEXT,
    status TEXT NOT NULL DEFAULT 'ready',
    error TEXT,
    embed_model TEXT,
    embed_dim INTEGER,
    created_at INTEGER NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY(chat_id) REFERENCES chats(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS doc_folders (
    user_id TEXT NOT NULL,
    path TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY(user_id, path)
);

CREATE TABLE IF NOT EXISTS web_searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    query TEXT NOT NULL,
    results INTEGER DEFAULT 0,
    provider TEXT,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    user_id TEXT PRIMARY KEY,
    data TEXT NOT NULL
);

-- Application-wide key/value settings (admin-scoped runtime config).
-- Distinct from `settings` above, which is per-user JSON.
CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id TEXT,
    action TEXT NOT NULL,
    target_id TEXT,
    meta TEXT,
    ip TEXT,
    created_at INTEGER NOT NULL
);

-- Foreign-key indexes (cascade + listing performance)
CREATE INDEX IF NOT EXISTS ix_chats_user           ON chats(user_id);
CREATE INDEX IF NOT EXISTS ix_chats_updated        ON chats(user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS ix_messages_chat        ON messages(chat_id);
CREATE INDEX IF NOT EXISTS ix_messages_user        ON messages(user_id);
CREATE INDEX IF NOT EXISTS ix_messages_created     ON messages(chat_id, created_at);
CREATE INDEX IF NOT EXISTS ix_documents_user       ON documents(user_id);
CREATE INDEX IF NOT EXISTS ix_documents_chat       ON documents(chat_id);
CREATE INDEX IF NOT EXISTS ix_doc_folders_user     ON doc_folders(user_id);
CREATE INDEX IF NOT EXISTS ix_web_searches_user    ON web_searches(user_id, created_at);
CREATE INDEX IF NOT EXISTS ix_audit_actor          ON audit_log(actor_id, created_at);
"""


def init_db() -> None:
    # WAL gives readers a non-blocking view alongside writers.
    cx0 = sqlite3.connect(DB_PATH)
    try:
        cx0.execute("PRAGMA journal_mode=WAL")
        cx0.execute("PRAGMA synchronous=NORMAL")
        cx0.execute("PRAGMA busy_timeout=5000")
        cx0.execute("PRAGMA foreign_keys=ON")
        cx0.commit()
    finally:
        cx0.close()
    with connect() as cx:
        cx.executescript(SCHEMA)
        # Migrations for pre-existing databases
        cols = {r[1] for r in cx.execute("PRAGMA table_info(documents)").fetchall()}
        if "folder" not in cols:
            cx.execute("ALTER TABLE documents ADD COLUMN folder TEXT NOT NULL DEFAULT ''")
        if "chat_id" not in cols:
            cx.execute("ALTER TABLE documents ADD COLUMN chat_id TEXT")
        if "status" not in cols:
            cx.execute("ALTER TABLE documents ADD COLUMN status TEXT NOT NULL DEFAULT 'ready'")
        if "error" not in cols:
            cx.execute("ALTER TABLE documents ADD COLUMN error TEXT")
        if "embed_model" not in cols:
            cx.execute("ALTER TABLE documents ADD COLUMN embed_model TEXT")
        if "embed_dim" not in cols:
            cx.execute("ALTER TABLE documents ADD COLUMN embed_dim INTEGER")
        ucols = {r[1] for r in cx.execute("PRAGMA table_info(users)").fetchall()}
        if "avatar" not in ucols:
            cx.execute("ALTER TABLE users ADD COLUMN avatar TEXT")
        if "min_iat" not in ucols:
            cx.execute("ALTER TABLE users ADD COLUMN min_iat INTEGER NOT NULL DEFAULT 0")
        ccols = {r[1] for r in cx.execute("PRAGMA table_info(chats)").fetchall()}
        if "summary" not in ccols:
            cx.execute("ALTER TABLE chats ADD COLUMN summary TEXT")
        if "summary_until" not in ccols:
            cx.execute("ALTER TABLE chats ADD COLUMN summary_until INTEGER NOT NULL DEFAULT 0")
        if "entities" not in ccols:
            cx.execute("ALTER TABLE chats ADD COLUMN entities TEXT")


@contextmanager
def connect():
    cx = sqlite3.connect(DB_PATH, timeout=30.0)
    cx.row_factory = sqlite3.Row
    cx.execute("PRAGMA foreign_keys = ON")
    cx.execute("PRAGMA busy_timeout = 5000")
    try:
        yield cx
        cx.commit()
    except Exception:
        cx.rollback()
        raise
    finally:
        cx.close()


def now() -> int:
    return int(time.time() * 1000)


def new_id() -> str:
    return uuid.uuid4().hex


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}
