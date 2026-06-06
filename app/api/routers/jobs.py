from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db_session
from app.db.models import JobKind
from app.schemas.common import JobStageEventListResponse, JobStageEventRead
from app.schemas.job import LongRunningJobListResponse, LongRunningJobRead, ResumeJobStartResponse, RetryJobStartResponse
from app.services.auth_service import AuthenticatedUser
from app.services.job_progress_service import JobProgressService, get_job_progress_service
from app.services.job_resume_service import JobResumeService, get_job_resume_service
from app.services.job_retry_service import JobRetryService, get_job_retry_service
from app.services.job_stream_service import get_job_stream_service
from app.services.long_running_job_service import LongRunningJobService, get_long_running_job_service
from app.services.retry_errors import RetryStartError


router = APIRouter(prefix="/jobs")


@router.get("", response_model=LongRunningJobListResponse)
async def list_jobs(
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    job_kind: JobKind | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    long_running_job_service: LongRunningJobService = Depends(get_long_running_job_service),
) -> LongRunningJobListResponse:
    is_admin = current_user.role == "admin"
    items, total = long_running_job_service.list_jobs(
        session,
        user_id=current_user.user_id,
        limit=limit,
        offset=offset,
        job_kind=job_kind,
        status=status_filter,
        include_all_users=is_admin,
        include_owner_profile=is_admin,
    )
    return LongRunningJobListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/{job_id}", response_model=LongRunningJobRead)
async def get_job(
    job_id: UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    long_running_job_service: LongRunningJobService = Depends(get_long_running_job_service),
) -> LongRunningJobRead:
    is_admin = current_user.role == "admin"
    job = long_running_job_service.get_user_job(
        session,
        user_id=current_user.user_id,
        job_id=job_id,
        include_all_users=is_admin,
        include_owner_profile=is_admin,
    )
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    return job


@router.get("/{job_id}/events", response_model=JobStageEventListResponse)
async def list_job_events(
    job_id: UUID,
    job_kind: JobKind | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    long_running_job_service: LongRunningJobService = Depends(get_long_running_job_service),
    job_progress_service: JobProgressService = Depends(get_job_progress_service),
) -> JobStageEventListResponse:
    is_admin = current_user.role == "admin"
    if job_kind is not None:
        job = long_running_job_service.get_user_job_by_kind(
            session,
            user_id=current_user.user_id,
            job_id=job_id,
            job_kind=job_kind,
            include_all_users=is_admin,
            include_owner_profile=is_admin,
        )
    else:
        job = long_running_job_service.get_user_job(
            session,
            user_id=current_user.user_id,
            job_id=job_id,
            include_all_users=is_admin,
            include_owner_profile=is_admin,
        )
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    events = job_progress_service.list_events(
        session,
        job_kind=job.job_kind,
        job_id=job_id,
        user_id=job.user_id,
    )
    sliced_events = events[offset:offset + limit]
    return JobStageEventListResponse(
        items=[JobStageEventRead.model_validate(event) for event in sliced_events],
        total=len(events),
        limit=limit,
        offset=offset,
    )

@router.get("/{job_id}/stream")
async def stream_job_progress(
    job_id: UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    long_running_job_service: LongRunningJobService = Depends(get_long_running_job_service),
) -> StreamingResponse:
    is_admin = current_user.role == "admin"
    job = long_running_job_service.get_user_job(
        session,
        user_id=current_user.user_id,
        job_id=job_id,
        include_all_users=is_admin,
        include_owner_profile=is_admin,
    )
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    job_owner_id = UUID(job.user_id)

    stream_service = get_job_stream_service()

    async def event_generator():
        async for chunk in stream_service.stream(user_id=job_owner_id, job_id=job_id):
            yield chunk

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{job_id}/retry", response_model=RetryJobStartResponse)
async def retry_job(
    job_id: UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    long_running_job_service: LongRunningJobService = Depends(get_long_running_job_service),
    job_retry_service: JobRetryService = Depends(get_job_retry_service),
) -> RetryJobStartResponse:
    is_admin = current_user.role == "admin"
    job = long_running_job_service.get_user_job(
        session,
        user_id=current_user.user_id,
        job_id=job_id,
        include_all_users=is_admin,
    )
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")

    try:
        return job_retry_service.retry_job(
            session,
            user_id=UUID(job.user_id),
            job_id=job_id,
        )
    except RetryStartError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.post("/{job_id}/resume", response_model=ResumeJobStartResponse)
async def resume_job(
    job_id: UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    long_running_job_service: LongRunningJobService = Depends(get_long_running_job_service),
    job_resume_service: JobResumeService = Depends(get_job_resume_service),
) -> ResumeJobStartResponse:
    is_admin = current_user.role == "admin"
    job = long_running_job_service.get_user_job(
        session,
        user_id=current_user.user_id,
        job_id=job_id,
        include_all_users=is_admin,
    )
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    if job.job_kind is not JobKind.INGESTION:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Only ingestion jobs can be resumed.")

    try:
        return job_resume_service.resume_job(
            session,
            user_id=UUID(job.user_id),
            job_id=job_id,
        )
    except RetryStartError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
