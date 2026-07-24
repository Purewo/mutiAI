"""SQLAlchemy engine and session boundary."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from mutiai.config import Settings


class Database:
    """Owns the engine and session factory for one application instance."""

    def __init__(self, settings: Settings) -> None:
        self._prepare_sqlite_directory(settings.database_url)
        connect_args = (
            {"check_same_thread": False}
            if settings.database_url.startswith("sqlite")
            else {}
        )
        self.engine: Engine = create_engine(
            settings.database_url,
            connect_args=connect_args,
            pool_pre_ping=True,
        )
        if settings.database_url.startswith("sqlite"):
            event.listen(self.engine, "connect", self._enable_sqlite_foreign_keys)
        self.session_factory = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
        )

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Yield one request-scoped session and roll it back on errors."""

        session = self.session_factory()
        try:
            yield session
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        self.engine.dispose()

    @staticmethod
    def _prepare_sqlite_directory(database_url: str) -> None:
        url = make_url(database_url)
        if url.get_backend_name() != "sqlite":
            return
        if not url.database or url.database == ":memory:":
            return

        Path(url.database).parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _enable_sqlite_foreign_keys(dbapi_connection, _) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()
