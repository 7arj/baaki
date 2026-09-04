"""Engine and session management. SQLite by default, any SQLAlchemy URL via DATABASE_URL."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from . import models  # noqa: F401  — registers tables on SQLModel.metadata

DEFAULT_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "baaki.db"


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    DEFAULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{DEFAULT_PATH}"


def make_engine(url: str | None = None, echo: bool = False):
    url = url or database_url()
    kwargs: dict = {"echo": echo}
    if url.startswith("sqlite"):
        # check_same_thread=False: FastAPI serves requests on a threadpool.
        kwargs["connect_args"] = {"check_same_thread": False}
        if ":memory:" in url:
            kwargs["poolclass"] = StaticPool
    eng = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        from sqlalchemy import event

        @event.listens_for(eng, "connect")
        def _pragmas(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")  # concurrent reads while the worker writes
            cur.close()

    return eng


_engine = None


def engine():
    global _engine
    if _engine is None:
        _engine = make_engine()
    return _engine


ROOT = Path(__file__).resolve().parent.parent.parent


def _alembic_config(eng):
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", str(eng.url))
    cfg.attributes["engine"] = eng
    return cfg


def init_db(eng=None) -> str:
    """Bring the schema to head.

    A fresh database is created from the models and stamped at head — faster than replaying
    every migration and identical in result. An existing one is upgraded. Tests pass their own
    engine and take the create-and-stamp path.
    """
    from alembic import command
    from alembic.runtime.migration import MigrationContext

    eng = eng or engine()
    with eng.connect() as conn:
        current = MigrationContext.configure(conn).get_current_revision()
        has_tables = conn.dialect.has_table(conn, "orgs")

    cfg = _alembic_config(eng)
    if not has_tables:
        SQLModel.metadata.create_all(eng)
        command.stamp(cfg, "head")
        return "created"
    if current is None:
        # Pre-Alembic database from an earlier build: adopt it at the initial revision.
        command.stamp(cfg, "head")
        return "adopted"
    command.upgrade(cfg, "head")
    return "upgraded"


def current_revision(eng=None) -> str | None:
    from alembic.runtime.migration import MigrationContext

    with (eng or engine()).connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def get_session() -> Iterator[Session]:
    with Session(engine()) as s:
        yield s
