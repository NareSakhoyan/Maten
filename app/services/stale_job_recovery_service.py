from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import (
    DiscoveryBuildRun,
    DocumentNayiriLookupRun,
    IngestionJob,
    IngestionJobStatus,
    JobKind,
    JobStageEvent,
    MorphologyRun,
    MorphologyRunStatus,
    ReferenceImportStatus,
    ReferenceMatchRun,
    ReferenceMatchRunStatus,
    ReferenceSourceImport,
)
from app.schemas.job import LongRunningJobRead
from app.services.document_service import DocumentService, get_document_service
from app.services.ingestion_error_service import IngestionFailureInfo, IngestionRetryError
from app.services.job_orchestrator import get_job_orchestrator
from app.services.job_progress_service import JobProgressService, get_job_progress_service

logger = logging.getLogger(__name__)

STALE_ERROR_CODE = "job_stale_no_progress"
STALE_USER_MESSAGE = "This job stopped making progress before it completed."
STALE_NEXT_STEPS = [
    "An automatic retry may have started if this job type supports it.",
    "If the job is still stuck, use Retry or contact the administrator.",
]

AUTO_RETRY_KINDS = frozenset(
    {
        JobKind.INGESTION,
        JobKind.REFERENCE_IMPORT,
        JobKind.REFERENCE_MATCHING,
    }
)


@dataclass(slots=True)
class StaleRecoverySummary:
    scanned: int = 0
    recovered: int = 0
    auto_retried: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "scanned": self.scanned,
            "recovered": self.recovered,
            "auto_retried": self.auto_retried,
        }


class StaleJobRecoveryService:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        job_progress_service: JobProgressService | None = None,
        document_service: DocumentService | None = None,
        ingestion_job_service: object | None = None,
        reference_import_service: object | None = None,
        reference_matching_service: object | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.job_progress_service = job_progress_service or get_job_progress_service()
        self.document_service = document_service or get_document_service()
        self._ingestion_job_service = ingestion_job_service
        self._reference_import_service = reference_import_service
        self._reference_matching_service = reference_matching_service
        self.job_orchestrator = get_job_orchestrator()

    @property
    def ingestion_job_service(self) -> object:
        if self._ingestion_job_service is None:
            from app.services.ingestion_job_service import get_ingestion_job_service

            self._ingestion_job_service = get_ingestion_job_service()
        return self._ingestion_job_service

    @property
    def reference_import_service(self) -> object:
        if self._reference_import_service is None:
            from app.services.reference_import_service import get_reference_import_service

            self._reference_import_service = get_reference_import_service()
        return self._reference_import_service

    @property
    def reference_matching_service(self) -> object:
        if self._reference_matching_service is None:
            from app.services.reference_matching_service import get_reference_matching_service

            self._reference_matching_service = get_reference_matching_service()
        return self._reference_matching_service

    def sweep_stale_jobs(self, session: Session, *, limit: int = 100) -> StaleRecoverySummary:
        summary = StaleRecoverySummary()
        active_jobs = self._collect_active_jobs(session, limit=limit)
        for job_kind, job in active_jobs:
            summary.scanned += 1
            if self.reconcile_active_job(session, job_kind=job_kind, job=job):
                summary.recovered += 1
                if self._count_auto_retries(session, job_kind=job_kind, job_id=getattr(job, "id")) > 0:
                    summary.auto_retried += 1
        return summary

    def reconcile_active_job(self, session: Session, *, job_kind: JobKind, job: object) -> bool:
        if not self._is_active(job):
            return False
        bind = session.get_bind()
        try:
            if bind.dialect.name == "postgresql":
                session.execute(text("SET LOCAL lock_timeout = '5s'"))
            if not self.recover_job_if_stale(session, job_kind=job_kind, job=job):
                return False
            self._auto_retry_stale_job(session, job_kind=job_kind, job=job)
            session.refresh(job)
            return True
        except OperationalError as exc:
            session.rollback()
            if bind.dialect.name == "postgresql" and "lock timeout" in str(getattr(exc, "orig", exc)).lower():
                logger.warning(
                    "Skipped stale job recovery because the job row is locked job_kind=%s job_id=%s",
                    job_kind.value,
                    getattr(job, "id"),
                )
                return False
            raise

    def recover_job_if_stale(
        self,
        session: Session,
        *,
        job_kind: JobKind,
        job: object,
    ) -> bool:
        if not self._is_active(job):
            return False
        is_stale, last_progress_at = self.is_job_stale(session, job_kind=job_kind, job=job)
        if not is_stale:
            return False

        self._mark_job_stale(
            session,
            job_kind=job_kind,
            job=job,
            last_progress_at=last_progress_at,
        )
        logger.warning(
            "Marked stale job as failed job_kind=%s job_id=%s last_progress_at=%s",
            job_kind.value,
            getattr(job, "id"),
            last_progress_at.isoformat(),
        )
        return True

    def is_job_stale(
        self,
        session: Session,
        *,
        job_kind: JobKind,
        job: object,
    ) -> tuple[bool, datetime]:
        last_progress_at = self.get_last_progress_at(session, job_kind=job_kind, job=job)
        stale_after = timedelta(minutes=self.settings.job_stale_after_minutes)
        is_stale = datetime.now(timezone.utc) - last_progress_at >= stale_after
        return is_stale, last_progress_at

    def get_last_progress_at(self, session: Session, *, job_kind: JobKind, job: object) -> datetime:
        updated_at = self._ensure_utc(getattr(job, "updated_at", None))
        created_at = self._ensure_utc(getattr(job, "created_at", None))
        latest_event_at = self._ensure_utc(
            session.scalar(
                select(func.max(JobStageEvent.created_at)).where(
                    JobStageEvent.job_kind == job_kind,
                    JobStageEvent.job_id == str(getattr(job, "id")),
                )
            )
        )
        candidates = [value for value in (updated_at, latest_event_at) if value is not None]
        if candidates:
            return max(candidates)
        return created_at or datetime.now(timezone.utc)

    def enrich_job_read(self, session: Session, job: LongRunningJobRead) -> LongRunningJobRead:
        last_progress_at = self.get_last_progress_at_for_read(session, job)
        job.last_progress_at = last_progress_at

        if job.status in {"queued", "running"}:
            stale_after = timedelta(minutes=self.settings.job_stale_after_minutes)
            job.is_stale = datetime.now(timezone.utc) - last_progress_at >= stale_after
            return job

        job.is_stale = False
        if job.error_code == STALE_ERROR_CODE:
            job.stale_detected_at = job.finished_at
            job.recovery_note = self._recovery_note_for_failed_job(session, job)
        return job

    def get_last_progress_at_for_read(self, session: Session, job: LongRunningJobRead) -> datetime:
        updated_at = self._ensure_utc(job.updated_at)
        created_at = self._ensure_utc(job.created_at)
        latest_event_at = self._ensure_utc(
            session.scalar(
                select(func.max(JobStageEvent.created_at)).where(
                    JobStageEvent.job_kind == job.job_kind,
                    JobStageEvent.job_id == str(job.id),
                )
            )
        )
        candidates = [value for value in (updated_at, latest_event_at) if value is not None]
        if candidates:
            return max(candidates)
        return created_at or datetime.now(timezone.utc)

    def _auto_retry_stale_job(self, session: Session, *, job_kind: JobKind, job: object) -> UUID | None:
        if job_kind not in AUTO_RETRY_KINDS:
            return None
        if self._count_auto_retries(session, job_kind=job_kind, job_id=getattr(job, "id")) >= self.settings.job_stale_auto_retry_limit:
            return None

        user_id = self._job_user_id(job)
        try:
            if job_kind is JobKind.INGESTION:
                retry_job = self.ingestion_job_service.create_retry_job(
                    session,
                    user_id=user_id,
                    failed_job_id=getattr(job, "id"),
                )
                self.job_orchestrator.enqueue(JobKind.INGESTION, retry_job.id)
                self.document_service.mark_document_queued(session, document_id=retry_job.document_id)
                return retry_job.id
            if job_kind is JobKind.REFERENCE_IMPORT:
                failed_import = session.get(ReferenceSourceImport, getattr(job, "id"))
                if failed_import is None:
                    return None
                retry_import = self.reference_import_service.create_retry_import_run(
                    session,
                    user_id=user_id,
                    source_id=failed_import.source_id,
                    failed_import_id=failed_import.id,
                )
                self.job_orchestrator.enqueue(JobKind.REFERENCE_IMPORT, retry_import.id)
                return retry_import.id
            if job_kind is JobKind.REFERENCE_MATCHING:
                retry_run = self.reference_matching_service.create_retry_run(
                    session,
                    user_id=user_id,
                    failed_run_id=getattr(job, "id"),
                )
                self.job_orchestrator.enqueue(
                    JobKind.REFERENCE_MATCHING,
                    retry_run.id,
                    kwargs={
                        "view": retry_run.requested_view,
                        "include_fuzzy": retry_run.include_fuzzy,
                    },
                )
                return retry_run.id
        except (IngestionRetryError, ValueError) as exc:
            logger.warning(
                "Skipped auto-retry for stale job job_kind=%s job_id=%s reason=%s",
                job_kind.value,
                getattr(job, "id"),
                exc,
            )
            return None
        except Exception:
            logger.exception(
                "Auto-retry failed for stale job job_kind=%s job_id=%s",
                job_kind.value,
                getattr(job, "id"),
            )
            return None
        return None

    def _mark_job_stale(
        self,
        session: Session,
        *,
        job_kind: JobKind,
        job: object,
        last_progress_at: datetime,
    ) -> None:
        technical = (
            f"No database progress detected for {self.settings.job_stale_after_minutes} minutes. "
            f"Last progress at {last_progress_at.isoformat()}."
        )
        if job_kind is JobKind.INGESTION:
            failure_info = IngestionFailureInfo(
                error_code=STALE_ERROR_CODE,
                error_message_user=STALE_USER_MESSAGE,
                error_message_technical=technical,
                next_steps=list(STALE_NEXT_STEPS),
                can_retry=True,
            )
            self.document_service.mark_job_failed(
                session,
                document_id=getattr(job, "document_id"),
                job_id=getattr(job, "id"),
                failure_info=failure_info,
            )
            return

        if job_kind is JobKind.REFERENCE_IMPORT:
            import_run = job if isinstance(job, ReferenceSourceImport) else session.get(ReferenceSourceImport, getattr(job, "id"))
            if import_run is None:
                return
            import_run.status = ReferenceImportStatus.FAILED
            import_run.error_code = STALE_ERROR_CODE
            import_run.error_message = STALE_USER_MESSAGE
            import_run.error_message_user = STALE_USER_MESSAGE
            import_run.next_steps = list(STALE_NEXT_STEPS)
            import_run.can_retry = True
            self.job_progress_service.fail(
                session,
                job_kind=job_kind,
                job=import_run,
                message_user=STALE_USER_MESSAGE,
            )
            session.commit()
            return

        if job_kind is JobKind.REFERENCE_MATCHING:
            run = job if isinstance(job, ReferenceMatchRun) else session.get(ReferenceMatchRun, getattr(job, "id"))
            if run is None:
                return
            self.reference_matching_service._clear_run_results(session, run_id=run.id)  # noqa: SLF001
            run.status = ReferenceMatchRunStatus.FAILED
            run.error_code = STALE_ERROR_CODE
            run.error_message = STALE_USER_MESSAGE
            run.error_message_user = STALE_USER_MESSAGE
            run.next_steps = list(STALE_NEXT_STEPS)
            run.can_retry = True
            self.job_progress_service.fail(session, job_kind=job_kind, job=run, message_user=STALE_USER_MESSAGE)
            session.commit()
            return

        if job_kind is JobKind.MORPHOLOGY:
            run = job if isinstance(job, MorphologyRun) else session.get(MorphologyRun, getattr(job, "id"))
            if run is None:
                return
            run.status = MorphologyRunStatus.FAILED
            run.error_code = STALE_ERROR_CODE
            run.error_message = STALE_USER_MESSAGE
            run.error_message_user = STALE_USER_MESSAGE
            run.next_steps = list(STALE_NEXT_STEPS)
            run.can_retry = True
            self.job_progress_service.fail(session, job_kind=job_kind, job=run, message_user=STALE_USER_MESSAGE)
            session.commit()
            return

        if job_kind is JobKind.NAYIRI_TRUSTED_LOOKUP:
            run = job if isinstance(job, DocumentNayiriLookupRun) else session.get(DocumentNayiriLookupRun, getattr(job, "id"))
            if run is None:
                return
            run.status = MorphologyRunStatus.FAILED
            run.error_code = STALE_ERROR_CODE
            run.error_message = STALE_USER_MESSAGE
            run.error_message_user = STALE_USER_MESSAGE
            run.next_steps = list(STALE_NEXT_STEPS)
            run.can_retry = True
            self.job_progress_service.fail(session, job_kind=job_kind, job=run, message_user=STALE_USER_MESSAGE)
            session.commit()
            return

        if job_kind is JobKind.DISCOVERY_BUILD:
            run = job if isinstance(job, DiscoveryBuildRun) else session.get(DiscoveryBuildRun, getattr(job, "id"))
            if run is None:
                return
            run.status = MorphologyRunStatus.FAILED
            run.error_code = STALE_ERROR_CODE
            run.error_message = STALE_USER_MESSAGE
            run.error_message_user = STALE_USER_MESSAGE
            run.next_steps = list(STALE_NEXT_STEPS)
            run.can_retry = True
            self.job_progress_service.fail(session, job_kind=job_kind, job=run, message_user=STALE_USER_MESSAGE)
            session.commit()

    def _recovery_note_for_failed_job(self, session: Session, job: LongRunningJobRead) -> str | None:
        if job.latest_retry_job_id is None:
            if job.job_kind in AUTO_RETRY_KINDS:
                return "No automatic retry was started. Use Retry if available."
            return "This job was marked failed after no progress was detected. Start it again if needed."
        retry_status = job.latest_retry_job_status or "queued"
        return f"An automatic retry was started as job {job.latest_retry_job_id} (status: {retry_status})."

    def _collect_active_jobs(self, session: Session, *, limit: int) -> list[tuple[JobKind, object]]:
        per_kind_limit = max(limit, 1)
        jobs: list[tuple[JobKind, object]] = []

        ingestion_jobs = session.scalars(
            select(IngestionJob)
            .where(IngestionJob.status.in_([IngestionJobStatus.QUEUED, IngestionJobStatus.RUNNING]))
            .order_by(IngestionJob.updated_at.asc(), IngestionJob.id.asc())
            .limit(per_kind_limit)
        )
        jobs.extend((JobKind.INGESTION, job) for job in ingestion_jobs)

        reference_imports = session.scalars(
            select(ReferenceSourceImport)
            .where(ReferenceSourceImport.status.in_([ReferenceImportStatus.QUEUED, ReferenceImportStatus.RUNNING]))
            .order_by(ReferenceSourceImport.updated_at.asc(), ReferenceSourceImport.id.asc())
            .limit(per_kind_limit)
        )
        jobs.extend((JobKind.REFERENCE_IMPORT, job) for job in reference_imports)

        reference_matching = session.scalars(
            select(ReferenceMatchRun)
            .where(ReferenceMatchRun.status.in_([ReferenceMatchRunStatus.QUEUED, ReferenceMatchRunStatus.RUNNING]))
            .order_by(ReferenceMatchRun.updated_at.asc(), ReferenceMatchRun.id.asc())
            .limit(per_kind_limit)
        )
        jobs.extend((JobKind.REFERENCE_MATCHING, job) for job in reference_matching)

        morphology_runs = session.scalars(
            select(MorphologyRun)
            .where(MorphologyRun.status.in_([MorphologyRunStatus.QUEUED, MorphologyRunStatus.RUNNING]))
            .order_by(MorphologyRun.updated_at.asc(), MorphologyRun.id.asc())
            .limit(per_kind_limit)
        )
        jobs.extend((JobKind.MORPHOLOGY, job) for job in morphology_runs)

        nayiri_runs = session.scalars(
            select(DocumentNayiriLookupRun)
            .where(DocumentNayiriLookupRun.status.in_([MorphologyRunStatus.QUEUED, MorphologyRunStatus.RUNNING]))
            .order_by(DocumentNayiriLookupRun.updated_at.asc(), DocumentNayiriLookupRun.id.asc())
            .limit(per_kind_limit)
        )
        jobs.extend((JobKind.NAYIRI_TRUSTED_LOOKUP, job) for job in nayiri_runs)

        discovery_runs = session.scalars(
            select(DiscoveryBuildRun)
            .where(DiscoveryBuildRun.status.in_([MorphologyRunStatus.QUEUED, MorphologyRunStatus.RUNNING]))
            .order_by(DiscoveryBuildRun.updated_at.asc(), DiscoveryBuildRun.id.asc())
            .limit(per_kind_limit)
        )
        jobs.extend((JobKind.DISCOVERY_BUILD, job) for job in discovery_runs)

        jobs.sort(key=lambda item: (getattr(item[1], "updated_at"), getattr(item[1], "id")))
        return jobs[:limit]

    @staticmethod
    def _count_auto_retries(session: Session, *, job_kind: JobKind, job_id: UUID) -> int:
        if job_kind is JobKind.INGESTION:
            return int(
                session.scalar(
                    select(func.count(IngestionJob.id)).where(IngestionJob.retry_of_job_id == job_id)
                )
                or 0
            )
        if job_kind is JobKind.REFERENCE_IMPORT:
            return int(
                session.scalar(
                    select(func.count(ReferenceSourceImport.id)).where(ReferenceSourceImport.retry_of_job_id == job_id)
                )
                or 0
            )
        if job_kind is JobKind.REFERENCE_MATCHING:
            return int(
                session.scalar(
                    select(func.count(ReferenceMatchRun.id)).where(ReferenceMatchRun.retry_of_job_id == job_id)
                )
                or 0
            )
        return 0

    @staticmethod
    def _is_active(job: object) -> bool:
        status = getattr(job, "status", None)
        if status is None:
            return False
        return status.value in {"queued", "running"}

    @staticmethod
    def _job_user_id(job: object) -> UUID:
        user_id = getattr(job, "user_id")
        return user_id if isinstance(user_id, UUID) else UUID(str(user_id))

    @staticmethod
    def _ensure_utc(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


def get_stale_job_recovery_service() -> StaleJobRecoveryService:
    return StaleJobRecoveryService()
