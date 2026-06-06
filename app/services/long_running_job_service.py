from __future__ import annotations

from typing import Any
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
from app.services.auth_service import get_supabase_admin_client
from app.services.stale_job_recovery_service import StaleJobRecoveryService, get_stale_job_recovery_service


def _value_from_object(value: Any, key: str) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


class LongRunningJobService:
    def __init__(self, *, stale_job_recovery_service: StaleJobRecoveryService | None = None) -> None:
        self.stale_job_recovery_service = stale_job_recovery_service or get_stale_job_recovery_service()

    def build_job_read(
        self,
        job: IngestionJob | ReferenceSourceImport | ReferenceMatchRun | MorphologyRun | DocumentNayiriLookupRun | DiscoveryBuildRun,
        *,
        session: Session | None = None,
    ) -> LongRunningJobRead:
        if isinstance(job, IngestionJob):
            payload = self._build_ingestion_job(session, job)
        elif isinstance(job, ReferenceSourceImport):
            payload = self._build_reference_import_job(session, job)
        elif isinstance(job, ReferenceMatchRun):
            payload = self._build_reference_matching_job(session, job)
        elif isinstance(job, MorphologyRun):
            payload = self._build_morphology_job(session, job)
        elif isinstance(job, DocumentNayiriLookupRun):
            payload = self._build_nayiri_lookup_job(session, job)
        elif isinstance(job, DiscoveryBuildRun):
            payload = self._build_discovery_build_job(session, job)
        else:
            raise TypeError(f"Unsupported job type: {type(job)!r}")

        if session is not None:
            return self._finalize_job_read(session, payload)
        return payload

    def get_user_job(
        self,
        session: Session,
        *,
        user_id: UUID,
        job_id: UUID,
        include_all_users: bool = False,
        include_owner_profile: bool = False,
    ) -> LongRunningJobRead | None:
        ingestion_job = session.get(IngestionJob, job_id)
        if ingestion_job is not None and (include_all_users or ingestion_job.user_id == user_id):
            return self._finalize_job_read(
                session,
                self._with_owner_profile(self._build_ingestion_job(session, ingestion_job), include_owner_profile),
            )

        reference_import = session.get(ReferenceSourceImport, job_id)
        if reference_import is not None and (include_all_users or reference_import.user_id == str(user_id)):
            return self._finalize_job_read(
                session,
                self._with_owner_profile(self._build_reference_import_job(session, reference_import), include_owner_profile),
            )

        reference_matching = session.get(ReferenceMatchRun, job_id)
        if reference_matching is not None and (include_all_users or reference_matching.user_id == str(user_id)):
            return self._finalize_job_read(
                session,
                self._with_owner_profile(
                    self._build_reference_matching_job(session, reference_matching),
                    include_owner_profile,
                ),
            )

        morphology_run = session.get(MorphologyRun, job_id)
        if morphology_run is not None and (include_all_users or morphology_run.user_id == str(user_id)):
            return self._finalize_job_read(
                session,
                self._with_owner_profile(self._build_morphology_job(session, morphology_run), include_owner_profile),
            )

        nayiri_lookup_run = session.get(DocumentNayiriLookupRun, job_id)
        if nayiri_lookup_run is not None and (include_all_users or nayiri_lookup_run.user_id == str(user_id)):
            return self._finalize_job_read(
                session,
                self._with_owner_profile(self._build_nayiri_lookup_job(session, nayiri_lookup_run), include_owner_profile),
            )

        discovery_build_run = session.get(DiscoveryBuildRun, job_id)
        if discovery_build_run is not None and (include_all_users or discovery_build_run.user_id == str(user_id)):
            return self._finalize_job_read(
                session,
                self._with_owner_profile(
                    self._build_discovery_build_job(session, discovery_build_run),
                    include_owner_profile,
                ),
            )
        return None

    def get_user_job_by_kind(
        self,
        session: Session,
        *,
        user_id: UUID,
        job_id: UUID,
        job_kind: JobKind,
        include_all_users: bool = False,
        include_owner_profile: bool = False,
    ) -> LongRunningJobRead | None:
        user_id_text = str(user_id)
        if job_kind is JobKind.INGESTION:
            job = session.get(IngestionJob, job_id)
            if job is not None and (include_all_users or job.user_id == user_id):
                return self._finalize_job_read(
                    session,
                    self._with_owner_profile(self._build_ingestion_job(session, job), include_owner_profile),
                )
            return None
        if job_kind is JobKind.REFERENCE_IMPORT:
            job = session.get(ReferenceSourceImport, job_id)
            if job is not None and (include_all_users or job.user_id == user_id_text):
                return self._finalize_job_read(
                    session,
                    self._with_owner_profile(self._build_reference_import_job(session, job), include_owner_profile),
                )
            return None
        if job_kind is JobKind.REFERENCE_MATCHING:
            job = session.get(ReferenceMatchRun, job_id)
            if job is not None and (include_all_users or job.user_id == user_id_text):
                return self._finalize_job_read(
                    session,
                    self._with_owner_profile(self._build_reference_matching_job(session, job), include_owner_profile),
                )
            return None
        if job_kind is JobKind.MORPHOLOGY:
            job = session.get(MorphologyRun, job_id)
            if job is not None and (include_all_users or job.user_id == user_id_text):
                return self._finalize_job_read(
                    session,
                    self._with_owner_profile(self._build_morphology_job(session, job), include_owner_profile),
                )
            return None
        if job_kind is JobKind.NAYIRI_TRUSTED_LOOKUP:
            job = session.get(DocumentNayiriLookupRun, job_id)
            if job is not None and (include_all_users or job.user_id == user_id_text):
                return self._finalize_job_read(
                    session,
                    self._with_owner_profile(self._build_nayiri_lookup_job(session, job), include_owner_profile),
                )
            return None
        if job_kind is JobKind.DISCOVERY_BUILD:
            job = session.get(DiscoveryBuildRun, job_id)
            if job is not None and (include_all_users or job.user_id == user_id_text):
                return self._finalize_job_read(
                    session,
                    self._with_owner_profile(self._build_discovery_build_job(session, job), include_owner_profile),
                )
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
        include_all_users: bool = False,
        include_owner_profile: bool = False,
    ) -> tuple[list[LongRunningJobRead], int]:
        jobs: list[LongRunningJobRead] = []
        total = 0

        if job_kind in {None, JobKind.INGESTION}:
            filters = [] if include_all_users else [IngestionJob.user_id == user_id]
            if status:
                filters.append(IngestionJob.status == status)
            total += session.scalar(select(func.count(IngestionJob.id)).where(*filters)) or 0
            for job in session.scalars(select(IngestionJob).where(*filters)):
                jobs.append(self._build_ingestion_job(session, job))

        if job_kind in {None, JobKind.REFERENCE_IMPORT}:
            filters = [] if include_all_users else [ReferenceSourceImport.user_id == str(user_id)]
            if status:
                filters.append(ReferenceSourceImport.status == status)
            total += session.scalar(select(func.count(ReferenceSourceImport.id)).where(*filters)) or 0
            for job in session.scalars(select(ReferenceSourceImport).where(*filters)):
                jobs.append(self._build_reference_import_job(session, job))

        if job_kind in {None, JobKind.REFERENCE_MATCHING}:
            filters = [] if include_all_users else [ReferenceMatchRun.user_id == str(user_id)]
            if status:
                filters.append(ReferenceMatchRun.status == status)
            total += session.scalar(select(func.count(ReferenceMatchRun.id)).where(*filters)) or 0
            for job in session.scalars(select(ReferenceMatchRun).where(*filters)):
                jobs.append(self._build_reference_matching_job(session, job))

        if job_kind in {None, JobKind.MORPHOLOGY}:
            filters = [] if include_all_users else [MorphologyRun.user_id == str(user_id)]
            if status:
                filters.append(MorphologyRun.status == status)
            total += session.scalar(select(func.count(MorphologyRun.id)).where(*filters)) or 0
            for job in session.scalars(select(MorphologyRun).where(*filters)):
                jobs.append(self._build_morphology_job(session, job))

        if job_kind in {None, JobKind.NAYIRI_TRUSTED_LOOKUP}:
            filters = [] if include_all_users else [DocumentNayiriLookupRun.user_id == str(user_id)]
            if status:
                filters.append(DocumentNayiriLookupRun.status == status)
            total += session.scalar(select(func.count(DocumentNayiriLookupRun.id)).where(*filters)) or 0
            for job in session.scalars(select(DocumentNayiriLookupRun).where(*filters)):
                jobs.append(self._build_nayiri_lookup_job(session, job))

        if job_kind in {None, JobKind.DISCOVERY_BUILD}:
            filters = [] if include_all_users else [DiscoveryBuildRun.user_id == str(user_id)]
            if status:
                filters.append(DiscoveryBuildRun.status == status)
            total += session.scalar(select(func.count(DiscoveryBuildRun.id)).where(*filters)) or 0
            for job in session.scalars(select(DiscoveryBuildRun).where(*filters)):
                jobs.append(self._build_discovery_build_job(session, job))

        jobs = [self._finalize_job_read(session, job) for job in jobs]
        jobs.sort(key=lambda item: (item.created_at, item.id), reverse=True)
        self._attach_owner_profiles(jobs, enabled=include_owner_profile)
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
        for job in ingestion_jobs:
            jobs.append(self._build_ingestion_job(session, job))

        reference_imports = session.scalars(
            select(ReferenceSourceImport)
            .where(
                ReferenceSourceImport.user_id == str(user_id),
                ReferenceSourceImport.status.in_(active_statuses),
            )
            .order_by(ReferenceSourceImport.created_at.desc(), ReferenceSourceImport.id.desc())
            .limit(per_kind_limit)
        )
        for job in reference_imports:
            jobs.append(self._build_reference_import_job(session, job))

        reference_matching = session.scalars(
            select(ReferenceMatchRun)
            .where(
                ReferenceMatchRun.user_id == str(user_id),
                ReferenceMatchRun.status.in_(active_statuses),
            )
            .order_by(ReferenceMatchRun.created_at.desc(), ReferenceMatchRun.id.desc())
            .limit(per_kind_limit)
        )
        for job in reference_matching:
            jobs.append(self._build_reference_matching_job(session, job))

        morphology_runs = session.scalars(
            select(MorphologyRun)
            .where(
                MorphologyRun.user_id == str(user_id),
                MorphologyRun.status.in_(active_statuses),
            )
            .order_by(MorphologyRun.created_at.desc(), MorphologyRun.id.desc())
            .limit(per_kind_limit)
        )
        for job in morphology_runs:
            jobs.append(self._build_morphology_job(session, job))

        nayiri_lookup_runs = session.scalars(
            select(DocumentNayiriLookupRun)
            .where(
                DocumentNayiriLookupRun.user_id == str(user_id),
                DocumentNayiriLookupRun.status.in_(active_statuses),
            )
            .order_by(DocumentNayiriLookupRun.created_at.desc(), DocumentNayiriLookupRun.id.desc())
            .limit(per_kind_limit)
        )
        for job in nayiri_lookup_runs:
            jobs.append(self._build_nayiri_lookup_job(session, job))

        discovery_build_runs = session.scalars(
            select(DiscoveryBuildRun)
            .where(
                DiscoveryBuildRun.user_id == str(user_id),
                DiscoveryBuildRun.status.in_(active_statuses),
            )
            .order_by(DiscoveryBuildRun.created_at.desc(), DiscoveryBuildRun.id.desc())
            .limit(per_kind_limit)
        )
        for job in discovery_build_runs:
            jobs.append(self._build_discovery_build_job(session, job))

        jobs = [self._finalize_job_read(session, job) for job in jobs]
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

    def _finalize_job_read(self, session: Session, job: LongRunningJobRead) -> LongRunningJobRead:
        return self.stale_job_recovery_service.enrich_job_read(session, job)

    def _with_owner_profile(self, job: LongRunningJobRead, enabled: bool) -> LongRunningJobRead:
        self._attach_owner_profiles([job], enabled=enabled)
        return job

    def _attach_owner_profiles(self, jobs: list[LongRunningJobRead], *, enabled: bool) -> None:
        if not enabled or not jobs:
            return

        profiles = {user_id: self._resolve_owner_profile(user_id) for user_id in {job.user_id for job in jobs}}
        for job in jobs:
            profile = profiles.get(job.user_id) or {}
            job.owner_email = profile.get("email")
            job.owner_display_name = profile.get("display_name") or job.owner_email or job.user_id

    @staticmethod
    def _resolve_owner_profile(user_id: str) -> dict[str, str | None]:
        try:
            response = get_supabase_admin_client().auth.admin.get_user_by_id(user_id)
        except Exception:
            return {"email": None, "display_name": user_id}

        payload = _value_from_object(response, "user") or _value_from_object(
            _value_from_object(response, "data"),
            "user",
        )
        email = _value_from_object(payload, "email")
        metadata = _value_from_object(payload, "user_metadata") or {}
        display_name = (
            _value_from_object(metadata, "full_name")
            or _value_from_object(metadata, "name")
            or _value_from_object(metadata, "user_name")
            or email
            or user_id
        )
        return {
            "email": str(email) if email else None,
            "display_name": str(display_name) if display_name else user_id,
        }

    @staticmethod
    def _build_ingestion_job(session: Session | None, job: IngestionJob) -> LongRunningJobRead:
        latest_retry_job_id: UUID | None = None
        latest_retry_job_status: str | None = None
        latest_resume_job_id: UUID | None = None
        latest_resume_job_status: str | None = None
        can_resume: bool | None = None
        resume_from_page: int | None = job.resume_from_page
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

            latest_resume_job = session.scalar(
                select(IngestionJob)
                .where(IngestionJob.resume_of_job_id == job.id)
                .order_by(IngestionJob.created_at.desc(), IngestionJob.id.desc())
                .limit(1)
            )
            if latest_resume_job is not None:
                latest_resume_job_id = latest_resume_job.id
                latest_resume_job_status = latest_resume_job.status.value

            from app.services.ingestion_job_service import get_ingestion_job_service

            ingestion_job_service = get_ingestion_job_service()
            can_resume = ingestion_job_service.can_resume_job(session, job)
            if can_resume:
                resume_from_page = ingestion_job_service.compute_resume_from_page(job)
        return LongRunningJobRead(
            id=job.id,
            job_kind=JobKind.INGESTION,
            user_id=str(job.user_id),
            status=job.status.value,
            can_retry=job.can_retry,
            can_resume=can_resume,
            resume_from_page=resume_from_page,
            resume_of_job_id=job.resume_of_job_id,
            latest_retry_job_id=latest_retry_job_id,
            latest_retry_job_status=latest_retry_job_status,
            latest_resume_job_id=latest_resume_job_id,
            latest_resume_job_status=latest_resume_job_status,
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
