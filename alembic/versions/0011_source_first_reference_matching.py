"""Refactor reference matching runs to support source-first mode."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0011_source_first_match"
down_revision = "0010_reference_match_run_results"
branch_labels = None
depends_on = None


reference_matching_direction = postgresql.ENUM(
    "source_to_internal",
    "internal_to_reference",
    name="reference_matching_direction",
    create_type=False,
)
reference_match_target_scope = postgresql.ENUM(
    "lexicon",
    "imported_books",
    "all_internal",
    name="reference_match_target_scope",
    create_type=False,
)
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
    reference_matching_direction.create(bind, checkfirst=True)
    reference_match_target_scope.create(bind, checkfirst=True)

    op.execute("ALTER TYPE reference_match_target_type ADD VALUE IF NOT EXISTS 'reference_entry'")

    op.add_column(
        "reference_match_runs",
        sa.Column(
            "matching_direction",
            reference_matching_direction,
            nullable=False,
            server_default="internal_to_reference",
        ),
    )
    op.add_column("reference_match_runs", sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("reference_match_runs", sa.Column("target_scope", reference_match_target_scope, nullable=True))
    op.create_foreign_key(
        "fk_reference_match_runs_source_id",
        "reference_match_runs",
        "reference_sources",
        ["source_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.add_column(
        "reference_match_run_results",
        sa.Column(
            "matching_direction",
            reference_matching_direction,
            nullable=False,
            server_default="internal_to_reference",
        ),
    )
    op.add_column("reference_match_run_results", sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column(
        "reference_match_run_results",
        sa.Column("reference_entry_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "reference_match_run_results",
        sa.Column("normalized_form", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "reference_match_run_results",
        sa.Column("exists_in_lexicon", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "reference_match_run_results",
        sa.Column("matching_lexeme_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("reference_match_run_results", sa.Column("best_lexeme_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("reference_match_run_results", sa.Column("best_lexeme_canonical_form", sa.Text(), nullable=True))
    op.add_column(
        "reference_match_run_results",
        sa.Column("found_in_books", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "reference_match_run_results",
        sa.Column("matching_book_occurrence_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("reference_match_run_results", sa.Column("best_document_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("reference_match_run_results", sa.Column("best_document_title", sa.Text(), nullable=True))
    op.add_column("reference_match_run_results", sa.Column("best_page_number", sa.Integer(), nullable=True))
    op.add_column("reference_match_run_results", sa.Column("best_context_snippet", sa.Text(), nullable=True))
    op.add_column("reference_match_run_results", sa.Column("source_import_method", reference_import_method, nullable=True))
    op.add_column("reference_match_run_results", sa.Column("source_warning", sa.Text(), nullable=True))

    op.create_foreign_key(
        "fk_reference_match_run_results_source_id",
        "reference_match_run_results",
        "reference_sources",
        ["source_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_reference_match_run_results_reference_entry_id",
        "reference_match_run_results",
        "reference_entries",
        ["reference_entry_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_reference_match_run_results_best_lexeme_id",
        "reference_match_run_results",
        "lexemes",
        ["best_lexeme_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_reference_match_run_results_best_document_id",
        "reference_match_run_results",
        "documents",
        ["best_document_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_reference_match_run_results_run_id_normalized_form",
        "reference_match_run_results",
        ["run_id", "normalized_form"],
        unique=False,
    )
    op.create_unique_constraint(
        "uq_reference_match_run_results_reference_entry",
        "reference_match_run_results",
        ["run_id", "reference_entry_id"],
    )

    op.execute(
        sa.text(
            """
            UPDATE reference_match_run_results AS results
            SET matching_direction = runs.matching_direction,
                normalized_form = COALESCE(NULLIF(results.target_label, ''), results.target_key)
            FROM reference_match_runs AS runs
            WHERE runs.id = results.run_id
            """
        )
    )

    op.alter_column("reference_match_runs", "matching_direction", server_default=None)
    op.alter_column("reference_match_run_results", "matching_direction", server_default=None)
    op.alter_column("reference_match_run_results", "normalized_form", server_default=None)
    op.alter_column("reference_match_run_results", "exists_in_lexicon", server_default=None)
    op.alter_column("reference_match_run_results", "matching_lexeme_count", server_default=None)
    op.alter_column("reference_match_run_results", "found_in_books", server_default=None)
    op.alter_column("reference_match_run_results", "matching_book_occurrence_count", server_default=None)


def downgrade() -> None:
    op.drop_constraint(
        "uq_reference_match_run_results_reference_entry",
        "reference_match_run_results",
        type_="unique",
    )
    op.drop_index("ix_reference_match_run_results_run_id_normalized_form", table_name="reference_match_run_results")
    op.drop_constraint(
        "fk_reference_match_run_results_best_document_id",
        "reference_match_run_results",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_reference_match_run_results_best_lexeme_id",
        "reference_match_run_results",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_reference_match_run_results_reference_entry_id",
        "reference_match_run_results",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_reference_match_run_results_source_id",
        "reference_match_run_results",
        type_="foreignkey",
    )
    op.drop_column("reference_match_run_results", "source_warning")
    op.drop_column("reference_match_run_results", "source_import_method")
    op.drop_column("reference_match_run_results", "best_context_snippet")
    op.drop_column("reference_match_run_results", "best_page_number")
    op.drop_column("reference_match_run_results", "best_document_title")
    op.drop_column("reference_match_run_results", "best_document_id")
    op.drop_column("reference_match_run_results", "matching_book_occurrence_count")
    op.drop_column("reference_match_run_results", "found_in_books")
    op.drop_column("reference_match_run_results", "best_lexeme_canonical_form")
    op.drop_column("reference_match_run_results", "best_lexeme_id")
    op.drop_column("reference_match_run_results", "matching_lexeme_count")
    op.drop_column("reference_match_run_results", "exists_in_lexicon")
    op.drop_column("reference_match_run_results", "normalized_form")
    op.drop_column("reference_match_run_results", "reference_entry_id")
    op.drop_column("reference_match_run_results", "source_id")
    op.drop_column("reference_match_run_results", "matching_direction")

    op.drop_constraint("fk_reference_match_runs_source_id", "reference_match_runs", type_="foreignkey")
    op.drop_column("reference_match_runs", "target_scope")
    op.drop_column("reference_match_runs", "source_id")
    op.drop_column("reference_match_runs", "matching_direction")

    bind = op.get_bind()
    reference_match_target_scope.drop(bind, checkfirst=True)
    reference_matching_direction.drop(bind, checkfirst=True)
