"""performance indexes

Revision ID: 0021_performance_indexes
Revises: 0020_discovery_candidates
Create Date: 2026-05-21
"""

from __future__ import annotations

from alembic import op


revision = "0021_performance_indexes"
down_revision = "0020_discovery_candidates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_occurrences_document_normalized_token",
        "occurrences",
        ["document_id", "normalized_token"],
    )
    op.create_index(
        "ix_lexicon_group_index_documents_document_form",
        "lexicon_group_index_documents",
        ["user_id", "document_id", "normalized_form"],
    )
    op.create_index(
        "ix_morphology_analyses_user_document_token",
        "morphology_analyses",
        ["user_id", "document_id", "token_normalized"],
    )
    op.create_index(
        "ix_morphology_analyses_user_document_lemma",
        "morphology_analyses",
        ["user_id", "document_id", "lemma_normalized"],
    )


def downgrade() -> None:
    op.drop_index("ix_morphology_analyses_user_document_lemma", table_name="morphology_analyses")
    op.drop_index("ix_morphology_analyses_user_document_token", table_name="morphology_analyses")
    op.drop_index(
        "ix_lexicon_group_index_documents_document_form",
        table_name="lexicon_group_index_documents",
    )
    op.drop_index("ix_occurrences_document_normalized_token", table_name="occurrences")
