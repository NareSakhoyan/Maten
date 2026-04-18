"""Add MVP-2 lexicon and lexeme tables."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0002_mvp2_lexicon_lexemes"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


lexeme_status = postgresql.ENUM(
    "draft",
    "curated",
    name="lexeme_status",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    lexeme_status.create(bind, checkfirst=True)

    op.create_table(
        "lexemes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("canonical_form", sa.Text(), nullable=False),
        sa.Column("canonical_normalized_form", sa.Text(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("status", lexeme_status, nullable=False, server_default="draft"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_lexemes_user_id", "lexemes", ["user_id"], unique=False)
    op.create_index(
        "ix_lexemes_canonical_normalized_form",
        "lexemes",
        ["canonical_normalized_form"],
        unique=False,
    )

    op.create_table(
        "lexeme_forms",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("lexeme_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("normalized_form", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["lexeme_id"], ["lexemes.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_lexeme_forms_lexeme_id", "lexeme_forms", ["lexeme_id"], unique=False)
    op.create_index(
        "ix_lexeme_forms_user_normalized_form",
        "lexeme_forms",
        ["user_id", "normalized_form"],
        unique=True,
    )

    op.add_column("occurrences", sa.Column("lexeme_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_index("ix_occurrences_lexeme_id", "occurrences", ["lexeme_id"], unique=False)
    op.create_foreign_key(
        "fk_occurrences_lexeme_id_lexemes",
        "occurrences",
        "lexemes",
        ["lexeme_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_occurrences_lexeme_id_lexemes", "occurrences", type_="foreignkey")
    op.drop_index("ix_occurrences_lexeme_id", table_name="occurrences")
    op.drop_column("occurrences", "lexeme_id")

    op.drop_index("ix_lexeme_forms_user_normalized_form", table_name="lexeme_forms")
    op.drop_index("ix_lexeme_forms_lexeme_id", table_name="lexeme_forms")
    op.drop_table("lexeme_forms")

    op.drop_index("ix_lexemes_canonical_normalized_form", table_name="lexemes")
    op.drop_index("ix_lexemes_user_id", table_name="lexemes")
    op.drop_table("lexemes")

    bind = op.get_bind()
    lexeme_status.drop(bind, checkfirst=True)
