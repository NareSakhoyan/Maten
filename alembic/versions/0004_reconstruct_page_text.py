"""Preserve raw page text and reconstructed page text."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0004_reconstruct_page_text"
down_revision = "0003_review_lexicon"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("document_pages", sa.Column("raw_extracted_text", sa.Text(), nullable=True))
    op.add_column("document_pages", sa.Column("reconstructed_text", sa.Text(), nullable=True))
    op.execute(
        sa.text(
            """
            UPDATE document_pages
            SET raw_extracted_text = extracted_text,
                reconstructed_text = extracted_text
            WHERE raw_extracted_text IS NULL
               OR reconstructed_text IS NULL
            """
        )
    )


def downgrade() -> None:
    op.drop_column("document_pages", "reconstructed_text")
    op.drop_column("document_pages", "raw_extracted_text")
