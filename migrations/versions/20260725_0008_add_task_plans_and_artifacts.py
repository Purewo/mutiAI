"""add Task execution plans and Artifact handoff records

Revision ID: 20260725_0008
Revises: 20260725_0007
Create Date: 2026-07-25 01:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260725_0008"
down_revision: str | Sequence[str] | None = "20260725_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_execution_plans",
        sa.Column("plan_id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column(
            "organization_spec_version_id",
            sa.String(length=36),
            nullable=False,
        ),
        sa.Column("plan_version", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.String(length=20), nullable=False),
        sa.Column("definition_hash", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("validation_summary", sa.Text(), nullable=True),
        sa.Column("initial_input_contracts", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("activated_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('draft', 'validated', 'active', 'completed', "
            "'needs_revision', 'failed', 'cancelled')",
            name=op.f("ck_task_execution_plans_valid_status"),
        ),
        sa.CheckConstraint(
            "plan_version >= 1",
            name=op.f("ck_task_execution_plans_positive_plan_version"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_spec_version_id"],
            ["organization_spec_versions.spec_version_id"],
            name=op.f(
                "fk_task_execution_plans_organization_spec_version_id_"
                "organization_spec_versions"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.task_id"],
            name=op.f("fk_task_execution_plans_task_id_tasks"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "plan_id",
            name=op.f("pk_task_execution_plans"),
        ),
        sa.UniqueConstraint(
            "task_id",
            "plan_version",
            name="uq_task_execution_plans_task_version",
        ),
    )
    op.create_index(
        op.f("ix_task_execution_plans_definition_hash"),
        "task_execution_plans",
        ["definition_hash"],
        unique=False,
    )
    op.create_index(
        op.f("ix_task_execution_plans_organization_spec_version_id"),
        "task_execution_plans",
        ["organization_spec_version_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_task_execution_plans_status"),
        "task_execution_plans",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_task_execution_plans_task_id"),
        "task_execution_plans",
        ["task_id"],
        unique=False,
    )

    op.create_table(
        "plan_steps",
        sa.Column("plan_step_id", sa.String(length=36), nullable=False),
        sa.Column("plan_id", sa.String(length=36), nullable=False),
        sa.Column("step_key", sa.String(length=64), nullable=False),
        sa.Column("role_key", sa.String(length=64), nullable=False),
        sa.Column("step_kind", sa.String(length=32), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("acceptance_criteria", sa.Text(), nullable=False),
        sa.Column("input_contracts", sa.JSON(), nullable=False),
        sa.Column("output_contracts", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("ready_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending_dependency', 'ready', 'submitted', 'running', "
            "'waiting', 'validating_output', 'completed', 'blocked', 'failed', "
            "'cancelled')",
            name=op.f("ck_plan_steps_valid_status"),
        ),
        sa.CheckConstraint(
            "sequence >= 0",
            name=op.f("ck_plan_steps_nonnegative_sequence"),
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["task_execution_plans.plan_id"],
            name=op.f("fk_plan_steps_plan_id_task_execution_plans"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("plan_step_id", name=op.f("pk_plan_steps")),
        sa.UniqueConstraint(
            "plan_id",
            "sequence",
            name="uq_plan_steps_plan_sequence",
        ),
        sa.UniqueConstraint(
            "plan_id",
            "step_key",
            name="uq_plan_steps_plan_key",
        ),
    )
    op.create_index(
        op.f("ix_plan_steps_plan_id"),
        "plan_steps",
        ["plan_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_plan_steps_role_key"),
        "plan_steps",
        ["role_key"],
        unique=False,
    )
    op.create_index(
        op.f("ix_plan_steps_status"),
        "plan_steps",
        ["status"],
        unique=False,
    )

    op.create_table(
        "plan_step_dependencies",
        sa.Column("dependency_id", sa.String(length=36), nullable=False),
        sa.Column("plan_step_id", sa.String(length=36), nullable=False),
        sa.Column("depends_on_step_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "plan_step_id <> depends_on_step_id",
            name=op.f("ck_plan_step_dependencies_different_steps"),
        ),
        sa.ForeignKeyConstraint(
            ["depends_on_step_id"],
            ["plan_steps.plan_step_id"],
            name=op.f(
                "fk_plan_step_dependencies_depends_on_step_id_plan_steps"
            ),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["plan_step_id"],
            ["plan_steps.plan_step_id"],
            name=op.f("fk_plan_step_dependencies_plan_step_id_plan_steps"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "dependency_id",
            name=op.f("pk_plan_step_dependencies"),
        ),
        sa.UniqueConstraint(
            "plan_step_id",
            "depends_on_step_id",
            name="uq_plan_step_dependencies_edge",
        ),
    )
    op.create_index(
        op.f("ix_plan_step_dependencies_depends_on_step_id"),
        "plan_step_dependencies",
        ["depends_on_step_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_plan_step_dependencies_plan_step_id"),
        "plan_step_dependencies",
        ["plan_step_id"],
        unique=False,
    )

    with op.batch_alter_table("assignments") as batch_op:
        batch_op.add_column(
            sa.Column("plan_step_id", sa.String(length=36), nullable=True)
        )
        batch_op.create_foreign_key(
            op.f("fk_assignments_plan_step_id_plan_steps"),
            "plan_steps",
            ["plan_step_id"],
            ["plan_step_id"],
            ondelete="CASCADE",
        )
        batch_op.create_unique_constraint(
            op.f("uq_assignments_plan_step_id"),
            ["plan_step_id"],
        )
    op.create_index(
        op.f("ix_assignments_plan_step_id"),
        "assignments",
        ["plan_step_id"],
        unique=False,
    )

    op.create_table(
        "artifacts",
        sa.Column("artifact_id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("origin", sa.String(length=20), nullable=False),
        sa.Column("source_delivery_id", sa.String(length=100), nullable=False),
        sa.Column("producer_assignment_id", sa.String(length=36), nullable=True),
        sa.Column("producer_plan_step_id", sa.String(length=36), nullable=True),
        sa.Column("source_workspace_id", sa.String(length=36), nullable=True),
        sa.Column("contract_key", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=20), nullable=False),
        sa.Column("artifact_version", sa.Integer(), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("file_name", sa.String(length=255), nullable=False),
        sa.Column("source_relative_path", sa.String(length=1024), nullable=False),
        sa.Column("storage_relative_path", sa.String(length=1024), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("validation_summary", sa.Text(), nullable=True),
        sa.Column("supersedes_artifact_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("released_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('draft', 'validated', 'released', 'rejected', "
            "'superseded')",
            name=op.f("ck_artifacts_valid_status"),
        ),
        sa.CheckConstraint(
            "origin IN ('task_input', 'assignment')",
            name=op.f("ck_artifacts_valid_origin"),
        ),
        sa.CheckConstraint(
            "(origin = 'task_input' AND producer_assignment_id IS NULL AND "
            "producer_plan_step_id IS NULL AND source_workspace_id IS NULL) OR "
            "(origin = 'assignment' AND producer_assignment_id IS NOT NULL AND "
            "producer_plan_step_id IS NOT NULL AND source_workspace_id IS NOT NULL)",
            name=op.f("ck_artifacts_valid_origin_ownership"),
        ),
        sa.CheckConstraint(
            "byte_size >= 0",
            name=op.f("ck_artifacts_nonnegative_byte_size"),
        ),
        sa.CheckConstraint(
            "artifact_version >= 1",
            name=op.f("ck_artifacts_positive_artifact_version"),
        ),
        sa.ForeignKeyConstraint(
            ["producer_assignment_id"],
            ["assignments.assignment_id"],
            name=op.f("fk_artifacts_producer_assignment_id_assignments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["producer_plan_step_id"],
            ["plan_steps.plan_step_id"],
            name=op.f("fk_artifacts_producer_plan_step_id_plan_steps"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_workspace_id"],
            ["workspaces.workspace_id"],
            name=op.f("fk_artifacts_source_workspace_id_workspaces"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_artifact_id"],
            ["artifacts.artifact_id"],
            name=op.f("fk_artifacts_supersedes_artifact_id_artifacts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.task_id"],
            name=op.f("fk_artifacts_task_id_tasks"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("artifact_id", name=op.f("pk_artifacts")),
        sa.UniqueConstraint(
            "task_id",
            "contract_key",
            "artifact_version",
            name="uq_artifacts_task_contract_version",
        ),
        sa.UniqueConstraint(
            "task_id",
            "contract_key",
            "source_delivery_id",
            name="uq_artifacts_task_contract_delivery",
        ),
        sa.UniqueConstraint(
            "task_id",
            "storage_relative_path",
            name="uq_artifacts_task_storage_path",
        ),
    )
    for column in (
        "contract_key",
        "origin",
        "producer_assignment_id",
        "producer_plan_step_id",
        "source_delivery_id",
        "source_workspace_id",
        "status",
        "task_id",
    ):
        op.create_index(
            op.f(f"ix_artifacts_{column}"),
            "artifacts",
            [column],
            unique=False,
        )

    op.create_table(
        "artifact_input_bindings",
        sa.Column("input_binding_id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("plan_step_id", sa.String(length=36), nullable=False),
        sa.Column("artifact_id", sa.String(length=36), nullable=False),
        sa.Column("consumer_workspace_id", sa.String(length=36), nullable=False),
        sa.Column(
            "materialized_relative_path",
            sa.String(length=1024),
            nullable=False,
        ),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('materialized', 'revoked')",
            name=op.f("ck_artifact_input_bindings_valid_status"),
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["artifacts.artifact_id"],
            name=op.f("fk_artifact_input_bindings_artifact_id_artifacts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["consumer_workspace_id"],
            ["workspaces.workspace_id"],
            name=op.f(
                "fk_artifact_input_bindings_consumer_workspace_id_workspaces"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plan_step_id"],
            ["plan_steps.plan_step_id"],
            name=op.f("fk_artifact_input_bindings_plan_step_id_plan_steps"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.task_id"],
            name=op.f("fk_artifact_input_bindings_task_id_tasks"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "input_binding_id",
            name=op.f("pk_artifact_input_bindings"),
        ),
        sa.UniqueConstraint(
            "plan_step_id",
            "artifact_id",
            name="uq_artifact_input_bindings_step_artifact",
        ),
    )
    for column in (
        "artifact_id",
        "consumer_workspace_id",
        "plan_step_id",
        "status",
        "task_id",
    ):
        op.create_index(
            op.f(f"ix_artifact_input_bindings_{column}"),
            "artifact_input_bindings",
            [column],
            unique=False,
        )


def downgrade() -> None:
    op.drop_table("artifact_input_bindings")
    op.drop_table("artifacts")

    op.drop_index(
        op.f("ix_assignments_plan_step_id"),
        table_name="assignments",
    )
    with op.batch_alter_table("assignments") as batch_op:
        batch_op.drop_constraint(
            op.f("uq_assignments_plan_step_id"),
            type_="unique",
        )
        batch_op.drop_constraint(
            op.f("fk_assignments_plan_step_id_plan_steps"),
            type_="foreignkey",
        )
        batch_op.drop_column("plan_step_id")

    op.drop_table("plan_step_dependencies")
    op.drop_table("plan_steps")
    op.drop_table("task_execution_plans")
