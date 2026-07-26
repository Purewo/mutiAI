"""add Runtime capability profiles and feasibility checks

Revision ID: 20260726_0011
Revises: 20260725_0010
Create Date: 2026-07-26 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260726_0011"
down_revision: str | Sequence[str] | None = "20260725_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "runtime_capability_profiles",
        sa.Column("capability_profile_id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("runtime_binding_id", sa.String(length=36), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("profile_payload", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("trusted", sa.Boolean(), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "revision >= 1",
            name=op.f("ck_runtime_capability_profiles_positive_revision"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.user_id"],
            name=op.f(
                "fk_runtime_capability_profiles_owner_user_id_users"
            ),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["runtime_binding_id"],
            ["runtime_bindings.runtime_binding_id"],
            name=op.f(
                "fk_runtime_capability_profiles_runtime_binding_id_runtime_bindings"
            ),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "capability_profile_id",
            name=op.f("pk_runtime_capability_profiles"),
        ),
        sa.UniqueConstraint(
            "runtime_binding_id",
            "revision",
            name="uq_runtime_capability_profiles_binding_revision",
        ),
    )
    op.create_index(
        op.f("ix_runtime_capability_profiles_owner_user_id"),
        "runtime_capability_profiles",
        ["owner_user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_runtime_capability_profiles_runtime_binding_id"),
        "runtime_capability_profiles",
        ["runtime_binding_id"],
        unique=False,
    )

    op.create_table(
        "feasibility_checks",
        sa.Column("feasibility_check_id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("target_type", sa.String(length=50), nullable=False),
        sa.Column("target_id", sa.String(length=100), nullable=False),
        sa.Column("phase", sa.String(length=50), nullable=False),
        sa.Column("validator_version", sa.String(length=20), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("requirements_payload", sa.JSON(), nullable=False),
        sa.Column("profile_revisions_payload", sa.JSON(), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("findings_payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('feasible', 'conditional', 'blocked', "
            "'capability_unknown')",
            name=op.f("ck_feasibility_checks_valid_outcome"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.user_id"],
            name=op.f("fk_feasibility_checks_owner_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "feasibility_check_id",
            name=op.f("pk_feasibility_checks"),
        ),
    )
    for column in (
        "owner_user_id",
        "target_type",
        "target_id",
        "phase",
        "input_hash",
        "outcome",
    ):
        op.create_index(
            op.f(f"ix_feasibility_checks_{column}"),
            "feasibility_checks",
            [column],
            unique=False,
        )

    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(
            sa.Column(
                "capability_requirements",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_column("capability_requirements")

    for column in (
        "outcome",
        "input_hash",
        "phase",
        "target_id",
        "target_type",
        "owner_user_id",
    ):
        op.drop_index(
            op.f(f"ix_feasibility_checks_{column}"),
            table_name="feasibility_checks",
        )
    op.drop_table("feasibility_checks")
    op.drop_index(
        op.f("ix_runtime_capability_profiles_runtime_binding_id"),
        table_name="runtime_capability_profiles",
    )
    op.drop_index(
        op.f("ix_runtime_capability_profiles_owner_user_id"),
        table_name="runtime_capability_profiles",
    )
    op.drop_table("runtime_capability_profiles")
