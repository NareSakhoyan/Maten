from __future__ import annotations

from datetime import datetime
from uuid import UUID

from app.db.models import JobKind, JobResultResourceType
from app.schemas.common import APIModel, JobProgressState, OffsetPagination


class LongRunningJobRead(JobProgressState):
    id: UUID
    job_kind: JobKind
    user_id: str
    status: str
    can_retry: bool | None = None
    latest_retry_job_id: UUID | None = None
    latest_retry_job_status: str | None = None
    error_code: str | None = None
    error_message_user: str | None = None
    next_steps: list[str] | None = None
    result_resource_type: JobResultResourceType | None = None
    result_resource_id: str | None = None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


class LongRunningJobListResponse(OffsetPagination):
    items: list[LongRunningJobRead]


class RetryJobStartResponse(APIModel):
    message: str
    document_id: UUID | None = None
    job: LongRunningJobRead
