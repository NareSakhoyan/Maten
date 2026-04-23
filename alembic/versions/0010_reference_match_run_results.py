"""Add reference matching run result persistence."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0010_reference_match_run_results"
down_revision = "0009_async_job_envelope_refactor"
branch_labels = None
depends_on = None


reference_match_target_type = postgresql.ENUM(
    "lexicon_group",
    "lexeme",
    name="reference_match_target_type",
    create_type=False,
)
reference_match_type = postgresql.ENUM(
    "exact",
    "normalized",
    "fuzzy",
    name="reference_match_type",
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
reference_match_status = postgresql.ENUM(
    "matched",
    "unmatched",
    name="reference_match_status",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    reference_match_target_type.create(bind, checkfirst=True)
    reference_match_type.create(bind, checkfirst=True)
    reference_import_method.create(bind, checkfirst=True)
    reference_match_status.create(bind, checkfirst=True)

    op.add_column("reference_match_runs", sa.Column("unmatched_items", sa.Integer(), nullable=True))
    op.add_column("reference_match_runs", sa.Column("exact_match_count", sa.Integer(), nullable=True))
    op.add_column("reference_match_runs", sa.Column("normalized_match_count", sa.Integer(), nullable=True))
    op.add_column("reference_match_runs", sa.Column("fuzzy_match_count", sa.Integer(), nullable=True))

    op.execute(
        sa.text(
            """
            UPDATE reference_match_runs
            SET unmatched_items = CASE
                    WHEN total_items IS NULL OR matched_items IS NULL THEN NULL
                    ELSE GREATEST(total_items - matched_items, 0)
                END
            """
        )
    )

    op.create_table(
        "reference_match_run_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("target_type", reference_match_target_type, nullable=False),
        sa.Column("target_key", sa.Text(), nullable=False),
        sa.Column("target_label", sa.Text(), nullable=False),
        sa.Column("match_status", reference_match_status, nullable=False),
        sa.Column("best_match_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("best_match_type", reference_match_type, nullable=True),
        sa.Column("best_match_score", sa.Numeric(5, 2), nullable=True),
        sa.Column("best_source_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("best_source_display_name", sa.Text(), nullable=True),
        sa.Column("best_matched_form", sa.Text(), nullable=True),
        sa.Column("match_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("related_resource_type", reference_match_target_type, nullable=True),
        sa.Column("related_resource_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["reference_match_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["best_source_id"], ["reference_sources.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("run_id", "target_type", "target_key", name="uq_reference_match_run_results_target"),
    )
    op.create_index("ix_reference_match_run_results_run_id", "reference_match_run_results", ["run_id"], unique=False)
    op.create_index(
        "ix_reference_match_run_results_user_id_run_id",
        "reference_match_run_results",
        ["user_id", "run_id"],
        unique=False,
    )
    op.create_index(
        "ix_reference_match_run_results_run_id_match_status",
        "reference_match_run_results",
        ["run_id", "match_status"],
        unique=False,
    )
    op.create_index(
        "ix_reference_match_run_results_run_id_target_type",
        "reference_match_run_results",
        ["run_id", "target_type"],
        unique=False,
    )

    op.create_table(
        "reference_match_run_result_matches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("result_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_display_name", sa.Text(), nullable=False),
        sa.Column("reference_entry_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("surface_form", sa.Text(), nullable=False),
        sa.Column("normalized_form", sa.Text(), nullable=False),
        sa.Column("match_type", reference_match_type, nullable=False),
        sa.Column("match_score", sa.Numeric(5, 2), nullable=True),
        sa.Column("source_import_method", reference_import_method, nullable=True),
        sa.Column("source_warning", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["result_id"], ["reference_match_run_results.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["reference_match_runs.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_reference_match_run_result_matches_result_id",
        "reference_match_run_result_matches",
        ["result_id"],
        unique=False,
    )
    op.create_index(
        "ix_reference_match_run_result_matches_run_id",
        "reference_match_run_result_matches",
        ["run_id"],
        unique=False,
    )
    op.create_index(
        "ix_reference_match_run_result_matches_user_id",
        "reference_match_run_result_matches",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_reference_match_run_result_matches_user_id", table_name="reference_match_run_result_matches")
    op.drop_index("ix_reference_match_run_result_matches_run_id", table_name="reference_match_run_result_matches")
    op.drop_index("ix_reference_match_run_result_matches_result_id", table_name="reference_match_run_result_matches")
    op.drop_table("reference_match_run_result_matches")

    op.drop_index("ix_reference_match_run_results_run_id_target_type", table_name="reference_match_run_results")
    op.drop_index("ix_reference_match_run_results_run_id_match_status", table_name="reference_match_run_results")
    op.drop_index("ix_reference_match_run_results_user_id_run_id", table_name="reference_match_run_results")
    op.drop_index("ix_reference_match_run_results_run_id", table_name="reference_match_run_results")
    op.drop_table("reference_match_run_results")

    op.drop_column("reference_match_runs", "fuzzy_match_count")
    op.drop_column("reference_match_runs", "normalized_match_count")
    op.drop_column("reference_match_runs", "exact_match_count")
    op.drop_column("reference_match_runs", "unmatched_items")

    bind = op.get_bind()
    reference_match_status.drop(bind, checkfirst=True)
