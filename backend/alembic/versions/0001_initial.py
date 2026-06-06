"""initial schema baseline

This revision is a no-op: the schema is bootstrapped at runtime by
`app.db.init_db()`. It exists so future revisions have a starting point
(`alembic stamp 0001` on existing installs).

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-31 00:00:00
"""
from __future__ import annotations

from alembic import op  # noqa: F401  (imported for side-effect parity)

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No-op. The current schema lives in app/db.py SCHEMA and is created by
    # init_db() on container start. New columns / tables go in subsequent
    # revisions and should also be reflected in app/db.py for fresh installs.
    pass


def downgrade() -> None:
    pass
