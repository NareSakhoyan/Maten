from __future__ import annotations

import enum
from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.db.models import OccurrenceScriptType
from app.schemas.common import APIModel, OffsetPagination


class LexiconGroupView(str, enum.Enum):
    CANDIDATES = "candidates"
    LINKED = "linked"
    SUSPICIOUS = "suspicious"
    IGNORED = "ignored"
    ALL = "all"


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
    token: str
    normalized_token: str
    context_snippet: str
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
    occurrences: list[LexiconGroupOccurrenceRead]


class LexiconGroupListResponse(OffsetPagination):
    items: list[LexiconGroupSummary]


class LexiconGroupIgnoreRequest(APIModel):
    normalized_forms: list[str] = Field(min_length=1)
    reviewer_note: str | None = None


class LexiconGroupUnignoreRequest(APIModel):
    normalized_forms: list[str] = Field(min_length=1)


class LexiconGroupReviewActionResponse(APIModel):
    normalized_forms: list[str]
    group_state: LexiconGroupState
