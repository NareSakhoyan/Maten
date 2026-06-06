from __future__ import annotations

from datetime import datetime
from uuid import UUID

from app.db.models import JobKind, JobResultResourceType
from pydantic import Field

from app.schemas.common import APIModel, JobProgressState, OffsetPagination


class LongRunningJobRead(JobProgressState):
    id: UUID
    job_kind: JobKind
    user_id: str
    owner_email: str | None = None
    owner_display_name: str | None = None
    status: str
    can_retry: bool | None = None
    can_resume: bool | None = None
    resume_from_page: int | None = Field(default=None, ge=1)
    resume_of_job_id: UUID | None = None
    latest_retry_job_id: UUID | None = None
    latest_retry_job_status: str | None = None
    latest_resume_job_id: UUID | None = None
    latest_resume_job_status: str | None = None
    error_code: str | None = None
    error_message_user: str | None = None
    next_steps: list[str] | None = None
    result_resource_type: JobResultResourceType | None = None
    result_resource_id: str | None = None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime
    is_stale: bool = False
    stale_detected_at: datetime | None = None
    last_progress_at: datetime | None = None
    recovery_note: str | None = None


class LongRunningJobListResponse(OffsetPagination):
    items: list[LongRunningJobRead]


class RetryJobStartResponse(APIModel):
    message: str
    document_id: UUID | None = None
    job: LongRunningJobRead


class ResumeJobStartResponse(APIModel):
    message: str
    document_id: UUID | None = None
    job: LongRunningJobRead
