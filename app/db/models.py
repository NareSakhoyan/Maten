from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Enum as SqlEnum, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, func
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


class ReferenceSourceType(str, enum.Enum):
    IMPORTED_DICTIONARY = "imported_dictionary"
    IMPORTED_WORDLIST = "imported_wordlist"
    MANUAL = "manual"


class ReferenceMatchRunScope(str, enum.Enum):
    LEXICON_GROUPS = "lexicon_groups"
    LEXEMES = "lexemes"
    ALL = "all"


class ReferenceMatchingDirection(str, enum.Enum):
    SOURCE_TO_INTERNAL = "source_to_internal"
    INTERNAL_TO_REFERENCE = "internal_to_reference"


class ReferenceMatchTargetScope(str, enum.Enum):
    LEXICON = "lexicon"
    IMPORTED_BOOKS = "imported_books"
    ALL_INTERNAL = "all_internal"


class ReferenceMatchRunStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ReferenceMatchTargetType(str, enum.Enum):
    REFERENCE_ENTRY = "reference_entry"
    LEXICON_GROUP = "lexicon_group"
    LEXEME = "lexeme"


class ReferenceMatchType(str, enum.Enum):
    EXACT = "exact"
    NORMALIZED = "normalized"
    FUZZY = "fuzzy"


class ReferenceMatchStatus(str, enum.Enum):
    MATCHED = "matched"
    UNMATCHED = "unmatched"


class ReferenceImportMethod(str, enum.Enum):
    TXT = "txt"
    CSV = "csv"
    DOCX = "docx"
    PDF_TEXT = "pdf_text"
    PDF_OCR = "pdf_ocr"
    XLSX = "xlsx"


class ReferenceImportStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class JobKind(str, enum.Enum):
    INGESTION = "ingestion"
    REFERENCE_IMPORT = "reference_import"
    REFERENCE_MATCHING = "reference_matching"


class JobResultResourceType(str, enum.Enum):
    DOCUMENT = "document"
    REFERENCE_SOURCE = "reference_source"
    REFERENCE_MATCH_RUN = "reference_match_run"


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


class ReferenceSource(UpdatedTimestampMixin, Base):
    __tablename__ = "reference_sources"
    __table_args__ = (
        Index("ix_reference_sources_user_id", "user_id"),
        UniqueConstraint("user_id", "key", name="uq_reference_sources_user_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_type: Mapped[ReferenceSourceType] = mapped_column(
        SqlEnum(
            ReferenceSourceType,
            name="reference_source_type",
            values_callable=enum_values,
            validate_strings=True,
        ),
        nullable=False,
        default=ReferenceSourceType.IMPORTED_WORDLIST,
    )
    language: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    last_import_method: Mapped[ReferenceImportMethod | None] = mapped_column(
        SqlEnum(
            ReferenceImportMethod,
            name="reference_import_method",
            values_callable=enum_values,
            validate_strings=True,
        ),
        nullable=True,
    )
    last_import_warning: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    entry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    entries: Mapped[list["ReferenceEntry"]] = relationship(
        back_populates="source",
        cascade="all, delete-orphan",
    )
    imports: Mapped[list["ReferenceSourceImport"]] = relationship(
        back_populates="source",
        cascade="all, delete-orphan",
        order_by="ReferenceSourceImport.created_at.desc()",
    )
    matches: Mapped[list["ReferenceMatch"]] = relationship(
        back_populates="source",
        cascade="all, delete-orphan",
    )
    run_results: Mapped[list["ReferenceMatchRunResult"]] = relationship(
        back_populates="best_source",
        foreign_keys="ReferenceMatchRunResult.best_source_id",
    )


class ReferenceEntry(UpdatedTimestampMixin, Base):
    __tablename__ = "reference_entries"
    __table_args__ = (
        Index("ix_reference_entries_source_id", "source_id"),
        Index("ix_reference_entries_normalized_form", "normalized_form"),
        Index("ix_reference_entries_source_id_normalized_form", "source_id", "normalized_form"),
        UniqueConstraint(
            "source_id",
            "surface_form",
            "normalized_form",
            name="uq_reference_entries_source_surface_normalized",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("reference_sources.id", ondelete="CASCADE"),
        nullable=False,
    )
    surface_form: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_form: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)

    source: Mapped["ReferenceSource"] = relationship(back_populates="entries")
    matches: Mapped[list["ReferenceMatch"]] = relationship(
        back_populates="reference_entry",
        cascade="all, delete-orphan",
    )


class ReferenceMatchRun(UpdatedTimestampMixin, Base):
    __tablename__ = "reference_match_runs"
    __table_args__ = (
        Index("ix_reference_match_runs_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    matching_direction: Mapped[ReferenceMatchingDirection] = mapped_column(
        SqlEnum(
            ReferenceMatchingDirection,
            name="reference_matching_direction",
            values_callable=enum_values,
            validate_strings=True,
        ),
        nullable=False,
        default=ReferenceMatchingDirection.SOURCE_TO_INTERNAL,
    )
    run_scope: Mapped[ReferenceMatchRunScope] = mapped_column(
        SqlEnum(
            ReferenceMatchRunScope,
            name="reference_match_run_scope",
            values_callable=enum_values,
            validate_strings=True,
        ),
        nullable=False,
        default=ReferenceMatchRunScope.ALL,
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("reference_sources.id", ondelete="SET NULL"),
        nullable=True,
    )
    retry_of_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("reference_match_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    target_scope: Mapped[ReferenceMatchTargetScope | None] = mapped_column(
        SqlEnum(
            ReferenceMatchTargetScope,
            name="reference_match_target_scope",
            values_callable=enum_values,
            validate_strings=True,
        ),
        nullable=True,
    )
    requested_view: Mapped[str] = mapped_column(Text, nullable=False, default="candidates")
    include_fuzzy: Mapped[bool] = mapped_column(nullable=False, default=False)
    status: Mapped[ReferenceMatchRunStatus] = mapped_column(
        SqlEnum(
            ReferenceMatchRunStatus,
            name="reference_match_run_status",
            values_callable=enum_values,
            validate_strings=True,
        ),
        nullable=False,
        default=ReferenceMatchRunStatus.QUEUED,
    )
    total_items: Mapped[int | None] = mapped_column(Integer, nullable=True)
    matched_items: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unmatched_items: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exact_match_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    normalized_match_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fuzzy_match_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    current_stage_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_stage_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    stage_message_user: Mapped[str | None] = mapped_column(Text, nullable=True)
    progress_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_processed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    items_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message_user: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_steps: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    result_resource_type: Mapped[JobResultResourceType | None] = mapped_column(
        SqlEnum(
            JobResultResourceType,
            name="job_result_resource_type",
            values_callable=enum_values,
            validate_strings=True,
        ),
        nullable=True,
    )
    result_resource_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    can_retry: Mapped[bool] = mapped_column(nullable=False, default=True)
    last_retried_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source: Mapped["ReferenceSource | None"] = relationship(foreign_keys=[source_id])
    results: Mapped[list["ReferenceMatchRunResult"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="ReferenceMatchRunResult.target_label",
    )


class ReferenceMatch(TimestampMixin, Base):
    __tablename__ = "reference_matches"
    __table_args__ = (
        Index("ix_reference_matches_user_id", "user_id"),
        Index("ix_reference_matches_user_target", "user_id", "target_type", "target_key"),
        Index("ix_reference_matches_source_id", "source_id"),
        Index("ix_reference_matches_reference_entry_id", "reference_entry_id"),
        UniqueConstraint(
            "user_id",
            "target_type",
            "target_key",
            "source_id",
            "reference_entry_id",
            "match_type",
            name="uq_reference_matches_target_source_entry_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[ReferenceMatchTargetType] = mapped_column(
        SqlEnum(
            ReferenceMatchTargetType,
            name="reference_match_target_type",
            values_callable=enum_values,
            validate_strings=True,
        ),
        nullable=False,
    )
    target_key: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("reference_sources.id", ondelete="CASCADE"),
        nullable=False,
    )
    reference_entry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("reference_entries.id", ondelete="CASCADE"),
        nullable=False,
    )
    match_type: Mapped[ReferenceMatchType] = mapped_column(
        SqlEnum(
            ReferenceMatchType,
            name="reference_match_type",
            values_callable=enum_values,
            validate_strings=True,
        ),
        nullable=False,
    )
    match_score: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    matched_form: Mapped[str] = mapped_column(Text, nullable=False)

    source: Mapped["ReferenceSource"] = relationship(back_populates="matches")
    reference_entry: Mapped["ReferenceEntry"] = relationship(back_populates="matches")


class ReferenceMatchRunResult(UpdatedTimestampMixin, Base):
    __tablename__ = "reference_match_run_results"
    __table_args__ = (
        Index("ix_reference_match_run_results_run_id", "run_id"),
        Index("ix_reference_match_run_results_user_id_run_id", "user_id", "run_id"),
        Index("ix_reference_match_run_results_run_id_match_status", "run_id", "match_status"),
        Index("ix_reference_match_run_results_run_id_normalized_form", "run_id", "normalized_form"),
        Index("ix_reference_match_run_results_run_id_target_type", "run_id", "target_type"),
        UniqueConstraint("run_id", "target_type", "target_key", name="uq_reference_match_run_results_target"),
        UniqueConstraint("run_id", "reference_entry_id", name="uq_reference_match_run_results_reference_entry"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("reference_match_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    matching_direction: Mapped[ReferenceMatchingDirection] = mapped_column(
        SqlEnum(
            ReferenceMatchingDirection,
            name="reference_matching_direction",
            values_callable=enum_values,
            validate_strings=True,
        ),
        nullable=False,
        default=ReferenceMatchingDirection.SOURCE_TO_INTERNAL,
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("reference_sources.id", ondelete="SET NULL"),
        nullable=True,
    )
    reference_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("reference_entries.id", ondelete="CASCADE"),
        nullable=True,
    )
    target_type: Mapped[ReferenceMatchTargetType] = mapped_column(
        SqlEnum(
            ReferenceMatchTargetType,
            name="reference_match_target_type",
            values_callable=enum_values,
            validate_strings=True,
        ),
        nullable=False,
    )
    target_key: Mapped[str] = mapped_column(Text, nullable=False)
    target_label: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_form: Mapped[str] = mapped_column(Text, nullable=False, default="")
    match_status: Mapped[ReferenceMatchStatus] = mapped_column(
        SqlEnum(
            ReferenceMatchStatus,
            name="reference_match_status",
            values_callable=enum_values,
            validate_strings=True,
        ),
        nullable=False,
    )
    best_match_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    best_match_type: Mapped[ReferenceMatchType | None] = mapped_column(
        SqlEnum(
            ReferenceMatchType,
            name="reference_match_type",
            values_callable=enum_values,
            validate_strings=True,
        ),
        nullable=True,
    )
    best_match_score: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    best_source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("reference_sources.id", ondelete="SET NULL"),
        nullable=True,
    )
    best_source_display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    best_matched_form: Mapped[str | None] = mapped_column(Text, nullable=True)
    match_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    exists_in_lexicon: Mapped[bool] = mapped_column(default=False, nullable=False)
    matching_lexeme_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    best_lexeme_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("lexemes.id", ondelete="SET NULL"),
        nullable=True,
    )
    best_lexeme_canonical_form: Mapped[str | None] = mapped_column(Text, nullable=True)
    found_in_books: Mapped[bool] = mapped_column(default=False, nullable=False)
    matching_book_occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    best_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="SET NULL"),
        nullable=True,
    )
    best_document_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    best_page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    best_context_snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_import_method: Mapped[ReferenceImportMethod | None] = mapped_column(
        SqlEnum(
            ReferenceImportMethod,
            name="reference_import_method",
            values_callable=enum_values,
            validate_strings=True,
        ),
        nullable=True,
    )
    source_warning: Mapped[str | None] = mapped_column(Text, nullable=True)
    related_resource_type: Mapped[ReferenceMatchTargetType | None] = mapped_column(
        SqlEnum(
            ReferenceMatchTargetType,
            name="reference_match_target_type",
            values_callable=enum_values,
            validate_strings=True,
        ),
        nullable=True,
    )
    related_resource_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    run: Mapped["ReferenceMatchRun"] = relationship(back_populates="results")
    best_source: Mapped["ReferenceSource | None"] = relationship(
        back_populates="run_results",
        foreign_keys=[best_source_id],
    )
    matches: Mapped[list["ReferenceMatchRunResultMatch"]] = relationship(
        back_populates="result",
        cascade="all, delete-orphan",
        order_by="ReferenceMatchRunResultMatch.created_at",
    )


class ReferenceMatchRunResultMatch(TimestampMixin, Base):
    __tablename__ = "reference_match_run_result_matches"
    __table_args__ = (
        Index("ix_reference_match_run_result_matches_result_id", "result_id"),
        Index("ix_reference_match_run_result_matches_run_id", "run_id"),
        Index("ix_reference_match_run_result_matches_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    result_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("reference_match_run_results.id", ondelete="CASCADE"),
        nullable=False,
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("reference_match_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_display_name: Mapped[str] = mapped_column(Text, nullable=False)
    reference_entry_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    surface_form: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_form: Mapped[str] = mapped_column(Text, nullable=False)
    match_type: Mapped[ReferenceMatchType] = mapped_column(
        SqlEnum(
            ReferenceMatchType,
            name="reference_match_type",
            values_callable=enum_values,
            validate_strings=True,
        ),
        nullable=False,
    )
    match_score: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    source_import_method: Mapped[ReferenceImportMethod | None] = mapped_column(
        SqlEnum(
            ReferenceImportMethod,
            name="reference_import_method",
            values_callable=enum_values,
            validate_strings=True,
        ),
        nullable=True,
    )
    source_warning: Mapped[str | None] = mapped_column(Text, nullable=True)

    result: Mapped["ReferenceMatchRunResult"] = relationship(back_populates="matches")


class ReferenceSourceImport(UpdatedTimestampMixin, Base):
    __tablename__ = "reference_source_imports"
    __table_args__ = (
        Index("ix_reference_source_imports_source_id", "source_id"),
        Index("ix_reference_source_imports_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("reference_sources.id", ondelete="CASCADE"),
        nullable=False,
    )
    retry_of_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("reference_source_imports.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    file_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    storage_bucket: Mapped[str | None] = mapped_column(Text, nullable=True)
    storage_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[ReferenceImportStatus] = mapped_column(
        SqlEnum(
            ReferenceImportStatus,
            name="reference_import_status",
            values_callable=enum_values,
            validate_strings=True,
        ),
        nullable=False,
        default=ReferenceImportStatus.QUEUED,
    )
    import_method: Mapped[ReferenceImportMethod | None] = mapped_column(
        SqlEnum(
            ReferenceImportMethod,
            name="reference_import_method",
            values_callable=enum_values,
            validate_strings=True,
        ),
        nullable=True,
    )
    rows_read: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rows_imported: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rows_skipped: Mapped[int | None] = mapped_column(Integer, nullable=True)
    warning_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message_user: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_steps: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    current_stage_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_stage_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    stage_message_user: Mapped[str | None] = mapped_column(Text, nullable=True)
    progress_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_processed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    items_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result_resource_type: Mapped[JobResultResourceType | None] = mapped_column(
        SqlEnum(
            JobResultResourceType,
            name="job_result_resource_type",
            values_callable=enum_values,
            validate_strings=True,
        ),
        nullable=True,
    )
    result_resource_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    can_retry: Mapped[bool] = mapped_column(nullable=False, default=True)
    last_retried_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    source: Mapped["ReferenceSource"] = relationship(back_populates="imports")


class JobStageEvent(TimestampMixin, Base):
    __tablename__ = "job_stage_events"
    __table_args__ = (
        Index("ix_job_stage_events_job_kind_job_id", "job_kind", "job_id"),
        Index("ix_job_stage_events_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_kind: Mapped[JobKind] = mapped_column(
        SqlEnum(
            JobKind,
            name="job_kind",
            values_callable=enum_values,
            validate_strings=True,
        ),
        nullable=False,
    )
    job_id: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    stage_code: Mapped[str] = mapped_column(Text, nullable=False)
    stage_label: Mapped[str] = mapped_column(Text, nullable=False)
    message_user: Mapped[str | None] = mapped_column(Text, nullable=True)
    progress_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    items_processed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    items_total: Mapped[int | None] = mapped_column(Integer, nullable=True)


class IngestionJob(UpdatedTimestampMixin, Base):
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
    current_stage_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_stage_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    stage_message_user: Mapped[str | None] = mapped_column(Text, nullable=True)
    progress_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_processed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    items_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message_user: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message_technical: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_steps: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    result_resource_type: Mapped[JobResultResourceType | None] = mapped_column(
        SqlEnum(
            JobResultResourceType,
            name="job_result_resource_type",
            values_callable=enum_values,
            validate_strings=True,
        ),
        nullable=True,
    )
    result_resource_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    can_retry: Mapped[bool] = mapped_column(nullable=False, default=True)
    last_retried_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    document: Mapped["Document"] = relationship(back_populates="jobs")
