"""Add MVP-3 reference source and matching tables."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0006_mvp3_reference_matching"
down_revision = "0005_job_recovery"
branch_labels = None
depends_on = None


reference_source_type = postgresql.ENUM(
    "imported_dictionary",
    "imported_wordlist",
    "manual",
    name="reference_source_type",
    create_type=False,
)
reference_match_run_scope = postgresql.ENUM(
    "lexicon_groups",
    "lexemes",
    "all",
    name="reference_match_run_scope",
    create_type=False,
)
reference_match_run_status = postgresql.ENUM(
    "queued",
    "running",
    "completed",
    "failed",
    name="reference_match_run_status",
    create_type=False,
)
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


def upgrade() -> None:
    bind = op.get_bind()
    reference_source_type.create(bind, checkfirst=True)
    reference_match_run_scope.create(bind, checkfirst=True)
    reference_match_run_status.create(bind, checkfirst=True)
    reference_match_target_type.create(bind, checkfirst=True)
    reference_match_type.create(bind, checkfirst=True)

    op.create_table(
        "reference_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("source_type", reference_source_type, nullable=False, server_default="imported_wordlist"),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("user_id", "key", name="uq_reference_sources_user_key"),
    )
    op.create_index("ix_reference_sources_user_id", "reference_sources", ["user_id"], unique=False)

    op.create_table(
        "reference_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("surface_form", sa.Text(), nullable=False),
        sa.Column("normalized_form", sa.Text(), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["reference_sources.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "source_id",
            "surface_form",
            "normalized_form",
            name="uq_reference_entries_source_surface_normalized",
        ),
    )
    op.create_index("ix_reference_entries_source_id", "reference_entries", ["source_id"], unique=False)
    op.create_index("ix_reference_entries_normalized_form", "reference_entries", ["normalized_form"], unique=False)
    op.create_index(
        "ix_reference_entries_source_id_normalized_form",
        "reference_entries",
        ["source_id", "normalized_form"],
        unique=False,
    )

    op.create_table(
        "reference_match_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("run_scope", reference_match_run_scope, nullable=False),
        sa.Column("status", reference_match_run_status, nullable=False, server_default="queued"),
        sa.Column("total_items", sa.Integer(), nullable=True),
        sa.Column("matched_items", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_reference_match_runs_user_id", "reference_match_runs", ["user_id"], unique=False)

    op.create_table(
        "reference_matches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("target_type", reference_match_target_type, nullable=False),
        sa.Column("target_key", sa.Text(), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reference_entry_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("match_type", reference_match_type, nullable=False),
        sa.Column("match_score", sa.Numeric(5, 2), nullable=True),
        sa.Column("matched_form", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["reference_sources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reference_entry_id"], ["reference_entries.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "user_id",
            "target_type",
            "target_key",
            "source_id",
            "reference_entry_id",
            "match_type",
            name="uq_reference_matches_target_source_entry_type",
        ),
    )
    op.create_index("ix_reference_matches_user_id", "reference_matches", ["user_id"], unique=False)
    op.create_index(
        "ix_reference_matches_user_target",
        "reference_matches",
        ["user_id", "target_type", "target_key"],
        unique=False,
    )
    op.create_index("ix_reference_matches_source_id", "reference_matches", ["source_id"], unique=False)
    op.create_index(
        "ix_reference_matches_reference_entry_id",
        "reference_matches",
        ["reference_entry_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_reference_matches_reference_entry_id", table_name="reference_matches")
    op.drop_index("ix_reference_matches_source_id", table_name="reference_matches")
    op.drop_index("ix_reference_matches_user_target", table_name="reference_matches")
    op.drop_index("ix_reference_matches_user_id", table_name="reference_matches")
    op.drop_table("reference_matches")

    op.drop_index("ix_reference_match_runs_user_id", table_name="reference_match_runs")
    op.drop_table("reference_match_runs")

    op.drop_index("ix_reference_entries_source_id_normalized_form", table_name="reference_entries")
    op.drop_index("ix_reference_entries_normalized_form", table_name="reference_entries")
    op.drop_index("ix_reference_entries_source_id", table_name="reference_entries")
    op.drop_table("reference_entries")

    op.drop_index("ix_reference_sources_user_id", table_name="reference_sources")
    op.drop_table("reference_sources")

    bind = op.get_bind()
    reference_match_type.drop(bind, checkfirst=True)
    reference_match_target_type.drop(bind, checkfirst=True)
    reference_match_run_status.drop(bind, checkfirst=True)
    reference_match_run_scope.drop(bind, checkfirst=True)
    reference_source_type.drop(bind, checkfirst=True)
