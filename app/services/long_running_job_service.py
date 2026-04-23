from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import IngestionJob, JobKind, ReferenceMatchRun, ReferenceSourceImport
from app.schemas.job import LongRunningJobListResponse, LongRunningJobRead


class LongRunningJobService:
    def build_job_read(
        self,
        job: IngestionJob | ReferenceSourceImport | ReferenceMatchRun,
        *,
        session: Session | None = None,
    ) -> LongRunningJobRead:
        if isinstance(job, IngestionJob):
            return self._build_ingestion_job(session, job)
        if isinstance(job, ReferenceSourceImport):
            return self._build_reference_import_job(session, job)
        if isinstance(job, ReferenceMatchRun):
            return self._build_reference_matching_job(session, job)
        raise TypeError(f"Unsupported job type: {type(job)!r}")

    def get_user_job(self, session: Session, *, user_id: UUID, job_id: UUID) -> LongRunningJobRead | None:
        ingestion_job = session.get(IngestionJob, job_id)
        if ingestion_job is not None and ingestion_job.user_id == user_id:
            return self._build_ingestion_job(session, ingestion_job)

        reference_import = session.get(ReferenceSourceImport, job_id)
        if reference_import is not None and reference_import.user_id == str(user_id):
            return self._build_reference_import_job(session, reference_import)

        reference_matching = session.get(ReferenceMatchRun, job_id)
        if reference_matching is not None and reference_matching.user_id == str(user_id):
            return self._build_reference_matching_job(session, reference_matching)
        return None

    def list_jobs(
        self,
        session: Session,
        *,
        user_id: UUID,
        limit: int,
        offset: int,
        job_kind: JobKind | None = None,
        status: str | None = None,
    ) -> tuple[list[LongRunningJobRead], int]:
        jobs: list[LongRunningJobRead] = []
        total = 0

        if job_kind in {None, JobKind.INGESTION}:
            filters = [IngestionJob.user_id == user_id]
            if status:
                filters.append(IngestionJob.status == status)
            total += session.scalar(select(func.count(IngestionJob.id)).where(*filters)) or 0
            jobs.extend(
                self._build_ingestion_job(session, job)
                for job in session.scalars(select(IngestionJob).where(*filters))
            )

        if job_kind in {None, JobKind.REFERENCE_IMPORT}:
            filters = [ReferenceSourceImport.user_id == str(user_id)]
            if status:
                filters.append(ReferenceSourceImport.status == status)
            total += session.scalar(select(func.count(ReferenceSourceImport.id)).where(*filters)) or 0
            jobs.extend(
                self._build_reference_import_job(session, job)
                for job in session.scalars(select(ReferenceSourceImport).where(*filters))
            )

        if job_kind in {None, JobKind.REFERENCE_MATCHING}:
            filters = [ReferenceMatchRun.user_id == str(user_id)]
            if status:
                filters.append(ReferenceMatchRun.status == status)
            total += session.scalar(select(func.count(ReferenceMatchRun.id)).where(*filters)) or 0
            jobs.extend(
                self._build_reference_matching_job(session, job)
                for job in session.scalars(select(ReferenceMatchRun).where(*filters))
            )

        jobs.sort(key=lambda item: (item.created_at, item.id), reverse=True)
        return jobs[offset:offset + limit], total

    @staticmethod
    def _build_ingestion_job(session: Session | None, job: IngestionJob) -> LongRunningJobRead:
        latest_retry_job_id: UUID | None = None
        latest_retry_job_status: str | None = None
        if session is not None:
            latest_retry_job = session.scalar(
                select(IngestionJob)
                .where(IngestionJob.retry_of_job_id == job.id)
                .order_by(IngestionJob.created_at.desc(), IngestionJob.id.desc())
                .limit(1)
            )
            if latest_retry_job is not None:
                latest_retry_job_id = latest_retry_job.id
                latest_retry_job_status = latest_retry_job.status.value
        return LongRunningJobRead(
            id=job.id,
            job_kind=JobKind.INGESTION,
            user_id=str(job.user_id),
            status=job.status.value,
            can_retry=job.can_retry,
            latest_retry_job_id=latest_retry_job_id,
            latest_retry_job_status=latest_retry_job_status,
            current_stage_code=job.current_stage_code,
            current_stage_label=job.current_stage_label,
            stage_message_user=job.stage_message_user,
            progress_percent=job.progress_percent,
            items_processed=job.items_processed,
            items_total=job.items_total,
            error_code=job.error_code,
            error_message_user=job.error_message_user or job.error_message,
            next_steps=job.next_steps,
            result_resource_type=job.result_resource_type,
            result_resource_id=job.result_resource_id,
            started_at=job.started_at,
            finished_at=job.finished_at,
            created_at=job.created_at,
            updated_at=job.updated_at,
        )

    @staticmethod
    def _build_reference_import_job(session: Session | None, job: ReferenceSourceImport) -> LongRunningJobRead:
        latest_retry_job_id: UUID | None = None
        latest_retry_job_status: str | None = None
        if session is not None:
            latest_retry_job = session.scalar(
                select(ReferenceSourceImport)
                .where(ReferenceSourceImport.retry_of_job_id == job.id)
                .order_by(
                    ReferenceSourceImport.created_at.desc(),
                    ReferenceSourceImport.updated_at.desc(),
                    ReferenceSourceImport.retry_count.desc(),
                    ReferenceSourceImport.id.desc(),
                )
                .limit(1)
            )
            if latest_retry_job is not None:
                latest_retry_job_id = latest_retry_job.id
                latest_retry_job_status = latest_retry_job.status.value
        return LongRunningJobRead(
            id=job.id,
            job_kind=JobKind.REFERENCE_IMPORT,
            user_id=job.user_id,
            status=job.status.value,
            can_retry=job.can_retry,
            latest_retry_job_id=latest_retry_job_id,
            latest_retry_job_status=latest_retry_job_status,
            current_stage_code=job.current_stage_code,
            current_stage_label=job.current_stage_label,
            stage_message_user=job.stage_message_user,
            progress_percent=job.progress_percent,
            items_processed=job.items_processed,
            items_total=job.items_total,
            error_code=job.error_code,
            error_message_user=job.error_message_user or job.error_message,
            next_steps=job.next_steps,
            result_resource_type=job.result_resource_type,
            result_resource_id=job.result_resource_id,
            started_at=job.started_at,
            finished_at=job.finished_at,
            created_at=job.created_at,
            updated_at=job.updated_at,
        )

    @staticmethod
    def _build_reference_matching_job(session: Session | None, job: ReferenceMatchRun) -> LongRunningJobRead:
        latest_retry_job_id: UUID | None = None
        latest_retry_job_status: str | None = None
        if session is not None:
            latest_retry_job = session.scalar(
                select(ReferenceMatchRun)
                .where(ReferenceMatchRun.retry_of_job_id == job.id)
                .order_by(
                    ReferenceMatchRun.created_at.desc(),
                    ReferenceMatchRun.updated_at.desc(),
                    ReferenceMatchRun.retry_count.desc(),
                    ReferenceMatchRun.id.desc(),
                )
                .limit(1)
            )
            if latest_retry_job is not None:
                latest_retry_job_id = latest_retry_job.id
                latest_retry_job_status = latest_retry_job.status.value
        return LongRunningJobRead(
            id=job.id,
            job_kind=JobKind.REFERENCE_MATCHING,
            user_id=job.user_id,
            status=job.status.value,
            can_retry=job.can_retry,
            latest_retry_job_id=latest_retry_job_id,
            latest_retry_job_status=latest_retry_job_status,
            current_stage_code=job.current_stage_code,
            current_stage_label=job.current_stage_label,
            stage_message_user=job.stage_message_user,
            progress_percent=job.progress_percent,
            items_processed=job.items_processed,
            items_total=job.items_total,
            error_code=job.error_code,
            error_message_user=job.error_message_user or job.error_message,
            next_steps=job.next_steps,
            result_resource_type=job.result_resource_type,
            result_resource_id=job.result_resource_id,
            started_at=job.started_at,
            finished_at=job.finished_at,
            created_at=job.created_at,
            updated_at=job.updated_at,
        )


def get_long_running_job_service() -> LongRunningJobService:
    return LongRunningJobService()
