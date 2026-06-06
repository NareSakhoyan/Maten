from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Document, DocumentStatus, IngestionJob, IngestionJobStatus, JobKind, JobResultResourceType
from app.schemas.job import LongRunningJobRead
from app.services.ingestion_error_service import IngestionRetryError
from app.services.job_progress_service import JobProgressService, get_job_progress_service
from app.services.long_running_job_service import LongRunningJobService, get_long_running_job_service
from app.services.stale_job_recovery_service import StaleJobRecoveryService, get_stale_job_recovery_service
from app.services.storage_service import StorageService, get_storage_service


class IngestionJobService:
    def __init__(
        self,
        storage_service: StorageService | None = None,
        job_progress_service: JobProgressService | None = None,
        long_running_job_service: LongRunningJobService | None = None,
        stale_job_recovery_service: StaleJobRecoveryService | None = None,
    ) -> None:
        self.storage_service = storage_service or get_storage_service()
        self.job_progress_service = job_progress_service or get_job_progress_service()
        self.long_running_job_service = long_running_job_service or get_long_running_job_service()
        self.stale_job_recovery_service = stale_job_recovery_service or get_stale_job_recovery_service()

    def get_user_job(self, session: Session, *, user_id: UUID, job_id: UUID) -> IngestionJob | None:
        return session.scalar(
            select(IngestionJob).where(
                IngestionJob.id == job_id,
                IngestionJob.user_id == user_id,
            )
        )

    def build_job_read(self, session: Session, job: IngestionJob) -> LongRunningJobRead:
        return self.long_running_job_service.build_job_read(job, session=session)

    @staticmethod
    def compute_resume_from_page(job: IngestionJob) -> int | None:
        checkpoint = job.items_processed
        if checkpoint is None or checkpoint < 1:
            return None
        return max(1, checkpoint)

    def can_resume_job(self, session: Session, job: IngestionJob) -> bool:
        resume_from_page = self.compute_resume_from_page(job)
        if resume_from_page is None:
            return False

        if job.status is IngestionJobStatus.FAILED:
            pass
        elif job.status is IngestionJobStatus.RUNNING:
            is_stale, _ = self.stale_job_recovery_service.is_job_stale(
                session,
                job_kind=JobKind.INGESTION,
                job=job,
            )
            if not is_stale:
                return False
        else:
            return False

        existing_active_document_job = session.scalar(
            select(IngestionJob.id).where(
                IngestionJob.document_id == job.document_id,
                IngestionJob.user_id == job.user_id,
                IngestionJob.id != job.id,
                IngestionJob.status.in_([IngestionJobStatus.QUEUED, IngestionJobStatus.RUNNING]),
            )
        )
        return existing_active_document_job is None

    def create_retry_job(self, session: Session, *, user_id: UUID, failed_job_id: UUID) -> IngestionJob:
        job = self.get_user_job(session, user_id=user_id, job_id=failed_job_id)
        if job is None:
            raise IngestionRetryError(status_code=404, message="Job not found.")
        if job.status is not IngestionJobStatus.FAILED:
            raise IngestionRetryError(status_code=409, message="Only failed jobs can be retried.")
        if not job.can_retry:
            raise IngestionRetryError(status_code=409, message="This failed job cannot be retried.")

        document = session.get(Document, job.document_id)
        if document is None:
            raise IngestionRetryError(status_code=404, message="Document not found for this job.")

        existing_active_document_job = session.scalar(
            select(IngestionJob.id).where(
                IngestionJob.document_id == job.document_id,
                IngestionJob.user_id == job.user_id,
                IngestionJob.id != job.id,
                IngestionJob.status.in_([IngestionJobStatus.QUEUED, IngestionJobStatus.RUNNING]),
            )
        )
        if existing_active_document_job is not None:
            raise IngestionRetryError(status_code=409, message="An ingestion job is already running for this document.")

        self._ensure_source_file_exists(document)

        retry_job = IngestionJob(
            id=uuid4(),
            document_id=job.document_id,
            retry_of_job_id=job.id,
            user_id=job.user_id,
            status=IngestionJobStatus.QUEUED,
            step="queued",
            progress_percent=0,
            retry_count=job.retry_count + 1,
            can_retry=True,
            result_resource_type=JobResultResourceType.DOCUMENT,
            result_resource_id=str(job.document_id),
        )
        job.last_retried_at = datetime.now(timezone.utc)
        document.status = DocumentStatus.QUEUED
        session.add(retry_job)
        session.flush()
        self.job_progress_service.set_stage(
            session,
            job_kind=JobKind.INGESTION,
            job=retry_job,
            stage_code="queued",
            progress_percent=0,
        )
        session.commit()
        session.refresh(retry_job)
        return retry_job

    def create_resume_job(self, session: Session, *, user_id: UUID, source_job_id: UUID) -> IngestionJob:
        job = self.get_user_job(session, user_id=user_id, job_id=source_job_id)
        if job is None:
            raise IngestionRetryError(status_code=404, message="Job not found.")

        resume_from_page = self.compute_resume_from_page(job)
        if resume_from_page is None:
            raise IngestionRetryError(status_code=409, message="This job has no checkpoint to resume from.")

        if job.status is IngestionJobStatus.FAILED:
            pass
        elif job.status is IngestionJobStatus.RUNNING:
            is_stale, _ = self.stale_job_recovery_service.is_job_stale(
                session,
                job_kind=JobKind.INGESTION,
                job=job,
            )
            if not is_stale:
                raise IngestionRetryError(
                    status_code=409,
                    message="Only failed or stale jobs can be resumed.",
                )
        else:
            raise IngestionRetryError(status_code=409, message="Only failed or stale jobs can be resumed.")

        if not self.can_resume_job(session, job):
            raise IngestionRetryError(
                status_code=409,
                message="An ingestion job is already running for this document.",
            )

        document = session.get(Document, job.document_id)
        if document is None:
            raise IngestionRetryError(status_code=404, message="Document not found for this job.")

        self._ensure_source_file_exists(document)

        resume_job = IngestionJob(
            id=uuid4(),
            document_id=job.document_id,
            resume_of_job_id=job.id,
            resume_from_page=resume_from_page,
            user_id=job.user_id,
            status=IngestionJobStatus.QUEUED,
            step="queued",
            progress_percent=0,
            items_processed=resume_from_page - 1,
            items_total=job.items_total,
            retry_count=job.retry_count,
            can_retry=True,
            result_resource_type=JobResultResourceType.DOCUMENT,
            result_resource_id=str(job.document_id),
        )
        document.status = DocumentStatus.QUEUED
        session.add(resume_job)
        session.flush()
        self.job_progress_service.set_stage(
            session,
            job_kind=JobKind.INGESTION,
            job=resume_job,
            stage_code="queued",
            progress_percent=0,
            items_processed=resume_from_page - 1,
            items_total=job.items_total,
        )
        session.commit()
        session.refresh(resume_job)
        return resume_job

    def _ensure_source_file_exists(self, document: Document) -> None:
        try:
            self.storage_service.download_bytes(document.storage_bucket, document.storage_path)
        except Exception as exc:
            raise IngestionRetryError(
                status_code=409,
                message="The original uploaded file could not be found. Re-upload the document and try again.",
            ) from exc


def get_ingestion_job_service() -> IngestionJobService:
    return IngestionJobService()
