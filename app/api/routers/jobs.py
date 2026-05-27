from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db_session
from app.db.models import JobKind
from app.schemas.common import JobStageEventListResponse, JobStageEventRead
from app.schemas.job import LongRunningJobListResponse, LongRunningJobRead, RetryJobStartResponse
from app.services.auth_service import AuthenticatedUser
from app.services.job_progress_service import JobProgressService, get_job_progress_service
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
    items, total = long_running_job_service.list_jobs(
        session,
        user_id=current_user.user_id,
        limit=limit,
        offset=offset,
        job_kind=job_kind,
        status=status_filter,
    )
    return LongRunningJobListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/{job_id}", response_model=LongRunningJobRead)
async def get_job(
    job_id: UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    long_running_job_service: LongRunningJobService = Depends(get_long_running_job_service),
) -> LongRunningJobRead:
    job = long_running_job_service.get_user_job(session, user_id=current_user.user_id, job_id=job_id)
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
    if job_kind is not None:
        job = long_running_job_service.get_user_job_by_kind(
            session,
            user_id=current_user.user_id,
            job_id=job_id,
            job_kind=job_kind,
        )
    else:
        job = long_running_job_service.get_user_job(
            session,
            user_id=current_user.user_id,
            job_id=job_id,
        )
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    events = job_progress_service.list_events(
        session,
        job_kind=job.job_kind,
        job_id=job_id,
        user_id=current_user.user_id,
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
    job = long_running_job_service.get_user_job(session, user_id=current_user.user_id, job_id=job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")

    stream_service = get_job_stream_service()

    async def event_generator():
        async for chunk in stream_service.stream(user_id=current_user.user_id, job_id=job_id):
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
    job_retry_service: JobRetryService = Depends(get_job_retry_service),
) -> RetryJobStartResponse:
    try:
        return job_retry_service.retry_job(
            session,
            user_id=current_user.user_id,
            job_id=job_id,
        )
    except RetryStartError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
