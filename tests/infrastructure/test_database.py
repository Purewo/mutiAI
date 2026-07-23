from sqlalchemy import text

from mutiai.config import Settings
from mutiai.db import Database


def test_database_creates_sqlite_parent_and_opens_session(tmp_path) -> None:
    database_path = tmp_path / "nested" / "mutiai.db"
    database = Database(
        Settings(
            app_env="test",
            database_url=f"sqlite+pysqlite:///{database_path.as_posix()}",
        )
    )

    try:
        with database.session() as session:
            assert session.scalar(text("SELECT 1")) == 1
    finally:
        database.dispose()

    assert database_path.exists()
