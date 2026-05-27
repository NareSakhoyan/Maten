"""Add PIE morphology runs, analyses, and eligibility metadata."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0014_morphology_analysis"
down_revision = "0013_external_lookup_cache"
branch_labels = None
depends_on = None


morphology_run_status = postgresql.ENUM(
    "queued",
    "running",
    "completed",
    "failed",
    name="morphology_run_status",
    create_type=False,
)
morphology_analysis_status = postgresql.ENUM(
    "completed",
    "failed",
    "skipped",
    name="morphology_analysis_status",
    create_type=False,
)
job_result_resource_type = postgresql.ENUM(
    "document",
    "reference_source",
    "reference_match_run",
    name="job_result_resource_type",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("ALTER TYPE job_kind ADD VALUE IF NOT EXISTS 'morphology'")
    morphology_run_status.create(bind, checkfirst=True)
    morphology_analysis_status.create(bind, checkfirst=True)
    job_result_resource_type.create(bind, checkfirst=True)

    op.add_column("documents", sa.Column("language_stage", sa.Text(), nullable=True))
    op.add_column("documents", sa.Column("morphology_profile", sa.Text(), nullable=True))
    op.add_column("reference_sources", sa.Column("language_stage", sa.Text(), nullable=True))
    op.add_column("reference_sources", sa.Column("morphology_profile", sa.Text(), nullable=True))

    op.create_table(
        "morphology_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reference_source_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("analyzer_provider", sa.Text(), nullable=False, server_default="pie"),
        sa.Column("analyzer_model_key", sa.Text(), nullable=False),
        sa.Column("analyzer_version", sa.Text(), nullable=True),
        sa.Column("status", morphology_run_status, nullable=False, server_default="queued"),
        sa.Column("completed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("current_stage_code", sa.Text(), nullable=True),
        sa.Column("current_stage_label", sa.Text(), nullable=True),
        sa.Column("stage_message_user", sa.Text(), nullable=True),
        sa.Column("progress_percent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("items_processed", sa.Integer(), nullable=True),
        sa.Column("items_total", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message_user", sa.Text(), nullable=True),
        sa.Column("next_steps", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("result_resource_type", job_result_resource_type, nullable=True),
        sa.Column("result_resource_id", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("can_retry", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_retried_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reference_source_id"], ["reference_sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_morphology_runs_user_id", "morphology_runs", ["user_id"], unique=False)
    op.create_index("ix_morphology_runs_document_id", "morphology_runs", ["document_id"], unique=False)
    op.create_index(
        "ix_morphology_runs_reference_source_id",
        "morphology_runs",
        ["reference_source_id"],
        unique=False,
    )

    op.create_table(
        "morphology_analyses",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("occurrence_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("page_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reference_source_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reference_entry_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("token_surface", sa.Text(), nullable=False),
        sa.Column("token_normalized", sa.Text(), nullable=False),
        sa.Column("lemma", sa.Text(), nullable=True),
        sa.Column("lemma_normalized", sa.Text(), nullable=True),
        sa.Column("pos", sa.Text(), nullable=True),
        sa.Column("morph_features", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("analyzer_provider", sa.Text(), nullable=False, server_default="pie"),
        sa.Column("analyzer_model_key", sa.Text(), nullable=False),
        sa.Column("analyzer_version", sa.Text(), nullable=True),
        sa.Column("analysis_status", morphology_analysis_status, nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["occurrence_id"], ["occurrences.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["page_id"], ["document_pages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reference_source_id"], ["reference_sources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reference_entry_id"], ["reference_entries.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_morphology_analyses_occurrence_id", "morphology_analyses", ["occurrence_id"], unique=False)
    op.create_index(
        "ix_morphology_analyses_reference_entry_id",
        "morphology_analyses",
        ["reference_entry_id"],
        unique=False,
    )
    op.create_index(
        "ix_morphology_analyses_token_normalized",
        "morphology_analyses",
        ["token_normalized"],
        unique=False,
    )
    op.create_index(
        "ix_morphology_analyses_lemma_normalized",
        "morphology_analyses",
        ["lemma_normalized"],
        unique=False,
    )
    op.create_index(
        "ix_morphology_analyses_user_document",
        "morphology_analyses",
        ["user_id", "document_id"],
        unique=False,
    )
    op.create_index(
        "ix_morphology_analyses_reference_source_id",
        "morphology_analyses",
        ["reference_source_id"],
        unique=False,
    )
    op.create_index("ix_morphology_analyses_user_id", "morphology_analyses", ["user_id"], unique=False)

    op.alter_column("morphology_runs", "analyzer_provider", server_default=None)
    op.alter_column("morphology_runs", "status", server_default=None)
    op.alter_column("morphology_runs", "completed_count", server_default=None)
    op.alter_column("morphology_runs", "skipped_count", server_default=None)
    op.alter_column("morphology_runs", "failed_count", server_default=None)
    op.alter_column("morphology_runs", "progress_percent", server_default=None)
    op.alter_column("morphology_runs", "retry_count", server_default=None)
    op.alter_column("morphology_runs", "can_retry", server_default=None)
    op.alter_column("morphology_analyses", "analyzer_provider", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_morphology_analyses_user_id", table_name="morphology_analyses")
    op.drop_index("ix_morphology_analyses_reference_source_id", table_name="morphology_analyses")
    op.drop_index("ix_morphology_analyses_user_document", table_name="morphology_analyses")
    op.drop_index("ix_morphology_analyses_lemma_normalized", table_name="morphology_analyses")
    op.drop_index("ix_morphology_analyses_token_normalized", table_name="morphology_analyses")
    op.drop_index("ix_morphology_analyses_reference_entry_id", table_name="morphology_analyses")
    op.drop_index("ix_morphology_analyses_occurrence_id", table_name="morphology_analyses")
    op.drop_table("morphology_analyses")

    op.drop_index("ix_morphology_runs_reference_source_id", table_name="morphology_runs")
    op.drop_index("ix_morphology_runs_document_id", table_name="morphology_runs")
    op.drop_index("ix_morphology_runs_user_id", table_name="morphology_runs")
    op.drop_table("morphology_runs")

    op.drop_column("reference_sources", "morphology_profile")
    op.drop_column("reference_sources", "language_stage")
    op.drop_column("documents", "morphology_profile")
    op.drop_column("documents", "language_stage")

    bind = op.get_bind()
    morphology_analysis_status.drop(bind, checkfirst=True)
    morphology_run_status.drop(bind, checkfirst=True)
