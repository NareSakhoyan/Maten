from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.db.models import LexemeStatus
from app.schemas.common import APIModel, OffsetPagination


class LexemeCreateRequest(APIModel):
    canonical_form: str = Field(min_length=1)
    normalized_forms: list[str] = Field(min_length=1)
    notes: str | None = None
    status: LexemeStatus = LexemeStatus.DRAFT


class LexemeUpdateRequest(APIModel):
    canonical_form: str | None = None
    notes: str | None = None
    status: LexemeStatus | None = None


class LexemeSummary(APIModel):
    id: UUID
    canonical_form: str
    canonical_normalized_form: str
    status: LexemeStatus
    notes: str | None
    form_count: int
    occurrence_count: int
    created_at: datetime
    updated_at: datetime


class LexemeDetail(APIModel):
    id: UUID
    canonical_form: str
    canonical_normalized_form: str
    status: LexemeStatus
    notes: str | None
    normalized_forms: list[str]
    occurrence_count: int
    sample_contexts: list[str]
    created_at: datetime
    updated_at: datetime


class LexemeMergeGroupsRequest(APIModel):
    normalized_forms: list[str] = Field(min_length=1)


class LexemeListResponse(OffsetPagination):
    items: list[LexemeSummary]
