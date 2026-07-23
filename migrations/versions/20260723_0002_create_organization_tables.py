"""Create organizations and OrganizationSpec versions.

Revision ID: 20260723_0002
Revises: 20260723_0001
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260723_0002"
down_revision: str | None = "20260723_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=2_000), nullable=False),
        sa.Column(
            "current_published_version_id",
            sa.String(length=36),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.user_id"],
            name=op.f("fk_organizations_owner_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("organization_id", name=op.f("pk_organizations")),
    )
    op.create_index(
        op.f("ix_organizations_owner_user_id"),
        "organizations",
        ["owner_user_id"],
        unique=False,
    )
    op.create_table(
        "organization_spec_versions",
        sa.Column("spec_version_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("spec_payload", sa.JSON(), nullable=False),
        sa.Column("source_request", sa.String(length=4_000), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('proposal', 'confirmed', 'published', "
            "'superseded', 'archived')",
            name=op.f("ck_organization_spec_versions_valid_status"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.organization_id"],
            name=op.f("fk_organization_spec_versions_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.user_id"],
            name=op.f("fk_organization_spec_versions_owner_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "spec_version_id", name=op.f("pk_organization_spec_versions")
        ),
        sa.UniqueConstraint(
            "organization_id",
            "version_number",
            name="uq_organization_spec_versions_organization_version",
        ),
    )
    op.create_index(
        op.f("ix_organization_spec_versions_organization_id"),
        "organization_spec_versions",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_organization_spec_versions_owner_user_id"),
        "organization_spec_versions",
        ["owner_user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_organization_spec_versions_status"),
        "organization_spec_versions",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_organization_spec_versions_status"),
        table_name="organization_spec_versions",
    )
    op.drop_index(
        op.f("ix_organization_spec_versions_owner_user_id"),
        table_name="organization_spec_versions",
    )
    op.drop_index(
        op.f("ix_organization_spec_versions_organization_id"),
        table_name="organization_spec_versions",
    )
    op.drop_table("organization_spec_versions")
    op.drop_index(
        op.f("ix_organizations_owner_user_id"),
        table_name="organizations",
    )
    op.drop_table("organizations")
