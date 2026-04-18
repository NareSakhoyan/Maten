from __future__ import annotations

from datetime import datetime
from uuid import UUID

from app.schemas.common import APIModel, OffsetPagination


class OccurrenceRead(APIModel):
    id: UUID
    document_id: UUID
    page_id: UUID
    page_number: int
    token: str
    normalized_token: str
    context_snippet: str
    char_start: int | None
    char_end: int | None
    created_at: datetime


class OccurrenceListResponse(OffsetPagination):
    items: list[OccurrenceRead]
