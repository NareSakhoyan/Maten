from __future__ import annotations

from datetime import datetime
from uuid import UUID

from app.db.models import DocumentStatus
from app.schemas.common import APIModel, OffsetPagination
from app.schemas.job import LongRunningJobRead


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
    latest_job_id: UUID | None = None
    latest_job_status: str | None = None
    word_candidate_count: int | None = None
    unmatched_candidate_count: int | None = None
    linked_candidate_count: int | None = None
    suspicious_candidate_count: int | None = None
    created_at: datetime
    updated_at: datetime


class DocumentStartResponse(APIModel):
    message: str
    document: DocumentRead
    job: LongRunningJobRead


class DocumentListResponse(OffsetPagination):
    items: list[DocumentRead]
