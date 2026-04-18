from __future__ import annotations

from datetime import datetime
from uuid import UUID

from app.db.models import IngestionJobStatus
from app.schemas.common import APIModel


class IngestionJobRead(APIModel):
    id: UUID
    document_id: UUID
    user_id: UUID
    status: IngestionJobStatus
    step: str | None
    progress_percent: int
    error_message: str | None
    error_code: str | None = None
    error_message_user: str | None = None
    next_steps: list[str] | None = None
    can_retry: bool = True
    retry_count: int = 0
    last_retried_at: datetime | None = None
    retry_of_job_id: UUID | None = None
    latest_retry_job_id: UUID | None = None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


class RetryJobResponse(APIModel):
    message: str
    job: IngestionJobRead
