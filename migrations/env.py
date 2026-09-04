"""Alembic environment. Metadata comes from the SQLModel tables; the URL from the app config."""

from logging.config import fileConfig

from alembic import context
from sqlmodel import SQLModel

from baaki.app import models  # noqa: F401  — registers every table
from baaki.app.db import database_url, make_engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    context.configure(url=database_url(), target_metadata=target_metadata,
                      literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # The caller (baaki.app.db.init_db) passes its own engine so migrations run against the
    # database the app actually opened — not whatever DATABASE_URL happens to say. Falling back
    # to the configured URL keeps the plain `alembic` CLI working.
    # The caller (baaki.app.db.init_db) passes an engine with SQLite foreign-key enforcement
    # already off, because batch_alter_table drops and recreates referenced tables. The plain
    # `alembic` CLI gets an equivalent engine built here.
    connectable = config.attributes.get("engine") or make_engine(
        config.get_main_option("sqlalchemy.url") or database_url(), foreign_keys=False
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite can't ALTER most things in place; batch mode rebuilds the table instead.
            render_as_batch=connection.dialect.name == "sqlite",
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
