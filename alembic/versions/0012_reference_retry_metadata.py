"""Add retry metadata for reference imports and matching runs."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0012_reference_retry_metadata"
down_revision = "0011_source_first_match"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "reference_source_imports",
        sa.Column("retry_of_job_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "reference_source_imports",
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "reference_source_imports",
        sa.Column("can_retry", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "reference_source_imports",
        sa.Column("last_retried_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_reference_source_imports_retry_of_job_id",
        "reference_source_imports",
        ["retry_of_job_id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_reference_source_imports_retry_of_job_id",
        "reference_source_imports",
        "reference_source_imports",
        ["retry_of_job_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.add_column(
        "reference_match_runs",
        sa.Column("retry_of_job_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "reference_match_runs",
        sa.Column("requested_view", sa.Text(), nullable=False, server_default="candidates"),
    )
    op.add_column(
        "reference_match_runs",
        sa.Column("include_fuzzy", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "reference_match_runs",
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "reference_match_runs",
        sa.Column("can_retry", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "reference_match_runs",
        sa.Column("last_retried_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_reference_match_runs_retry_of_job_id",
        "reference_match_runs",
        ["retry_of_job_id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_reference_match_runs_retry_of_job_id",
        "reference_match_runs",
        "reference_match_runs",
        ["retry_of_job_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.execute(
        sa.text(
            """
            UPDATE reference_source_imports
            SET can_retry = CASE WHEN status = 'failed' THEN true ELSE can_retry END
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE reference_match_runs
            SET can_retry = CASE WHEN status = 'failed' THEN true ELSE can_retry END
            """
        )
    )

    op.alter_column("reference_source_imports", "retry_count", server_default=None)
    op.alter_column("reference_source_imports", "can_retry", server_default=None)
    op.alter_column("reference_match_runs", "requested_view", server_default=None)
    op.alter_column("reference_match_runs", "include_fuzzy", server_default=None)
    op.alter_column("reference_match_runs", "retry_count", server_default=None)
    op.alter_column("reference_match_runs", "can_retry", server_default=None)


def downgrade() -> None:
    op.drop_constraint("fk_reference_match_runs_retry_of_job_id", "reference_match_runs", type_="foreignkey")
    op.drop_index("ix_reference_match_runs_retry_of_job_id", table_name="reference_match_runs")
    op.drop_column("reference_match_runs", "last_retried_at")
    op.drop_column("reference_match_runs", "can_retry")
    op.drop_column("reference_match_runs", "retry_count")
    op.drop_column("reference_match_runs", "include_fuzzy")
    op.drop_column("reference_match_runs", "requested_view")
    op.drop_column("reference_match_runs", "retry_of_job_id")

    op.drop_constraint("fk_reference_source_imports_retry_of_job_id", "reference_source_imports", type_="foreignkey")
    op.drop_index("ix_reference_source_imports_retry_of_job_id", table_name="reference_source_imports")
    op.drop_column("reference_source_imports", "last_retried_at")
    op.drop_column("reference_source_imports", "can_retry")
    op.drop_column("reference_source_imports", "retry_count")
    op.drop_column("reference_source_imports", "retry_of_job_id")
