from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    DiscoveryBuildRun,
    DocumentNayiriLookupRun,
    IngestionJob,
    JobKind,
    MorphologyRun,
    ReferenceMatchRun,
    ReferenceSourceImport,
)
from app.schemas.job import LongRunningJobListResponse, LongRunningJobRead


class LongRunningJobService:
    def build_job_read(
        self,
        job: IngestionJob | ReferenceSourceImport | ReferenceMatchRun | MorphologyRun | DocumentNayiriLookupRun | DiscoveryBuildRun,
        *,
        session: Session | None = None,
    ) -> LongRunningJobRead:
        if isinstance(job, IngestionJob):
            return self._build_ingestion_job(session, job)
        if isinstance(job, ReferenceSourceImport):
            return self._build_reference_import_job(session, job)
        if isinstance(job, ReferenceMatchRun):
            return self._build_reference_matching_job(session, job)
        if isinstance(job, MorphologyRun):
            return self._build_morphology_job(session, job)
        if isinstance(job, DocumentNayiriLookupRun):
            return self._build_nayiri_lookup_job(session, job)
        if isinstance(job, DiscoveryBuildRun):
            return self._build_discovery_build_job(session, job)
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

        morphology_run = session.get(MorphologyRun, job_id)
        if morphology_run is not None and morphology_run.user_id == str(user_id):
            return self._build_morphology_job(session, morphology_run)

        nayiri_lookup_run = session.get(DocumentNayiriLookupRun, job_id)
        if nayiri_lookup_run is not None and nayiri_lookup_run.user_id == str(user_id):
            return self._build_nayiri_lookup_job(session, nayiri_lookup_run)

        discovery_build_run = session.get(DiscoveryBuildRun, job_id)
        if discovery_build_run is not None and discovery_build_run.user_id == str(user_id):
            return self._build_discovery_build_job(session, discovery_build_run)
        return None

    def get_user_job_by_kind(
        self,
        session: Session,
        *,
        user_id: UUID,
        job_id: UUID,
        job_kind: JobKind,
    ) -> LongRunningJobRead | None:
        user_id_text = str(user_id)
        if job_kind is JobKind.INGESTION:
            job = session.get(IngestionJob, job_id)
            if job is not None and job.user_id == user_id:
                return self._build_ingestion_job(session, job)
            return None
        if job_kind is JobKind.REFERENCE_IMPORT:
            job = session.get(ReferenceSourceImport, job_id)
            if job is not None and job.user_id == user_id_text:
                return self._build_reference_import_job(session, job)
            return None
        if job_kind is JobKind.REFERENCE_MATCHING:
            job = session.get(ReferenceMatchRun, job_id)
            if job is not None and job.user_id == user_id_text:
                return self._build_reference_matching_job(session, job)
            return None
        if job_kind is JobKind.MORPHOLOGY:
            job = session.get(MorphologyRun, job_id)
            if job is not None and job.user_id == user_id_text:
                return self._build_morphology_job(session, job)
            return None
        if job_kind is JobKind.NAYIRI_TRUSTED_LOOKUP:
            job = session.get(DocumentNayiriLookupRun, job_id)
            if job is not None and job.user_id == user_id_text:
                return self._build_nayiri_lookup_job(session, job)
            return None
        if job_kind is JobKind.DISCOVERY_BUILD:
            job = session.get(DiscoveryBuildRun, job_id)
            if job is not None and job.user_id == user_id_text:
                return self._build_discovery_build_job(session, job)
            return None
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

        if job_kind in {None, JobKind.MORPHOLOGY}:
            filters = [MorphologyRun.user_id == str(user_id)]
            if status:
                filters.append(MorphologyRun.status == status)
            total += session.scalar(select(func.count(MorphologyRun.id)).where(*filters)) or 0
            jobs.extend(
                self._build_morphology_job(session, job)
                for job in session.scalars(select(MorphologyRun).where(*filters))
            )

        if job_kind in {None, JobKind.NAYIRI_TRUSTED_LOOKUP}:
            filters = [DocumentNayiriLookupRun.user_id == str(user_id)]
            if status:
                filters.append(DocumentNayiriLookupRun.status == status)
            total += session.scalar(select(func.count(DocumentNayiriLookupRun.id)).where(*filters)) or 0
            jobs.extend(
                self._build_nayiri_lookup_job(session, job)
                for job in session.scalars(select(DocumentNayiriLookupRun).where(*filters))
            )

        if job_kind in {None, JobKind.DISCOVERY_BUILD}:
            filters = [DiscoveryBuildRun.user_id == str(user_id)]
            if status:
                filters.append(DiscoveryBuildRun.status == status)
            total += session.scalar(select(func.count(DiscoveryBuildRun.id)).where(*filters)) or 0
            jobs.extend(
                self._build_discovery_build_job(session, job)
                for job in session.scalars(select(DiscoveryBuildRun).where(*filters))
            )

        jobs.sort(key=lambda item: (item.created_at, item.id), reverse=True)
        return jobs[offset:offset + limit], total

    def list_active_jobs(
        self,
        session: Session,
        *,
        user_id: UUID,
        limit: int = 50,
    ) -> list[LongRunningJobRead]:
        active_statuses = ("queued", "running")
        per_kind_limit = max(limit, 1)
        jobs: list[LongRunningJobRead] = []

        ingestion_jobs = session.scalars(
            select(IngestionJob)
            .where(
                IngestionJob.user_id == user_id,
                IngestionJob.status.in_(active_statuses),
            )
            .order_by(IngestionJob.created_at.desc(), IngestionJob.id.desc())
            .limit(per_kind_limit)
        )
        jobs.extend(self._build_ingestion_job(session, job) for job in ingestion_jobs)

        reference_imports = session.scalars(
            select(ReferenceSourceImport)
            .where(
                ReferenceSourceImport.user_id == str(user_id),
                ReferenceSourceImport.status.in_(active_statuses),
            )
            .order_by(ReferenceSourceImport.created_at.desc(), ReferenceSourceImport.id.desc())
            .limit(per_kind_limit)
        )
        jobs.extend(self._build_reference_import_job(session, job) for job in reference_imports)

        reference_matching = session.scalars(
            select(ReferenceMatchRun)
            .where(
                ReferenceMatchRun.user_id == str(user_id),
                ReferenceMatchRun.status.in_(active_statuses),
            )
            .order_by(ReferenceMatchRun.created_at.desc(), ReferenceMatchRun.id.desc())
            .limit(per_kind_limit)
        )
        jobs.extend(self._build_reference_matching_job(session, job) for job in reference_matching)

        morphology_runs = session.scalars(
            select(MorphologyRun)
            .where(
                MorphologyRun.user_id == str(user_id),
                MorphologyRun.status.in_(active_statuses),
            )
            .order_by(MorphologyRun.created_at.desc(), MorphologyRun.id.desc())
            .limit(per_kind_limit)
        )
        jobs.extend(self._build_morphology_job(session, job) for job in morphology_runs)

        nayiri_lookup_runs = session.scalars(
            select(DocumentNayiriLookupRun)
            .where(
                DocumentNayiriLookupRun.user_id == str(user_id),
                DocumentNayiriLookupRun.status.in_(active_statuses),
            )
            .order_by(DocumentNayiriLookupRun.created_at.desc(), DocumentNayiriLookupRun.id.desc())
            .limit(per_kind_limit)
        )
        jobs.extend(self._build_nayiri_lookup_job(session, job) for job in nayiri_lookup_runs)

        discovery_build_runs = session.scalars(
            select(DiscoveryBuildRun)
            .where(
                DiscoveryBuildRun.user_id == str(user_id),
                DiscoveryBuildRun.status.in_(active_statuses),
            )
            .order_by(DiscoveryBuildRun.created_at.desc(), DiscoveryBuildRun.id.desc())
            .limit(per_kind_limit)
        )
        jobs.extend(self._build_discovery_build_job(session, job) for job in discovery_build_runs)

        jobs.sort(key=lambda item: (item.created_at, item.id), reverse=True)
        return jobs[:limit]

    def count_active_jobs(self, session: Session, *, user_id: UUID) -> int:
        active_statuses = ("queued", "running")
        user_id_text = str(user_id)
        ingestion_count = (
            select(func.count(IngestionJob.id))
            .where(
                IngestionJob.user_id == user_id,
                IngestionJob.status.in_(active_statuses),
            )
            .scalar_subquery()
        )
        reference_import_count = (
            select(func.count(ReferenceSourceImport.id))
            .where(
                ReferenceSourceImport.user_id == user_id_text,
                ReferenceSourceImport.status.in_(active_statuses),
            )
            .scalar_subquery()
        )
        reference_match_count = (
            select(func.count(ReferenceMatchRun.id))
            .where(
                ReferenceMatchRun.user_id == user_id_text,
                ReferenceMatchRun.status.in_(active_statuses),
            )
            .scalar_subquery()
        )
        morphology_count = (
            select(func.count(MorphologyRun.id))
            .where(
                MorphologyRun.user_id == user_id_text,
                MorphologyRun.status.in_(active_statuses),
            )
            .scalar_subquery()
        )
        nayiri_lookup_count = (
            select(func.count(DocumentNayiriLookupRun.id))
            .where(
                DocumentNayiriLookupRun.user_id == user_id_text,
                DocumentNayiriLookupRun.status.in_(active_statuses),
            )
            .scalar_subquery()
        )
        discovery_build_count = (
            select(func.count(DiscoveryBuildRun.id))
            .where(
                DiscoveryBuildRun.user_id == user_id_text,
                DiscoveryBuildRun.status.in_(active_statuses),
            )
            .scalar_subquery()
        )
        total = session.scalar(
            select(
                func.coalesce(ingestion_count, 0)
                + func.coalesce(reference_import_count, 0)
                + func.coalesce(reference_match_count, 0)
                + func.coalesce(morphology_count, 0)
                + func.coalesce(nayiri_lookup_count, 0)
                + func.coalesce(discovery_build_count, 0)
            )
        )
        return int(total or 0)

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

    @staticmethod
    def _build_nayiri_lookup_job(
        session: Session | None,
        job: DocumentNayiriLookupRun,
    ) -> LongRunningJobRead:
        return LongRunningJobRead(
            id=job.id,
            job_kind=JobKind.NAYIRI_TRUSTED_LOOKUP,
            user_id=job.user_id,
            status=job.status.value,
            can_retry=job.can_retry,
            latest_retry_job_id=None,
            latest_retry_job_status=None,
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
    def _build_morphology_job(session: Session | None, job: MorphologyRun) -> LongRunningJobRead:
        return LongRunningJobRead(
            id=job.id,
            job_kind=JobKind.MORPHOLOGY,
            user_id=job.user_id,
            status=job.status.value,
            can_retry=job.can_retry,
            latest_retry_job_id=None,
            latest_retry_job_status=None,
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
    def _build_discovery_build_job(
        session: Session | None,
        job: DiscoveryBuildRun,
    ) -> LongRunningJobRead:
        return LongRunningJobRead(
            id=job.id,
            job_kind=JobKind.DISCOVERY_BUILD,
            user_id=job.user_id,
            status=job.status.value,
            can_retry=job.can_retry,
            latest_retry_job_id=None,
            latest_retry_job_status=None,
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
