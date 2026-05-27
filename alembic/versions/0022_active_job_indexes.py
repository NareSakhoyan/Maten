"""active job indexes

Revision ID: 0022_active_job_indexes
Revises: 0021_performance_indexes
Create Date: 2026-05-21
"""

from __future__ import annotations

from alembic import op


revision = "0022_active_job_indexes"
down_revision = "0021_performance_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_ingestion_jobs_user_status_created", "ingestion_jobs", ["user_id", "status", "created_at"])
    op.create_index(
        "ix_reference_source_imports_user_status_created",
        "reference_source_imports",
        ["user_id", "status", "created_at"],
    )
    op.create_index(
        "ix_reference_match_runs_user_status_created",
        "reference_match_runs",
        ["user_id", "status", "created_at"],
    )
    op.create_index("ix_morphology_runs_user_status_created", "morphology_runs", ["user_id", "status", "created_at"])
    op.create_index(
        "ix_document_nayiri_lookup_runs_user_status_created",
        "document_nayiri_lookup_runs",
        ["user_id", "status", "created_at"],
    )
    op.create_index(
        "ix_discovery_build_runs_user_status_created",
        "discovery_build_runs",
        ["user_id", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_discovery_build_runs_user_status_created", table_name="discovery_build_runs")
    op.drop_index("ix_document_nayiri_lookup_runs_user_status_created", table_name="document_nayiri_lookup_runs")
    op.drop_index("ix_morphology_runs_user_status_created", table_name="morphology_runs")
    op.drop_index("ix_reference_match_runs_user_status_created", table_name="reference_match_runs")
    op.drop_index("ix_reference_source_imports_user_status_created", table_name="reference_source_imports")
    op.drop_index("ix_ingestion_jobs_user_status_created", table_name="ingestion_jobs")
