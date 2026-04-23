"""Add reference source import metadata."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0007_reference_import_metadata"
down_revision = "0006_mvp3_reference_matching"
branch_labels = None
depends_on = None


reference_import_method = postgresql.ENUM(
    "txt",
    "csv",
    "docx",
    "pdf_text",
    "pdf_ocr",
    "xlsx",
    name="reference_import_method",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    reference_import_method.create(bind, checkfirst=True)

    op.add_column(
        "reference_sources",
        sa.Column("last_import_method", reference_import_method, nullable=True),
    )
    op.add_column("reference_sources", sa.Column("last_import_warning", sa.Text(), nullable=True))
    op.add_column("reference_sources", sa.Column("last_imported_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("reference_sources", sa.Column("entry_count", sa.Integer(), nullable=False, server_default="0"))

    op.execute(
        sa.text(
            """
            UPDATE reference_sources
            SET entry_count = entry_counts.entry_count
            FROM (
                SELECT source_id, COUNT(*) AS entry_count
                FROM reference_entries
                GROUP BY source_id
            ) AS entry_counts
            WHERE reference_sources.id = entry_counts.source_id
            """
        )
    )


def downgrade() -> None:
    op.drop_column("reference_sources", "entry_count")
    op.drop_column("reference_sources", "last_imported_at")
    op.drop_column("reference_sources", "last_import_warning")
    op.drop_column("reference_sources", "last_import_method")

    bind = op.get_bind()
    reference_import_method.drop(bind, checkfirst=True)
