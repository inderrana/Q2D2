"""chat memory: rolling summary + entity cache

Adds three columns to `chats` that back the conversation-memory feature:
- summary: free-text rolling summary of older turns
- summary_until: count of messages already folded into `summary`
- entities: JSON blob of {key: value} facts about the user/chat

For pre-existing databases these are also added by `app.db.init_db()` so the
migration here is just for Alembic-tracked installs.
"""
from __future__ import annotations

from alembic import op

revision = "0002_chat_memory"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def _has_column(table: str, col: str) -> bool:
    bind = op.get_bind()
    rows = bind.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == col for r in rows)


def upgrade() -> None:
    if not _has_column("chats", "summary"):
        op.execute("ALTER TABLE chats ADD COLUMN summary TEXT")
    if not _has_column("chats", "summary_until"):
        op.execute("ALTER TABLE chats ADD COLUMN summary_until INTEGER NOT NULL DEFAULT 0")
    if not _has_column("chats", "entities"):
        op.execute("ALTER TABLE chats ADD COLUMN entities TEXT")


def downgrade() -> None:
    # SQLite pre-3.35 can't DROP COLUMN; leave columns in place on downgrade.
    pass
