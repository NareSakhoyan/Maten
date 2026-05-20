"""Add lexicon group index read model for fast reviewer queues."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0016_lexicon_group_index"
down_revision = "0015_document_workflows"
branch_labels = None
depends_on = None


occurrence_script_type = postgresql.ENUM(
    "armenian",
    "latin",
    "mixed",
    "digit_mixed",
    "other",
    name="occurrence_script_type",
    create_type=False,
)


def upgrade() -> None:
    op.create_table(
        "lexicon_group_index",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("normalized_form", sa.Text(), nullable=False),
        sa.Column("occurrence_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("document_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("page_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dominant_script_type", occurrence_script_type, nullable=False),
        sa.Column("group_state", sa.Text(), nullable=False, server_default="unreviewed"),
        sa.Column("linked_lexeme_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("linked_lexeme_canonical_form", sa.Text(), nullable=True),
        sa.Column("sample_tokens", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column("sample_contexts", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column(
            "sample_document_titles",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("script_counts", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "normalized_form", name="pk_lexicon_group_index"),
    )
    op.create_index(
        "ix_lexicon_group_index_user_state_script",
        "lexicon_group_index",
        ["user_id", "group_state", "dominant_script_type"],
    )
    op.create_index(
        "ix_lexicon_group_index_user_occurrence_count",
        "lexicon_group_index",
        ["user_id", "occurrence_count"],
    )
    op.create_index(
        "ix_lexicon_group_index_user_normalized_form",
        "lexicon_group_index",
        ["user_id", "normalized_form"],
    )

    op.create_table(
        "lexicon_group_index_documents",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("normalized_form", sa.Text(), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("occurrence_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("page_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sample_tokens", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column("sample_contexts", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column("page_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint(
            "user_id",
            "normalized_form",
            "document_id",
            name="pk_lexicon_group_index_documents",
        ),
    )
    op.create_index(
        "ix_lexicon_group_index_documents_document",
        "lexicon_group_index_documents",
        ["user_id", "document_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_lexicon_group_index_documents_document", table_name="lexicon_group_index_documents")
    op.drop_table("lexicon_group_index_documents")
    op.drop_index("ix_lexicon_group_index_user_normalized_form", table_name="lexicon_group_index")
    op.drop_index("ix_lexicon_group_index_user_occurrence_count", table_name="lexicon_group_index")
    op.drop_index("ix_lexicon_group_index_user_state_script", table_name="lexicon_group_index")
    op.drop_table("lexicon_group_index")
