"""Add ingestion job resume metadata.

Revision ID: 0026_ingestion_job_resume_metadata
Revises: 0025_lexeme_form_mappings
Create Date: 2026-06-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0026_ingestion_job_resume_metadata"
down_revision = "0025_lexeme_form_mappings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ingestion_jobs",
        sa.Column("resume_of_job_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "ingestion_jobs",
        sa.Column("resume_from_page", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_ingestion_jobs_resume_of_job_id",
        "ingestion_jobs",
        "ingestion_jobs",
        ["resume_of_job_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_ingestion_jobs_resume_of_job_id",
        "ingestion_jobs",
        ["resume_of_job_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_ingestion_jobs_resume_of_job_id", table_name="ingestion_jobs")
    op.drop_constraint("fk_ingestion_jobs_resume_of_job_id", "ingestion_jobs", type_="foreignkey")
    op.drop_column("ingestion_jobs", "resume_from_page")
    op.drop_column("ingestion_jobs", "resume_of_job_id")
