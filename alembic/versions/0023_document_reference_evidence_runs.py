"""document reference evidence runs

Revision ID: 0023_document_reference_evidence_runs
Revises: 0022_active_job_indexes
Create Date: 2026-05-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0023_document_reference_evidence_runs"
down_revision = "0022_active_job_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "discovery_build_runs",
        sa.Column("build_mode", sa.Text(), nullable=False, server_default="full"),
    )
    op.add_column(
        "discovery_build_runs",
        sa.Column("reference_source_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "discovery_build_runs",
        sa.Column("reference_source_import_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "discovery_build_runs",
        sa.Column("matched_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "discovery_build_runs",
        sa.Column("unmatched_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_foreign_key(
        "fk_discovery_build_runs_reference_source_id",
        "discovery_build_runs",
        "reference_sources",
        ["reference_source_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_discovery_build_runs_reference_source_import_id",
        "discovery_build_runs",
        "reference_source_imports",
        ["reference_source_import_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_discovery_build_runs_reference_source_id",
        "discovery_build_runs",
        ["reference_source_id"],
    )
    op.create_index(
        "ix_discovery_build_runs_reference_import_id",
        "discovery_build_runs",
        ["reference_source_import_id"],
    )
    op.alter_column("discovery_build_runs", "build_mode", server_default=None)
    op.alter_column("discovery_build_runs", "matched_count", server_default=None)
    op.alter_column("discovery_build_runs", "unmatched_count", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_discovery_build_runs_reference_import_id", table_name="discovery_build_runs")
    op.drop_index("ix_discovery_build_runs_reference_source_id", table_name="discovery_build_runs")
    op.drop_constraint(
        "fk_discovery_build_runs_reference_source_import_id",
        "discovery_build_runs",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_discovery_build_runs_reference_source_id",
        "discovery_build_runs",
        type_="foreignkey",
    )
    op.drop_column("discovery_build_runs", "unmatched_count")
    op.drop_column("discovery_build_runs", "matched_count")
    op.drop_column("discovery_build_runs", "reference_source_import_id")
    op.drop_column("discovery_build_runs", "reference_source_id")
    op.drop_column("discovery_build_runs", "build_mode")
