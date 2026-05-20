from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from app.db.models import JobKind, ReferenceImportStatus, ReferenceSourceImport
from app.services.job_orchestrator import get_job_orchestrator
from app.schemas.job import RetryJobStartResponse
from app.services.document_service import DocumentService, get_document_service
from app.services.ingestion_error_service import get_ingestion_error_service
from app.services.ingestion_job_service import IngestionJobService, get_ingestion_job_service
from app.services.long_running_job_service import LongRunningJobService, get_long_running_job_service
from app.services.reference_import_service import ReferenceImportService, get_reference_import_service
from app.services.reference_matching_service import ReferenceMatchingService, get_reference_matching_service
from app.services.retry_errors import RetryStartError


class JobRetryService:
    def __init__(
        self,
        *,
        long_running_job_service: LongRunningJobService | None = None,
        ingestion_job_service: IngestionJobService | None = None,
        reference_import_service: ReferenceImportService | None = None,
        reference_matching_service: ReferenceMatchingService | None = None,
        document_service: DocumentService | None = None,
    ) -> None:
        self.long_running_job_service = long_running_job_service or get_long_running_job_service()
        self.ingestion_job_service = ingestion_job_service or get_ingestion_job_service()
        self.reference_import_service = reference_import_service or get_reference_import_service()
        self.reference_matching_service = reference_matching_service or get_reference_matching_service()
        self.document_service = document_service or get_document_service()
        self.job_orchestrator = get_job_orchestrator()

    def retry_job(self, session: Session, *, user_id: UUID, job_id: UUID) -> RetryJobStartResponse:
        job = self.long_running_job_service.get_user_job(session, user_id=user_id, job_id=job_id)
        if job is None:
            raise RetryStartError(status_code=404, message="Job not found.")

        if job.job_kind is JobKind.INGESTION:
            return self._retry_ingestion_job(session, user_id=user_id, job_id=job_id)
        if job.job_kind is JobKind.REFERENCE_IMPORT:
            return self._retry_reference_import_job(session, user_id=user_id, job_id=job_id)
        if job.job_kind is JobKind.REFERENCE_MATCHING:
            return self._retry_reference_matching_job(session, user_id=user_id, job_id=job_id)
        raise RetryStartError(status_code=409, message="This job type cannot be retried.")

    def _retry_ingestion_job(self, session: Session, *, user_id: UUID, job_id: UUID) -> RetryJobStartResponse:
        retry_job = self.ingestion_job_service.create_retry_job(
            session,
            user_id=user_id,
            failed_job_id=job_id,
        )
        try:
            self.job_orchestrator.enqueue(JobKind.INGESTION, retry_job.id)
            self.document_service.mark_document_queued(session, document_id=retry_job.document_id)
        except Exception as exc:
            failure_info = get_ingestion_error_service().map_exception(exc)
            self.document_service.mark_job_failed(
                session,
                document_id=retry_job.document_id,
                job_id=retry_job.id,
                failure_info=failure_info,
            )
            raise RetryStartError(
                status_code=500,
                message="Failed to enqueue retry job.",
            ) from exc

        retry_job = self.ingestion_job_service.get_user_job(session, user_id=user_id, job_id=retry_job.id)
        if retry_job is None:
            raise RetryStartError(status_code=500, message="Retry job not found.")
        return RetryJobStartResponse(
            message="Retry started",
            document_id=retry_job.document_id,
            job=self.ingestion_job_service.build_job_read(session, retry_job),
        )

    def _retry_reference_import_job(self, session: Session, *, user_id: UUID, job_id: UUID) -> RetryJobStartResponse:
        failed_import = session.get(ReferenceSourceImport, job_id)
        if failed_import is None or failed_import.user_id != str(user_id):
            raise RetryStartError(status_code=404, message="Job not found.")

        retry_import = self.reference_import_service.create_retry_import_run(
            session,
            user_id=user_id,
            source_id=failed_import.source_id,
            failed_import_id=job_id,
        )
        try:
            self.job_orchestrator.enqueue(JobKind.REFERENCE_IMPORT, retry_import.id)
        except Exception as exc:
            retry_import.status = ReferenceImportStatus.FAILED
            retry_import.error_code = "reference_import_enqueue_failed"
            retry_import.error_message = "Failed to enqueue reference import."
            retry_import.error_message_user = "The reference import could not be started."
            retry_import.next_steps = [
                "Try the import again.",
                "If it keeps failing, contact the administrator.",
            ]
            retry_import.can_retry = True
            session.commit()
            raise RetryStartError(
                status_code=500,
                message="Failed to enqueue reference import.",
            ) from exc

        return RetryJobStartResponse(
            message="Retry started",
            job=self.long_running_job_service.build_job_read(retry_import, session=session),
        )

    def _retry_reference_matching_job(self, session: Session, *, user_id: UUID, job_id: UUID) -> RetryJobStartResponse:
        retry_run = self.reference_matching_service.create_retry_run(
            session,
            user_id=user_id,
            failed_run_id=job_id,
        )
        try:
            self.job_orchestrator.enqueue(
                JobKind.REFERENCE_MATCHING,
                retry_run.id,
                kwargs={
                    "view": retry_run.requested_view,
                    "include_fuzzy": retry_run.include_fuzzy,
                },
            )
        except Exception as exc:
            self.reference_matching_service.mark_run_failed(
                session,
                run_id=retry_run.id,
                error_message="Failed to enqueue reference matching run.",
            )
            raise RetryStartError(
                status_code=500,
                message="Failed to enqueue reference matching run.",
            ) from exc

        return RetryJobStartResponse(
            message="Retry started",
            job=self.long_running_job_service.build_job_read(retry_run, session=session),
        )


def get_job_retry_service() -> JobRetryService:
    return JobRetryService()
