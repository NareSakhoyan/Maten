from __future__ import annotations

import enum
from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.db.models import OccurrenceScriptType
from app.schemas.common import APIModel, OffsetPagination
from app.schemas.reference import ReferenceMatchBest


class LexiconGroupView(str, enum.Enum):
    CANDIDATES = "candidates"
    LINKED = "linked"
    SUSPICIOUS = "suspicious"
    IGNORED = "ignored"
    ALL = "all"


class LexiconGroupSortKey(str, enum.Enum):
    NORMALIZED_FORM = "normalized_form"
    OCCURRENCE_COUNT = "occurrence_count"
    PAGE_COUNT = "page_count"
    GROUP_STATE = "group_state"
    DOMINANT_SCRIPT_TYPE = "dominant_script_type"


class LexiconGroupSortDirection(str, enum.Enum):
    ASC = "asc"
    DESC = "desc"


class LexiconGroupState(str, enum.Enum):
    UNREVIEWED = "unreviewed"
    LINKED = "linked"
    IGNORED_NOISE = "ignored_noise"


class LexiconGroupOccurrenceRead(APIModel):
    id: UUID
    document_id: UUID
    document_title: str
    original_filename: str | None = None
    page_id: UUID
    page_number: int
    page_image_available: bool = False
    page_image_api_path: str | None = None
    token: str
    normalized_token: str
    context_snippet: str
    context_highlight_start: int | None = None
    context_highlight_end: int | None = None
    created_at: datetime


class LexiconGroupSummary(APIModel):
    normalized_form: str
    occurrence_count: int
    document_count: int
    page_count: int
    sample_tokens: list[str]
    sample_contexts: list[str]
    sample_document_titles: list[str]
    linked_lexeme_id: UUID | None
    linked_lexeme_canonical_form: str | None
    group_state: LexiconGroupState
    dominant_script_type: OccurrenceScriptType
    is_suspicious: bool
    suspicion_reasons: list[str]
    has_reference_match: bool = False
    reference_match_count: int = 0
    best_reference_match: ReferenceMatchBest | None = None


class LexiconGroupDetail(APIModel):
    normalized_form: str
    occurrence_count: int
    document_count: int
    page_count: int
    linked_lexeme_id: UUID | None
    linked_lexeme_canonical_form: str | None
    group_state: LexiconGroupState
    dominant_script_type: OccurrenceScriptType
    is_suspicious: bool
    suspicion_reasons: list[str]
    has_reference_match: bool = False
    reference_match_count: int = 0
    best_reference_match: ReferenceMatchBest | None = None
    occurrences: list[LexiconGroupOccurrenceRead]


class LexiconGroupListResponse(OffsetPagination):
    items: list[LexiconGroupSummary]


class LexiconIndexRebuildResponse(APIModel):
    message: str
    form_count: int | None = None
    document_count: int | None = None
    task_id: str | None = None


class LexiconActionType(str, enum.Enum):
    CREATE_LEXEME = "create_lexeme"
    MERGE_INTO_LEXEME = "merge_into_lexeme"
    IGNORE = "ignore"
    UNIGNORE = "unignore"


class LexiconActionRequest(APIModel):
    action: LexiconActionType
    normalized_forms: list[str] = Field(min_length=1)
    lexeme_id: UUID | None = None
    canonical_form: str | None = None
    status: str | None = None
    notes: str | None = None
    reviewer_note: str | None = None


class LexiconActionResponse(APIModel):
    action: LexiconActionType
    normalized_forms: list[str]
    group_state: LexiconGroupState | None = None
    lexeme_id: UUID | None = None
    lexeme_canonical_form: str | None = None
