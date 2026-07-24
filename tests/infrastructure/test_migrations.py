from sqlalchemy import create_engine, inspect, text

from mutiai.migrations import upgrade_database


def test_migrations_create_current_product_schema(tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'migrations.db'}"

    upgrade_database(database_url)
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        runtime_execution_columns = {
            column["name"] for column in inspector.get_columns("runtime_executions")
        }
        workspace_columns = {
            column["name"] for column in inspector.get_columns("workspaces")
        }
        with engine.connect() as connection:
            revision = connection.scalar(
                text("SELECT version_num FROM alembic_version")
            )
    finally:
        engine.dispose()

    assert {
        "alembic_version",
        "browser_sessions",
        "assignments",
        "approval_requests",
        "organization_spec_versions",
        "organizations",
        "product_events",
        "runtime_executions",
        "runtime_control_policies",
        "runtime_provider_capacities",
        "runtime_bindings",
        "workspaces",
        "tasks",
        "users",
    } <= tables
    assert {
        "runtime_binding_id",
        "runtime_binding_key",
        "requested_model",
        "actual_model",
        "reasoning_effort",
        "security_mode",
        "approval_policy",
        "sandbox_mode",
        "network_access",
        "context_compactions",
    } <= runtime_execution_columns
    assert {
        "thread_compaction_count",
        "thread_generation",
        "last_compacted_at",
        "last_delivery_summary",
    } <= workspace_columns
    assert revision == "20260725_0007"
