from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import DocumentNayiriLookupRun, IngestionJob, IngestionJobStatus, MorphologyRunStatus
from app.services.long_running_job_service import LongRunningJobService, get_long_running_job_service


class BackpressureLimitError(RuntimeError):
    pass


class BackpressureService:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        long_running_job_service: LongRunningJobService | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.long_running_job_service = long_running_job_service or get_long_running_job_service()

    def ensure_user_capacity(self, session: Session, *, user_id: UUID) -> None:
        active_count = self.long_running_job_service.count_active_jobs(session, user_id=user_id)
        if active_count >= self.settings.max_active_jobs_per_user:
            raise BackpressureLimitError(
                "Too many jobs are already queued or running for this user. Try again after one finishes."
            )

    def ensure_ocr_capacity(self, session: Session) -> None:
        active_count = session.scalar(
            select(func.count(IngestionJob.id)).where(
                IngestionJob.status.in_((IngestionJobStatus.QUEUED, IngestionJobStatus.RUNNING)),
            )
        ) or 0
        if active_count >= self.settings.max_active_ocr_jobs_global:
            raise BackpressureLimitError(
                "OCR capacity is currently full. Try again after the active upload job advances."
            )

    def ensure_external_lookup_capacity(self, session: Session) -> None:
        active_count = session.scalar(
            select(func.count(DocumentNayiriLookupRun.id)).where(
                DocumentNayiriLookupRun.status.in_((MorphologyRunStatus.QUEUED, MorphologyRunStatus.RUNNING)),
            )
        ) or 0
        if active_count >= self.settings.max_active_external_lookups_global:
            raise BackpressureLimitError(
                "External lookup capacity is currently full. Try again after the active lookup finishes."
            )


def get_backpressure_service() -> BackpressureService:
    return BackpressureService()
