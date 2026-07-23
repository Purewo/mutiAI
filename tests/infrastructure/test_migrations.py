from sqlalchemy import create_engine, inspect, text

from mutiai.migrations import upgrade_database


def test_initial_migration_creates_auth_schema(tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'migrations.db'}"

    upgrade_database(database_url)
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        with engine.connect() as connection:
            revision = connection.scalar(
                text("SELECT version_num FROM alembic_version")
            )
    finally:
        engine.dispose()

    assert {"alembic_version", "users", "browser_sessions"} <= tables
    assert revision == "20260723_0001"
