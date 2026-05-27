"""Add pioNER named-entity evidence tables.

Revision ID: 0024_pioner_ner_evidence
Revises: 0023_document_reference_evidence_runs
Create Date: 2026-05-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0024_pioner_ner_evidence"
down_revision = "0023_document_reference_evidence_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ner_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_key", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column("dataset_split", sa.Text(), nullable=False, server_default="unknown"),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("license", sa.Text(), nullable=True),
        sa.Column("version", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider_key", "source_kind", "dataset_split", name="uq_ner_sources_provider_kind_split"),
    )
    op.create_index("ix_ner_sources_provider_key", "ner_sources", ["provider_key"])
    op.create_index("ix_ner_sources_active", "ner_sources", ["is_active"])

    op.create_table(
        "ner_entity_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entity_surface", sa.Text(), nullable=False),
        sa.Column("normalized_surface", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("occurrence_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("confidence", sa.Numeric(5, 2), nullable=True),
        sa.Column("sample_contexts", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["ner_sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_id",
            "entity_surface",
            "normalized_surface",
            "entity_type",
            name="uq_ner_entity_entries_source_surface_type",
        ),
    )
    op.create_index("ix_ner_entity_entries_source_id", "ner_entity_entries", ["source_id"])
    op.create_index("ix_ner_entity_entries_normalized_surface", "ner_entity_entries", ["normalized_surface"])
    op.create_index("ix_ner_entity_entries_entity_type", "ner_entity_entries", ["entity_type"])
    op.create_index("ix_ner_entity_entries_source_normalized", "ner_entity_entries", ["source_id", "normalized_surface"])
    op.alter_column("ner_sources", "dataset_split", server_default=None)
    op.alter_column("ner_sources", "is_active", server_default=None)
    op.alter_column("ner_entity_entries", "occurrence_count", server_default=None)
    op.alter_column("ner_entity_entries", "sample_contexts", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_ner_entity_entries_source_normalized", table_name="ner_entity_entries")
    op.drop_index("ix_ner_entity_entries_entity_type", table_name="ner_entity_entries")
    op.drop_index("ix_ner_entity_entries_normalized_surface", table_name="ner_entity_entries")
    op.drop_index("ix_ner_entity_entries_source_id", table_name="ner_entity_entries")
    op.drop_table("ner_entity_entries")
    op.drop_index("ix_ner_sources_active", table_name="ner_sources")
    op.drop_index("ix_ner_sources_provider_key", table_name="ner_sources")
    op.drop_table("ner_sources")
