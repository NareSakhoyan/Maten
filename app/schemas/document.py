from __future__ import annotations

from datetime import datetime
from uuid import UUID

from app.db.models import DocumentStatus
from app.schemas.common import APIModel, OffsetPagination
from app.schemas.job import IngestionJobRead


class DocumentRead(APIModel):
    id: UUID
    user_id: UUID
    title: str
    original_filename: str
    mime_type: str
    file_size_bytes: int
    storage_bucket: str
    storage_path: str
    sha256: str
    page_count: int | None
    status: DocumentStatus
    created_at: datetime
    updated_at: datetime


class DocumentUploadResponse(APIModel):
    document: DocumentRead
    job: IngestionJobRead


class DocumentListResponse(OffsetPagination):
    items: list[DocumentRead]
