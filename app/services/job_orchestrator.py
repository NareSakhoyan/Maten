from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.celery_app import celery_app
from app.db.models import JobKind
from app.services.job_progress_service import JobProgressService, get_job_progress_service


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class JobTaskSpec:
    task_name: str


JOB_TASKS: dict[JobKind, JobTaskSpec] = {
    JobKind.INGESTION: JobTaskSpec("app.workers.tasks.process_document_ingestion"),
    JobKind.REFERENCE_IMPORT: JobTaskSpec("app.workers.tasks.process_reference_source_import"),
    JobKind.REFERENCE_MATCHING: JobTaskSpec("app.workers.tasks.process_reference_matching_run"),
    JobKind.MORPHOLOGY: JobTaskSpec("app.workers.tasks.process_morphology_run"),
}


class JobOrchestrator:
    """Centralizes Celery enqueue (idempotent by job id) and terminal stage emission."""

    def __init__(self, *, job_progress_service: JobProgressService | None = None) -> None:
        self.job_progress_service = job_progress_service or get_job_progress_service()

    def enqueue(
        self,
        job_kind: JobKind,
        job_id: UUID,
        *,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> None:
        spec = JOB_TASKS.get(job_kind)
        if spec is None:
            raise ValueError(f"No Celery task registered for job kind {job_kind!r}.")

        celery_app.send_task(
            spec.task_name,
            args=args or [str(job_id)],
            kwargs=kwargs or {},
            task_id=str(job_id),
        )
        logger.info("Enqueued %s job_id=%s", job_kind.value, job_id)

    def mark_running(
        self,
        session: Session,
        *,
        job_kind: JobKind,
        job: object,
        stage_code: str,
        progress_percent: int | None = None,
        message_user: str | None = None,
    ) -> None:
        self.job_progress_service.set_stage(
            session,
            job_kind=job_kind,
            job=job,
            stage_code=stage_code,
            progress_percent=progress_percent,
            message_user=message_user,
        )

    def mark_completed(
        self,
        session: Session,
        *,
        job_kind: JobKind,
        job: object,
        message_user: str | None = None,
    ) -> None:
        self.job_progress_service.complete(
            session,
            job_kind=job_kind,
            job=job,
            message_user=message_user,
        )

    def mark_failed(
        self,
        session: Session,
        *,
        job_kind: JobKind,
        job: object,
        message_user: str,
    ) -> None:
        self.job_progress_service.fail(
            session,
            job_kind=job_kind,
            job=job,
            message_user=message_user,
        )


def get_job_orchestrator() -> JobOrchestrator:
    return JobOrchestrator()
