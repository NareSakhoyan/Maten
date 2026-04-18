"""Add ingestion job recovery metadata."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0005_job_recovery"
down_revision = "0004_reconstruct_page_text"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ingestion_jobs", sa.Column("retry_of_job_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("ingestion_jobs", sa.Column("error_code", sa.Text(), nullable=True))
    op.add_column("ingestion_jobs", sa.Column("error_message_user", sa.Text(), nullable=True))
    op.add_column("ingestion_jobs", sa.Column("error_message_technical", sa.Text(), nullable=True))
    op.add_column("ingestion_jobs", sa.Column("next_steps", sa.JSON(), nullable=True))
    op.add_column("ingestion_jobs", sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("ingestion_jobs", sa.Column("can_retry", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("ingestion_jobs", sa.Column("last_retried_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_ingestion_jobs_retry_of_job_id", "ingestion_jobs", ["retry_of_job_id"], unique=False)
    op.create_foreign_key(
        "fk_ingestion_jobs_retry_of_job_id",
        "ingestion_jobs",
        "ingestion_jobs",
        ["retry_of_job_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.execute(
        sa.text(
            """
            UPDATE ingestion_jobs
            SET error_message_user = error_message,
                can_retry = CASE WHEN status = 'failed' THEN true ELSE can_retry END
            """
        )
    )


def downgrade() -> None:
    op.drop_constraint("fk_ingestion_jobs_retry_of_job_id", "ingestion_jobs", type_="foreignkey")
    op.drop_index("ix_ingestion_jobs_retry_of_job_id", table_name="ingestion_jobs")
    op.drop_column("ingestion_jobs", "last_retried_at")
    op.drop_column("ingestion_jobs", "can_retry")
    op.drop_column("ingestion_jobs", "retry_count")
    op.drop_column("ingestion_jobs", "next_steps")
    op.drop_column("ingestion_jobs", "error_message_technical")
    op.drop_column("ingestion_jobs", "error_message_user")
    op.drop_column("ingestion_jobs", "error_code")
    op.drop_column("ingestion_jobs", "retry_of_job_id")
