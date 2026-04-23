"""Add trusted external lookup providers and cache tables."""

from __future__ import annotations

import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0013_external_lookup_cache"
down_revision = "0012_reference_retry_metadata"
branch_labels = None
depends_on = None


external_lookup_search_mode = postgresql.ENUM(
    "exact",
    "normalized",
    "fuzzy",
    name="external_lookup_search_mode",
    create_type=False,
)
external_lookup_status = postgresql.ENUM(
    "completed",
    "failed",
    name="external_lookup_status",
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
    external_lookup_search_mode.create(bind, checkfirst=True)
    external_lookup_status.create(bind, checkfirst=True)
    reference_match_type.create(bind, checkfirst=True)

    op.create_table(
        "external_providers",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key", name="uq_external_providers_key"),
    )
    op.bulk_insert(
        sa.table(
            "external_providers",
            sa.column("id", postgresql.UUID(as_uuid=True)),
            sa.column("key", sa.Text()),
            sa.column("display_name", sa.Text()),
            sa.column("is_active", sa.Boolean()),
        ),
        [
            {
                "id": uuid.uuid4(),
                "key": "nayiri_web",
                "display_name": "Nayiri",
                "is_active": True,
            }
        ],
    )

    op.create_table(
        "external_lookup_cache",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=True),
        sa.Column("provider_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("query_text", sa.Text(), nullable=False),
        sa.Column("normalized_query", sa.Text(), nullable=False),
        sa.Column("search_mode", external_lookup_search_mode, nullable=False),
        sa.Column("status", external_lookup_status, nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["provider_id"], ["external_providers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_external_lookup_cache_provider_id", "external_lookup_cache", ["provider_id"], unique=False)
    op.create_index(
        "ix_external_lookup_cache_normalized_query",
        "external_lookup_cache",
        ["normalized_query"],
        unique=False,
    )
    op.create_index(
        "ix_external_lookup_cache_provider_query_mode",
        "external_lookup_cache",
        ["provider_id", "normalized_query", "search_mode"],
        unique=False,
    )

    op.create_table(
        "external_lookup_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("cache_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("matched_form", sa.Text(), nullable=False),
        sa.Column("normalized_form", sa.Text(), nullable=True),
        sa.Column("source_title", sa.Text(), nullable=True),
        sa.Column("source_subtitle", sa.Text(), nullable=True),
        sa.Column("snippet", sa.Text(), nullable=True),
        sa.Column("reference_link", sa.Text(), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("match_type", reference_match_type, nullable=False),
        sa.Column("match_score", sa.Numeric(5, 2), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["cache_id"], ["external_lookup_cache.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["provider_id"], ["external_providers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_external_lookup_results_cache_id", "external_lookup_results", ["cache_id"], unique=False)
    op.create_index("ix_external_lookup_results_provider_id", "external_lookup_results", ["provider_id"], unique=False)
    op.create_index(
        "ix_external_lookup_results_normalized_form",
        "external_lookup_results",
        ["normalized_form"],
        unique=False,
    )

    op.alter_column("external_providers", "is_active", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_external_lookup_results_normalized_form", table_name="external_lookup_results")
    op.drop_index("ix_external_lookup_results_provider_id", table_name="external_lookup_results")
    op.drop_index("ix_external_lookup_results_cache_id", table_name="external_lookup_results")
    op.drop_table("external_lookup_results")

    op.drop_index("ix_external_lookup_cache_provider_query_mode", table_name="external_lookup_cache")
    op.drop_index("ix_external_lookup_cache_normalized_query", table_name="external_lookup_cache")
    op.drop_index("ix_external_lookup_cache_provider_id", table_name="external_lookup_cache")
    op.drop_table("external_lookup_cache")

    op.drop_table("external_providers")

    bind = op.get_bind()
    external_lookup_status.drop(bind, checkfirst=True)
    external_lookup_search_mode.drop(bind, checkfirst=True)
