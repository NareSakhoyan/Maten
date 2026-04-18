from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Enum as SqlEnum, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def enum_values(enum_class: type[enum.Enum]) -> list[str]:
    return [member.value for member in enum_class]


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class UpdatedTimestampMixin(TimestampMixin):
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class DocumentStatus(str, enum.Enum):
    UPLOADED = "uploaded"
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class ExtractionMethod(str, enum.Enum):
    PDF_TEXT = "pdf_text"
    OCR = "ocr"


class IngestionJobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class LexemeStatus(str, enum.Enum):
    DRAFT = "draft"
    CURATED = "curated"


class OccurrenceScriptType(str, enum.Enum):
    ARMENIAN = "armenian"
    LATIN = "latin"
    MIXED = "mixed"
    DIGIT_MIXED = "digit_mixed"
    OTHER = "other"


class LexiconGroupReviewStatus(str, enum.Enum):
    UNREVIEWED = "unreviewed"
    IGNORED_NOISE = "ignored_noise"


class Document(UpdatedTimestampMixin, Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[DocumentStatus] = mapped_column(
        SqlEnum(
            DocumentStatus,
            name="document_status",
            values_callable=enum_values,
            validate_strings=True,
        ),
        nullable=False,
        default=DocumentStatus.UPLOADED,
    )

    pages: Mapped[list["DocumentPage"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="DocumentPage.page_number",
    )
    occurrences: Mapped[list["Occurrence"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
    )
    jobs: Mapped[list["IngestionJob"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
    )


class DocumentPage(TimestampMixin, Base):
    __tablename__ = "document_pages"
    __table_args__ = (
        UniqueConstraint("document_id", "page_number", name="uq_document_pages_document_page"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    extraction_method: Mapped[ExtractionMethod] = mapped_column(
        SqlEnum(
            ExtractionMethod,
            name="extraction_method",
            values_callable=enum_values,
            validate_strings=True,
        ),
        nullable=False,
    )
    page_image_bucket: Mapped[str | None] = mapped_column(String(255), nullable=True)
    page_image_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    raw_extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    reconstructed_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    extracted_text: Mapped[str] = mapped_column(Text, nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    document: Mapped["Document"] = relationship(back_populates="pages")
    occurrences: Mapped[list["Occurrence"]] = relationship(
        back_populates="page",
        cascade="all, delete-orphan",
    )


class Occurrence(TimestampMixin, Base):
    __tablename__ = "occurrences"
    __table_args__ = (
        Index("ix_occurrences_document_id", "document_id"),
        Index("ix_occurrences_normalized_token", "normalized_token"),
        Index("ix_occurrences_document_page_number", "document_id", "page_number"),
        Index("ix_occurrences_lexeme_id", "lexeme_id"),
        Index("ix_occurrences_script_type", "script_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    page_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("document_pages.id", ondelete="CASCADE"),
        nullable=False,
    )
    lexeme_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("lexemes.id", ondelete="SET NULL"),
        nullable=True,
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    token: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_token: Mapped[str] = mapped_column(Text, nullable=False)
    script_type: Mapped[OccurrenceScriptType] = mapped_column(
        SqlEnum(
            OccurrenceScriptType,
            name="occurrence_script_type",
            values_callable=enum_values,
            validate_strings=True,
        ),
        nullable=False,
    )
    has_digits: Mapped[bool] = mapped_column(default=False, nullable=False)
    has_latin: Mapped[bool] = mapped_column(default=False, nullable=False)
    has_armenian: Mapped[bool] = mapped_column(default=False, nullable=False)
    token_length: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_snippet: Mapped[str] = mapped_column(Text, nullable=False)
    char_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    char_end: Mapped[int | None] = mapped_column(Integer, nullable=True)

    document: Mapped["Document"] = relationship(back_populates="occurrences")
    page: Mapped["DocumentPage"] = relationship(back_populates="occurrences")
    lexeme: Mapped["Lexeme | None"] = relationship(back_populates="occurrences")


class Lexeme(UpdatedTimestampMixin, Base):
    __tablename__ = "lexemes"
    __table_args__ = (
        Index("ix_lexemes_user_id", "user_id"),
        Index("ix_lexemes_canonical_normalized_form", "canonical_normalized_form"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_form: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_normalized_form: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[LexemeStatus] = mapped_column(
        SqlEnum(
            LexemeStatus,
            name="lexeme_status",
            values_callable=enum_values,
            validate_strings=True,
        ),
        nullable=False,
        default=LexemeStatus.DRAFT,
    )

    forms: Mapped[list["LexemeForm"]] = relationship(
        back_populates="lexeme",
        cascade="all, delete-orphan",
        order_by="LexemeForm.normalized_form",
    )
    occurrences: Mapped[list["Occurrence"]] = relationship(back_populates="lexeme")


class LexemeForm(TimestampMixin, Base):
    __tablename__ = "lexeme_forms"
    __table_args__ = (
        Index("ix_lexeme_forms_lexeme_id", "lexeme_id"),
        Index("ix_lexeme_forms_user_normalized_form", "user_id", "normalized_form", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lexeme_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("lexemes.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_form: Mapped[str] = mapped_column(Text, nullable=False)

    lexeme: Mapped["Lexeme"] = relationship(back_populates="forms")


class LexiconGroupReview(UpdatedTimestampMixin, Base):
    __tablename__ = "lexicon_group_reviews"
    __table_args__ = (
        Index("ix_lexicon_group_reviews_user_id", "user_id"),
        Index(
            "ix_lexicon_group_reviews_user_normalized_form",
            "user_id",
            "normalized_form",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_form: Mapped[str] = mapped_column(Text, nullable=False)
    review_status: Mapped[LexiconGroupReviewStatus] = mapped_column(
        SqlEnum(
            LexiconGroupReviewStatus,
            name="lexicon_group_review_status",
            values_callable=enum_values,
            validate_strings=True,
        ),
        nullable=False,
        default=LexiconGroupReviewStatus.UNREVIEWED,
    )
    reviewer_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class IngestionJob(TimestampMixin, Base):
    __tablename__ = "ingestion_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    retry_of_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ingestion_jobs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    status: Mapped[IngestionJobStatus] = mapped_column(
        SqlEnum(
            IngestionJobStatus,
            name="ingestion_job_status",
            values_callable=enum_values,
            validate_strings=True,
        ),
        nullable=False,
        default=IngestionJobStatus.QUEUED,
    )
    step: Mapped[str | None] = mapped_column(Text, nullable=True)
    progress_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message_user: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message_technical: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_steps: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    can_retry: Mapped[bool] = mapped_column(nullable=False, default=True)
    last_retried_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    document: Mapped["Document"] = relationship(back_populates="jobs")
