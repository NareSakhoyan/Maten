from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.db.models import JobKind


class APIModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class OffsetPagination(BaseModel):
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class HealthResponse(BaseModel):
    status: str


class HealthComponentStatus(APIModel):
    status: str
    detail: str | None = None


class ReadinessResponse(APIModel):
    status: str
    database: HealthComponentStatus
    redis: HealthComponentStatus


class JobProgressState(APIModel):
    current_stage_code: str | None = None
    current_stage_label: str | None = None
    stage_message_user: str | None = None
    progress_percent: int = Field(ge=0, le=100)
    items_processed: int | None = Field(default=None, ge=0)
    items_total: int | None = Field(default=None, ge=0)


class JobStageEventRead(APIModel):
    id: UUID
    job_kind: JobKind
    job_id: str
    stage_code: str
    stage_label: str
    message_user: str | None = None
    progress_percent: int | None = Field(default=None, ge=0, le=100)
    items_processed: int | None = Field(default=None, ge=0)
    items_total: int | None = Field(default=None, ge=0)
    created_at: datetime


class JobStageEventListResponse(OffsetPagination):
    items: list[JobStageEventRead]
