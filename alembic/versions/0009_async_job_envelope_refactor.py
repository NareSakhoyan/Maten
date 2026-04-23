"""Add unified async job envelope fields and async reference import storage."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0009_async_job_envelope_refactor"
down_revision = "0008_job_progress_tracking"
branch_labels = None
depends_on = None


job_result_resource_type = postgresql.ENUM(
    "document",
    "reference_source",
    "reference_match_run",
    name="job_result_resource_type",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    job_result_resource_type.create(bind, checkfirst=True)

    op.add_column(
        "ingestion_jobs",
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.add_column("ingestion_jobs", sa.Column("result_resource_type", job_result_resource_type, nullable=True))
    op.add_column("ingestion_jobs", sa.Column("result_resource_id", sa.Text(), nullable=True))

    op.add_column(
        "reference_match_runs",
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.add_column("reference_match_runs", sa.Column("error_code", sa.Text(), nullable=True))
    op.add_column("reference_match_runs", sa.Column("error_message_user", sa.Text(), nullable=True))
    op.add_column("reference_match_runs", sa.Column("next_steps", sa.JSON(), nullable=True))
    op.add_column("reference_match_runs", sa.Column("result_resource_type", job_result_resource_type, nullable=True))
    op.add_column("reference_match_runs", sa.Column("result_resource_id", sa.Text(), nullable=True))

    op.add_column(
        "reference_source_imports",
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.add_column("reference_source_imports", sa.Column("storage_bucket", sa.Text(), nullable=True))
    op.add_column("reference_source_imports", sa.Column("storage_path", sa.Text(), nullable=True))
    op.add_column("reference_source_imports", sa.Column("mime_type", sa.Text(), nullable=True))
    op.add_column("reference_source_imports", sa.Column("file_size_bytes", sa.Integer(), nullable=True))
    op.add_column("reference_source_imports", sa.Column("error_code", sa.Text(), nullable=True))
    op.add_column("reference_source_imports", sa.Column("error_message_user", sa.Text(), nullable=True))
    op.add_column("reference_source_imports", sa.Column("next_steps", sa.JSON(), nullable=True))
    op.add_column("reference_source_imports", sa.Column("result_resource_type", job_result_resource_type, nullable=True))
    op.add_column("reference_source_imports", sa.Column("result_resource_id", sa.Text(), nullable=True))

    op.execute(
        sa.text(
            """
            UPDATE ingestion_jobs
            SET result_resource_type = 'document',
                result_resource_id = document_id::text,
                updated_at = COALESCE(finished_at, started_at, created_at, now())
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE reference_match_runs
            SET result_resource_type = 'reference_match_run',
                result_resource_id = id::text,
                error_message_user = error_message,
                updated_at = COALESCE(finished_at, started_at, created_at, now())
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE reference_source_imports
            SET result_resource_type = 'reference_source',
                result_resource_id = source_id::text,
                error_message_user = error_message,
                updated_at = COALESCE(finished_at, started_at, created_at, now())
            """
        )
    )


def downgrade() -> None:
    op.drop_column("reference_source_imports", "result_resource_id")
    op.drop_column("reference_source_imports", "result_resource_type")
    op.drop_column("reference_source_imports", "next_steps")
    op.drop_column("reference_source_imports", "error_message_user")
    op.drop_column("reference_source_imports", "error_code")
    op.drop_column("reference_source_imports", "file_size_bytes")
    op.drop_column("reference_source_imports", "mime_type")
    op.drop_column("reference_source_imports", "storage_path")
    op.drop_column("reference_source_imports", "storage_bucket")
    op.drop_column("reference_source_imports", "updated_at")

    op.drop_column("reference_match_runs", "result_resource_id")
    op.drop_column("reference_match_runs", "result_resource_type")
    op.drop_column("reference_match_runs", "next_steps")
    op.drop_column("reference_match_runs", "error_message_user")
    op.drop_column("reference_match_runs", "error_code")
    op.drop_column("reference_match_runs", "updated_at")

    op.drop_column("ingestion_jobs", "result_resource_id")
    op.drop_column("ingestion_jobs", "result_resource_type")
    op.drop_column("ingestion_jobs", "updated_at")

    bind = op.get_bind()
    job_result_resource_type.drop(bind, checkfirst=True)
