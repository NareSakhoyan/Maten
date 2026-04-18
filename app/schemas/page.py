from __future__ import annotations

from datetime import datetime
from uuid import UUID

from app.db.models import ExtractionMethod
from app.schemas.common import APIModel, OffsetPagination


class DocumentPageRead(APIModel):
    id: UUID
    document_id: UUID
    page_number: int
    extraction_method: ExtractionMethod
    page_image_bucket: str | None
    page_image_path: str | None
    raw_extracted_text: str | None
    reconstructed_text: str | None
    extracted_text: str
    char_count: int
    created_at: datetime


class DocumentPageListResponse(OffsetPagination):
    items: list[DocumentPageRead]
