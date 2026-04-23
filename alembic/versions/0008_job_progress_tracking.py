"""Add shared job progress tracking tables and fields."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0008_job_progress_tracking"
down_revision = "0007_reference_import_metadata"
branch_labels = None
depends_on = None


reference_import_status = postgresql.ENUM(
    "queued",
    "running",
    "completed",
    "failed",
    name="reference_import_status",
    create_type=False,
)
job_kind = postgresql.ENUM(
    "ingestion",
    "reference_import",
    "reference_matching",
    name="job_kind",
    create_type=False,
)
reference_import_method = postgresql.ENUM(
    "txt",
    "csv",
    "docx",
    "pdf_text",
    "pdf_ocr",
    "xlsx",
    name="reference_import_method",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    reference_import_status.create(bind, checkfirst=True)
    job_kind.create(bind, checkfirst=True)

    op.add_column("ingestion_jobs", sa.Column("current_stage_code", sa.Text(), nullable=True))
    op.add_column("ingestion_jobs", sa.Column("current_stage_label", sa.Text(), nullable=True))
    op.add_column("ingestion_jobs", sa.Column("stage_message_user", sa.Text(), nullable=True))
    op.add_column("ingestion_jobs", sa.Column("items_processed", sa.Integer(), nullable=True))
    op.add_column("ingestion_jobs", sa.Column("items_total", sa.Integer(), nullable=True))

    op.add_column("reference_match_runs", sa.Column("current_stage_code", sa.Text(), nullable=True))
    op.add_column("reference_match_runs", sa.Column("current_stage_label", sa.Text(), nullable=True))
    op.add_column("reference_match_runs", sa.Column("stage_message_user", sa.Text(), nullable=True))
    op.add_column(
        "reference_match_runs",
        sa.Column("progress_percent", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("reference_match_runs", sa.Column("items_processed", sa.Integer(), nullable=True))
    op.add_column("reference_match_runs", sa.Column("items_total", sa.Integer(), nullable=True))

    op.create_table(
        "reference_source_imports",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("file_name", sa.Text(), nullable=True),
        sa.Column("file_type", sa.Text(), nullable=True),
        sa.Column("status", reference_import_status, nullable=False, server_default="queued"),
        sa.Column("import_method", reference_import_method, nullable=True),
        sa.Column("rows_read", sa.Integer(), nullable=True),
        sa.Column("rows_imported", sa.Integer(), nullable=True),
        sa.Column("rows_skipped", sa.Integer(), nullable=True),
        sa.Column("warning_message", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("current_stage_code", sa.Text(), nullable=True),
        sa.Column("current_stage_label", sa.Text(), nullable=True),
        sa.Column("stage_message_user", sa.Text(), nullable=True),
        sa.Column("progress_percent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("items_processed", sa.Integer(), nullable=True),
        sa.Column("items_total", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["reference_sources.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_reference_source_imports_source_id",
        "reference_source_imports",
        ["source_id"],
        unique=False,
    )
    op.create_index(
        "ix_reference_source_imports_user_id",
        "reference_source_imports",
        ["user_id"],
        unique=False,
    )

    op.create_table(
        "job_stage_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("job_kind", job_kind, nullable=False),
        sa.Column("job_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("stage_code", sa.Text(), nullable=False),
        sa.Column("stage_label", sa.Text(), nullable=False),
        sa.Column("message_user", sa.Text(), nullable=True),
        sa.Column("progress_percent", sa.Integer(), nullable=True),
        sa.Column("items_processed", sa.Integer(), nullable=True),
        sa.Column("items_total", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index(
        "ix_job_stage_events_job_kind_job_id",
        "job_stage_events",
        ["job_kind", "job_id"],
        unique=False,
    )
    op.create_index("ix_job_stage_events_user_id", "job_stage_events", ["user_id"], unique=False)

    op.execute(
        sa.text(
            """
            UPDATE ingestion_jobs
            SET current_stage_code = COALESCE(step, 'queued'),
                current_stage_label = CASE COALESCE(step, 'queued')
                    WHEN 'completed' THEN 'Completed'
                    WHEN 'failed' THEN 'Failed'
                    WHEN 'queued' THEN 'Queued'
                    ELSE 'Processing'
                END
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE reference_match_runs
            SET current_stage_code = CASE status
                    WHEN 'completed' THEN 'completed'
                    WHEN 'failed' THEN 'failed'
                    ELSE 'queued'
                END,
                current_stage_label = CASE status
                    WHEN 'completed' THEN 'Completed'
                    WHEN 'failed' THEN 'Failed'
                    ELSE 'Queued'
                END
            """
        )
    )


def downgrade() -> None:
    op.drop_index("ix_job_stage_events_user_id", table_name="job_stage_events")
    op.drop_index("ix_job_stage_events_job_kind_job_id", table_name="job_stage_events")
    op.drop_table("job_stage_events")

    op.drop_index("ix_reference_source_imports_user_id", table_name="reference_source_imports")
    op.drop_index("ix_reference_source_imports_source_id", table_name="reference_source_imports")
    op.drop_table("reference_source_imports")

    op.drop_column("reference_match_runs", "items_total")
    op.drop_column("reference_match_runs", "items_processed")
    op.drop_column("reference_match_runs", "progress_percent")
    op.drop_column("reference_match_runs", "stage_message_user")
    op.drop_column("reference_match_runs", "current_stage_label")
    op.drop_column("reference_match_runs", "current_stage_code")

    op.drop_column("ingestion_jobs", "items_total")
    op.drop_column("ingestion_jobs", "items_processed")
    op.drop_column("ingestion_jobs", "stage_message_user")
    op.drop_column("ingestion_jobs", "current_stage_label")
    op.drop_column("ingestion_jobs", "current_stage_code")

    bind = op.get_bind()
    job_kind.drop(bind, checkfirst=True)
    reference_import_status.drop(bind, checkfirst=True)
