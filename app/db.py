"""Engine and session factory. SQLite in WAL mode so the web app can read mid-crawl."""

from __future__ import annotations

import logging

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.models import Base

log = logging.getLogger(__name__)

engine = create_engine(settings.database_url, future=True)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=15000")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def _add_missing_columns() -> None:
    """Bring an existing database up to the current models.

    # ponytail: added columns only — no renames, type changes or drops. That has
    # covered every schema change so far; reach for Alembic when it stops being true.
    """
    inspector = inspect(engine)
    with engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            known = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in known:
                    continue
                kind = column.type.compile(engine.dialect)
                connection.execute(
                    text(f'ALTER TABLE {table.name} ADD COLUMN "{column.name}" {kind}')
                )
                log.info("added column %s.%s", table.name, column.name)


def init_db() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
    _add_missing_columns()
