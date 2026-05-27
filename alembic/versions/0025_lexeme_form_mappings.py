"""Add lexeme form mapping projection.

Revision ID: 0025_lexeme_form_mappings
Revises: 0024_pioner_ner_evidence
Create Date: 2026-05-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0025_lexeme_form_mappings"
down_revision = "0024_pioner_ner_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "lexeme_form_mappings",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=True),
        sa.Column("surface_form", sa.Text(), nullable=False),
        sa.Column("normalized_surface_form", sa.Text(), nullable=False),
        sa.Column("dictionary_lemma", sa.Text(), nullable=False),
        sa.Column("normalized_dictionary_lemma", sa.Text(), nullable=False),
        sa.Column("pos", sa.Text(), nullable=True),
        sa.Column("language_profile", sa.Text(), nullable=False, server_default="unknown"),
        sa.Column("mapping_type", sa.Text(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("source_key", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Numeric(5, 2), nullable=True),
        sa.Column("review_status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "normalized_surface_form",
            "normalized_dictionary_lemma",
            "language_profile",
            "source_type",
            name="uq_lexeme_form_mappings_form_lemma_profile_source",
        ),
    )
    op.create_index("ix_lexeme_form_mappings_user_form", "lexeme_form_mappings", ["user_id", "normalized_surface_form"])
    op.create_index(
        "ix_lexeme_form_mappings_user_lemma",
        "lexeme_form_mappings",
        ["user_id", "normalized_dictionary_lemma"],
    )
    op.create_index("ix_lexeme_form_mappings_source", "lexeme_form_mappings", ["source_key"])
    op.alter_column("lexeme_form_mappings", "language_profile", server_default=None)
    op.alter_column("lexeme_form_mappings", "review_status", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_lexeme_form_mappings_source", table_name="lexeme_form_mappings")
    op.drop_index("ix_lexeme_form_mappings_user_lemma", table_name="lexeme_form_mappings")
    op.drop_index("ix_lexeme_form_mappings_user_form", table_name="lexeme_form_mappings")
    op.drop_table("lexeme_form_mappings")
