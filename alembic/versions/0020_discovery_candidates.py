"""discovery candidates

Revision ID: 0020_discovery_candidates
Revises: 0019_document_nayiri_lookup_runs
Create Date: 2026-05-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0020_discovery_candidates"
down_revision = "0019_document_nayiri_lookup_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE job_kind ADD VALUE IF NOT EXISTS 'discovery_build'")

    op.create_table(
        "discovery_candidates",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("normalized_form", sa.Text(), nullable=False),
        sa.Column("canonical_form_candidate", sa.Text(), nullable=True),
        sa.Column("occurrence_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("page_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sample_tokens", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("sample_contexts", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("sample_pages", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("resolution_status", sa.Text(), nullable=False),
        sa.Column("candidate_type", sa.Text(), nullable=False),
        sa.Column("interest_score", sa.Numeric(6, 2), nullable=False, server_default="0"),
        sa.Column("confidence_score", sa.Numeric(5, 2), nullable=True),
        sa.Column("ocr_risk_score", sa.Numeric(5, 2), nullable=True),
        sa.Column("morphology_plausibility_score", sa.Numeric(5, 2), nullable=True),
        sa.Column("definition_quality_score", sa.Numeric(5, 2), nullable=True),
        sa.Column("best_evidence_summary", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("review_status", sa.Text(), nullable=False, server_default="unreviewed"),
        sa.Column("reviewer_decision", sa.Text(), nullable=True),
        sa.Column("reviewer_note", sa.Text(), nullable=True),
        sa.Column("linked_lexeme_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["linked_lexeme_id"], ["lexemes.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "document_id", "normalized_form", name="uq_discovery_candidates_user_document_form"),
    )
    op.create_index(
        "ix_discovery_candidates_user_document_review",
        "discovery_candidates",
        ["user_id", "document_id", "review_status"],
    )
    op.create_index(
        "ix_discovery_candidates_user_document_type",
        "discovery_candidates",
        ["user_id", "document_id", "candidate_type"],
    )
    op.create_index(
        "ix_discovery_candidates_user_document_resolution",
        "discovery_candidates",
        ["user_id", "document_id", "resolution_status"],
    )
    op.create_index(
        "ix_discovery_candidates_user_document_interest",
        "discovery_candidates",
        ["user_id", "document_id", "interest_score"],
    )

    op.create_table(
        "discovery_build_runs",
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
        sa.Column("candidate_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("shown_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("suppressed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("summary_counts", sa.JSON(), nullable=True),
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
    op.create_index("ix_discovery_build_runs_user_id", "discovery_build_runs", ["user_id"])
    op.create_index("ix_discovery_build_runs_document_id", "discovery_build_runs", ["document_id"])


def downgrade() -> None:
    op.drop_index("ix_discovery_build_runs_document_id", table_name="discovery_build_runs")
    op.drop_index("ix_discovery_build_runs_user_id", table_name="discovery_build_runs")
    op.drop_table("discovery_build_runs")
    op.drop_index("ix_discovery_candidates_user_document_interest", table_name="discovery_candidates")
    op.drop_index("ix_discovery_candidates_user_document_resolution", table_name="discovery_candidates")
    op.drop_index("ix_discovery_candidates_user_document_type", table_name="discovery_candidates")
    op.drop_index("ix_discovery_candidates_user_document_review", table_name="discovery_candidates")
    op.drop_table("discovery_candidates")
