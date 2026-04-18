"""Initial schema for OCR document ingestion."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


document_status = postgresql.ENUM(
    "uploaded",
    "queued",
    "processing",
    "completed",
    "failed",
    name="document_status",
    create_type=False,
)
extraction_method = postgresql.ENUM(
    "pdf_text",
    "ocr",
    name="extraction_method",
    create_type=False,
)
ingestion_job_status = postgresql.ENUM(
    "queued",
    "running",
    "completed",
    "failed",
    name="ingestion_job_status",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    document_status.create(bind, checkfirst=True)
    extraction_method.create(bind, checkfirst=True)
    ingestion_job_status.create(bind, checkfirst=True)

    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("original_filename", sa.String(length=512), nullable=False),
        sa.Column("mime_type", sa.String(length=255), nullable=False),
        sa.Column("file_size_bytes", sa.Integer(), nullable=False),
        sa.Column("storage_bucket", sa.String(length=255), nullable=False),
        sa.Column("storage_path", sa.String(length=1024), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("status", document_status, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_documents_user_id", "documents", ["user_id"], unique=False)

    op.create_table(
        "document_pages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("extraction_method", extraction_method, nullable=False),
        sa.Column("page_image_bucket", sa.String(length=255), nullable=True),
        sa.Column("page_image_path", sa.String(length=1024), nullable=True),
        sa.Column("extracted_text", sa.Text(), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("document_id", "page_number", name="uq_document_pages_document_page"),
    )
    op.create_index("ix_document_pages_document_id", "document_pages", ["document_id"], unique=False)

    op.create_table(
        "ingestion_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", ingestion_job_status, nullable=False),
        sa.Column("step", sa.Text(), nullable=True),
        sa.Column("progress_percent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_ingestion_jobs_document_id", "ingestion_jobs", ["document_id"], unique=False)
    op.create_index("ix_ingestion_jobs_user_id", "ingestion_jobs", ["user_id"], unique=False)

    op.create_table(
        "occurrences",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("page_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("token", sa.Text(), nullable=False),
        sa.Column("normalized_token", sa.Text(), nullable=False),
        sa.Column("context_snippet", sa.Text(), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=True),
        sa.Column("char_end", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["page_id"], ["document_pages.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_occurrences_document_id", "occurrences", ["document_id"], unique=False)
    op.create_index("ix_occurrences_normalized_token", "occurrences", ["normalized_token"], unique=False)
    op.create_index(
        "ix_occurrences_document_page_number",
        "occurrences",
        ["document_id", "page_number"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_occurrences_document_page_number", table_name="occurrences")
    op.drop_index("ix_occurrences_normalized_token", table_name="occurrences")
    op.drop_index("ix_occurrences_document_id", table_name="occurrences")
    op.drop_table("occurrences")

    op.drop_index("ix_ingestion_jobs_user_id", table_name="ingestion_jobs")
    op.drop_index("ix_ingestion_jobs_document_id", table_name="ingestion_jobs")
    op.drop_table("ingestion_jobs")

    op.drop_index("ix_document_pages_document_id", table_name="document_pages")
    op.drop_table("document_pages")

    op.drop_index("ix_documents_user_id", table_name="documents")
    op.drop_table("documents")

    bind = op.get_bind()
    ingestion_job_status.drop(bind, checkfirst=True)
    extraction_method.drop(bind, checkfirst=True)
    document_status.drop(bind, checkfirst=True)
