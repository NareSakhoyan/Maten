"""Add document workflow state for ingestion-to-lexeme review."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0015_document_workflows"
down_revision = "0014_morphology_analysis"
branch_labels = None
depends_on = None


document_workflow_stage = postgresql.ENUM(
    "uploaded",
    "ingesting",
    "ready_for_review",
    "in_review",
    "curated_partial",
    "curated_complete",
    "failed",
    name="document_workflow_stage",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    document_workflow_stage.create(bind, checkfirst=True)

    op.create_table(
        "document_workflows",
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "stage",
            document_workflow_stage,
            nullable=False,
            server_default="uploaded",
        ),
        sa.Column("candidate_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("linked_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ignored_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("suspicious_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("document_id"),
    )
    op.create_index("ix_document_workflows_user_id", "document_workflows", ["user_id"])
    op.create_index("ix_document_workflows_user_stage", "document_workflows", ["user_id", "stage"])
    op.create_index("ix_document_workflows_last_activity", "document_workflows", ["last_activity_at"])


def downgrade() -> None:
    op.drop_index("ix_document_workflows_last_activity", table_name="document_workflows")
    op.drop_index("ix_document_workflows_user_stage", table_name="document_workflows")
    op.drop_index("ix_document_workflows_user_id", table_name="document_workflows")
    op.drop_table("document_workflows")
    document_workflow_stage.drop(op.get_bind(), checkfirst=True)
