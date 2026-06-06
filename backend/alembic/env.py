"""Alembic environment for R2D2's SQLite store.

Schema is currently managed by `app/db.py SCHEMA` and `init_db()`. Alembic is
included as a forward-compatible migration channel: as the schema evolves,
new revisions go under `versions/`, and `alembic upgrade head` will run them
on the same SQLite file.
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

config = context.config

# Resolve DB path the same way the runtime app does.
data_dir = os.getenv("DATA_DIR", str(ROOT.parent / "data"))
db_path = os.path.join(data_dir, "r2d2.db")
config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")

if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name)
    except Exception:  # logging is optional during migrations
        pass

target_metadata = None  # raw-SQL migrations only


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
