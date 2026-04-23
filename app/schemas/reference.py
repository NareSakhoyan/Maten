from __future__ import annotations

import enum
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field

from app.db.models import (
    JobKind,
    JobResultResourceType,
    ReferenceImportStatus,
    ReferenceMatchingDirection,
    ReferenceMatchRunScope,
    ReferenceMatchRunStatus,
    ReferenceMatchStatus,
    ReferenceMatchTargetScope,
    ReferenceMatchTargetType,
    ReferenceMatchType,
    ReferenceSourceType,
)
from app.schemas.common import APIModel, JobProgressState, OffsetPagination
from app.schemas.reference_enums import SupportedReferenceImportMethod
from app.schemas.job import LongRunningJobRead


class ReferenceStatusFilter(str, enum.Enum):
    MATCHED = "matched"
    UNMATCHED = "unmatched"
    ALL = "all"


class ReferenceSourceCreateRequest(APIModel):
    display_name: str = Field(min_length=1)
    description: str | None = None
    source_type: ReferenceSourceType = ReferenceSourceType.IMPORTED_WORDLIST
    language: str | None = None


class ReferenceSourceSummary(APIModel):
    id: UUID
    key: str
    display_name: str
    description: str | None
    source_type: ReferenceSourceType
    language: str | None
    is_active: bool
    entry_count: int
    last_import_method: SupportedReferenceImportMethod | None = None
    last_import_warning: str | None = None
    last_imported_at: datetime | None = None
    latest_import_job_id: UUID | None = None
    latest_import_job_status: str | None = None
    created_at: datetime
    updated_at: datetime


class ReferenceSourceDetail(ReferenceSourceSummary):
    latest_import: "ReferenceImportResponse | None" = None
    latest_match_run_id: UUID | None = None
    latest_match_run_status: str | None = None
    imported_entry_count: int | None = None
    matched_entry_count: int | None = None
    unmatched_entry_count: int | None = None


class ReferenceImportResponse(JobProgressState):
    id: UUID
    job_kind: JobKind = JobKind.REFERENCE_IMPORT
    source_id: UUID
    source_display_name: str
    status: ReferenceImportStatus
    file_name: str | None = None
    file_type: str | None = None
    rows_read: int | None = None
    rows_imported: int | None = None
    rows_skipped: int | None = None
    import_method: SupportedReferenceImportMethod | None = None
    warning_message: str | None = None
    error_message: str | None = None
    error_code: str | None = None
    error_message_user: str | None = None
    next_steps: list[str] | None = None
    result_resource_type: JobResultResourceType | None = None
    result_resource_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ReferenceImportListResponse(OffsetPagination):
    items: list[ReferenceImportResponse]


class ReferenceImportStartResponse(APIModel):
    message: str
    source: ReferenceSourceDetail
    job: LongRunningJobRead
    import_run: ReferenceImportResponse


class ReferenceMatchBest(APIModel):
    source_display_name: str
    matched_form: str
    match_type: ReferenceMatchType
    match_score: float | None = None


class ReferenceMatchDetail(APIModel):
    source_id: UUID
    source_display_name: str
    reference_entry_id: UUID
    surface_form: str
    normalized_form: str
    match_type: ReferenceMatchType
    match_score: float | None = None
    source_import_method: SupportedReferenceImportMethod | None = None
    source_warning: str | None = None
    metadata_json: dict[str, Any] | None = None


class ReferenceTargetMatchesResponse(APIModel):
    target_type: ReferenceMatchTargetType
    target_key: str
    has_match: bool
    matches: list[ReferenceMatchDetail]


class ReferenceMatchRunCreateRequest(APIModel):
    matching_direction: ReferenceMatchingDirection = ReferenceMatchingDirection.SOURCE_TO_INTERNAL
    source_id: UUID | None = None
    target_scope: ReferenceMatchTargetScope = ReferenceMatchTargetScope.ALL_INTERNAL
    run_scope: ReferenceMatchRunScope = ReferenceMatchRunScope.ALL
    view: str = "candidates"
    include_fuzzy: bool = False


class ReferenceMatchRunSummary(JobProgressState):
    id: UUID
    job_kind: JobKind = JobKind.REFERENCE_MATCHING
    matching_direction: ReferenceMatchingDirection
    source_id: UUID | None = None
    target_scope: ReferenceMatchTargetScope | None = None
    run_scope: ReferenceMatchRunScope
    status: ReferenceMatchRunStatus
    total_items: int | None
    matched_items: int | None
    unmatched_items: int | None = None
    exact_match_count: int | None = None
    normalized_match_count: int | None = None
    fuzzy_match_count: int | None = None
    error_message: str | None
    error_code: str | None = None
    error_message_user: str | None = None
    next_steps: list[str] | None = None
    result_resource_type: JobResultResourceType | None = None
    result_resource_id: str | None = None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ReferenceMatchRunDetail(ReferenceMatchRunSummary):
    pass


class ReferenceMatchRunListResponse(OffsetPagination):
    items: list[ReferenceMatchRunSummary]


class ReferenceMatchRunResultTargetTypeFilter(str, enum.Enum):
    LEXICON_GROUP = "lexicon_group"
    LEXEME = "lexeme"
    ALL = "all"


class ReferenceMatchRunResultSummary(APIModel):
    id: UUID
    run_id: UUID
    matching_direction: ReferenceMatchingDirection
    source_id: UUID | None = None
    reference_entry_id: UUID | None = None
    target_type: ReferenceMatchTargetType
    target_key: str
    target_label: str
    normalized_form: str | None = None
    target_lexeme_id: UUID | None = None
    match_status: ReferenceMatchStatus
    match_count: int
    best_match_type: ReferenceMatchType | None = None
    best_match_score: float | None = None
    best_source_id: UUID | None = None
    best_source_display_name: str | None = None
    best_matched_form: str | None = None
    related_resource_type: ReferenceMatchTargetType | None = None
    related_resource_id: str | None = None
    created_at: datetime
    updated_at: datetime


class ReferenceMatchRunResultListResponse(OffsetPagination):
    items: list[ReferenceMatchRunResultSummary]


class ReferenceMatchRunResultDetail(ReferenceMatchRunResultSummary):
    matches: list[ReferenceMatchDetail]


class ReferenceMatchingBookContext(APIModel):
    document_id: UUID
    document_title: str
    page_number: int | None = None
    context_snippet: str | None = None
    occurrence_id: UUID | None = None
    reference_link: str | None = None


class ReferenceMatchingLexemeSummary(APIModel):
    lexeme_id: UUID
    canonical_form: str
    canonical_normalized_form: str


class ReferenceMatchRunEntryResultSummary(APIModel):
    id: UUID
    run_id: UUID
    reference_entry_id: UUID
    source_id: UUID
    target_label: str
    normalized_form: str
    match_status: ReferenceMatchStatus
    match_count: int
    source_import_method: SupportedReferenceImportMethod | None = None
    source_warning: str | None = None
    exists_in_lexicon: bool
    best_lexeme_id: UUID | None = None
    best_lexeme_canonical_form: str | None = None
    matching_lexeme_count: int = 0
    found_in_books: bool
    matching_book_occurrence_count: int = 0
    best_document_id: UUID | None = None
    best_document_title: str | None = None
    best_page_number: int | None = None
    best_context_snippet: str | None = None
    created_at: datetime
    updated_at: datetime


class ReferenceMatchRunEntrySourceDetail(APIModel):
    reference_entry_id: UUID
    surface_form: str
    normalized_form: str
    source_id: UUID
    source_display_name: str
    source_description: str | None = None
    source_import_method: SupportedReferenceImportMethod | None = None
    source_warning: str | None = None
    source_metadata: dict[str, Any] | None = None


class ReferenceMatchRunEntryResultListResponse(OffsetPagination):
    items: list[ReferenceMatchRunEntryResultSummary]


class ReferenceMatchRunEntryResultDetail(ReferenceMatchRunEntryResultSummary):
    source_entry: ReferenceMatchRunEntrySourceDetail
    matching_lexemes: list[ReferenceMatchingLexemeSummary] = Field(default_factory=list)
    book_evidence: list[ReferenceMatchingBookContext] = Field(default_factory=list)


class ReferenceMatchRunEntryResultScopeFilter(str, enum.Enum):
    LEXICON_ONLY = "lexicon_only"
    BOOKS_ONLY = "books_only"
    ANY = "any"


class ReferenceSourceEntrySummary(APIModel):
    reference_entry_id: UUID
    surface_form: str
    normalized_form: str
    source_import_method: SupportedReferenceImportMethod | None = None
    source_warning: str | None = None
    latest_match_status: ReferenceMatchStatus | None = None
    latest_match_count: int | None = None
    exists_in_lexicon: bool | None = None
    found_in_books: bool | None = None
    best_lexeme_id: UUID | None = None
    best_lexeme_canonical_form: str | None = None
    best_document_id: UUID | None = None
    best_document_title: str | None = None
    best_page_number: int | None = None
    best_context_snippet: str | None = None
    created_at: datetime
    updated_at: datetime


class ReferenceSourceEntryListResponse(OffsetPagination):
    items: list[ReferenceSourceEntrySummary]


class ReferenceMatchingStartResponse(APIModel):
    message: str
    run: ReferenceMatchRunDetail
    job: LongRunningJobRead


ReferenceSourceDetail.model_rebuild()
