"""Add script_counts to lexicon_group_index_documents for slice-local aggregation."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0017_index_doc_script_counts"
down_revision = "0016_lexicon_group_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL statement_timeout = '0'")
    # Alembic defaults to varchar(32); some revision ids are longer.
    op.execute("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(128)")

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    column_names = {column["name"] for column in inspector.get_columns("lexicon_group_index_documents")}
    if "script_counts" not in column_names:
        op.add_column(
            "lexicon_group_index_documents",
            sa.Column("script_counts", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        )

    op.execute(
        """
        UPDATE lexicon_group_index_documents
        SET script_counts = '{}'::jsonb
        WHERE script_counts IS NULL
        """
    )
    op.alter_column(
        "lexicon_group_index_documents",
        "script_counts",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
        server_default="{}",
    )


def downgrade() -> None:
    op.drop_column("lexicon_group_index_documents", "script_counts")
