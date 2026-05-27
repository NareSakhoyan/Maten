"""document nayiri lookup runs

Revision ID: 0019_document_nayiri_lookup_runs
Revises: 0018_lexicon_index_rls
Create Date: 2026-05-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0019_document_nayiri_lookup_runs"
down_revision = "0018_lexicon_index_rls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE job_kind ADD VALUE IF NOT EXISTS 'nayiri_trusted_lookup'")

    op.create_table(
        "document_nayiri_lookup_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "queued",
                "running",
                "completed",
                "failed",
                name="morphology_run_status",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("checked_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("current_stage_code", sa.Text(), nullable=True),
        sa.Column("current_stage_label", sa.Text(), nullable=True),
        sa.Column("stage_message_user", sa.Text(), nullable=True),
        sa.Column("progress_percent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("items_processed", sa.Integer(), nullable=True),
        sa.Column("items_total", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message_user", sa.Text(), nullable=True),
        sa.Column("next_steps", sa.JSON(), nullable=True),
        sa.Column(
            "result_resource_type",
            postgresql.ENUM(
                "document",
                "reference_source",
                "reference_match_run",
                name="job_result_resource_type",
                create_type=False,
            ),
            nullable=True,
        ),
        sa.Column("result_resource_id", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("can_retry", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_retried_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_document_nayiri_lookup_runs_user_id", "document_nayiri_lookup_runs", ["user_id"])
    op.create_index(
        op.f("ix_document_nayiri_lookup_runs_document_id"),
        "document_nayiri_lookup_runs",
        ["document_id"],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_document_nayiri_lookup_runs_document_id"), table_name="document_nayiri_lookup_runs")
    op.drop_index("ix_document_nayiri_lookup_runs_user_id", table_name="document_nayiri_lookup_runs")
    op.drop_table("document_nayiri_lookup_runs")
