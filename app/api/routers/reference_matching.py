from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db_session
from app.db.models import JobKind
from app.services.job_orchestrator import get_job_orchestrator
from app.db.models import JobKind
from app.schemas.common import JobStageEventListResponse, JobStageEventRead
from app.schemas.job import LongRunningJobRead
from app.schemas.reference import (
    ReferenceMatchRunCreateRequest,
    ReferenceMatchRunDetail,
    ReferenceMatchRunEntryResultDetail,
    ReferenceMatchRunEntryResultListResponse,
    ReferenceMatchRunEntryResultScopeFilter,
    ReferenceMatchRunListResponse,
    ReferenceMatchRunResultDetail,
    ReferenceMatchRunResultListResponse,
    ReferenceMatchRunResultTargetTypeFilter,
    ReferenceMatchingStartResponse,
    ReferenceStatusFilter,
)
from app.services.auth_service import AuthenticatedUser
from app.services.job_progress_service import JobProgressService, get_job_progress_service
from app.services.long_running_job_service import LongRunningJobService, get_long_running_job_service
from app.services.reference_matching_service import (
    ReferenceMatchingService,
    ReferenceSchemaNotReadyError,
    get_reference_matching_service,
)
from app.services.retry_errors import RetryStartError


router = APIRouter(prefix="/reference-matching")


@router.post("/runs", response_model=ReferenceMatchingStartResponse, status_code=status.HTTP_201_CREATED)
async def create_reference_matching_run(
    request: ReferenceMatchRunCreateRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    reference_matching_service: ReferenceMatchingService = Depends(get_reference_matching_service),
    long_running_job_service: LongRunningJobService = Depends(get_long_running_job_service),
) -> ReferenceMatchingStartResponse:
    try:
        run = reference_matching_service.create_run(
            session,
            user_id=current_user.user_id,
            request=request,
        )
    except ReferenceSchemaNotReadyError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    try:
        get_job_orchestrator().enqueue(
            JobKind.REFERENCE_MATCHING,
            run.id,
            kwargs={
                "view": request.view,
                "include_fuzzy": request.include_fuzzy,
            },
        )
    except Exception as exc:
        reference_matching_service.mark_run_failed(
            session,
            run_id=run.id,
            error_message="Failed to enqueue reference matching run.",
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to enqueue reference matching run.",
        ) from exc

    stored_run = reference_matching_service.get_user_run(session, user_id=current_user.user_id, run_id=run.id)
    if stored_run is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Reference matching run not found.")
    return ReferenceMatchingStartResponse(
        message="Reference matching started",
        run=ReferenceMatchRunDetail.model_validate(stored_run),
        job=long_running_job_service.build_job_read(stored_run, session=session),
    )


@router.post("/runs/{run_id}/retry", response_model=ReferenceMatchingStartResponse)
async def retry_reference_matching_run(
    run_id: UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    reference_matching_service: ReferenceMatchingService = Depends(get_reference_matching_service),
    long_running_job_service: LongRunningJobService = Depends(get_long_running_job_service),
) -> ReferenceMatchingStartResponse:
    try:
        retry_run = reference_matching_service.create_retry_run(
            session,
            user_id=current_user.user_id,
            failed_run_id=run_id,
        )
    except ReferenceSchemaNotReadyError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except RetryStartError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    try:
        get_job_orchestrator().enqueue(
            JobKind.REFERENCE_MATCHING,
            retry_run.id,
            kwargs={
                "view": retry_run.requested_view,
                "include_fuzzy": retry_run.include_fuzzy,
            },
        )
    except Exception as exc:
        reference_matching_service.mark_run_failed(
            session,
            run_id=retry_run.id,
            error_message="Failed to enqueue reference matching run.",
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to enqueue reference matching run.",
        ) from exc

    stored_run = reference_matching_service.get_user_run(session, user_id=current_user.user_id, run_id=retry_run.id)
    if stored_run is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Reference matching run not found.")
    return ReferenceMatchingStartResponse(
        message="Reference matching retry started",
        run=ReferenceMatchRunDetail.model_validate(stored_run),
        job=long_running_job_service.build_job_read(stored_run, session=session),
    )


@router.get("/runs", response_model=ReferenceMatchRunListResponse)
async def list_reference_matching_runs(
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    reference_matching_service: ReferenceMatchingService = Depends(get_reference_matching_service),
) -> ReferenceMatchRunListResponse:
    try:
        items, total = reference_matching_service.list_runs(
            session,
            user_id=current_user.user_id,
            limit=limit,
            offset=offset,
        )
    except ReferenceSchemaNotReadyError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return ReferenceMatchRunListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/runs/{run_id}", response_model=ReferenceMatchRunDetail)
async def get_reference_matching_run(
    run_id: UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    reference_matching_service: ReferenceMatchingService = Depends(get_reference_matching_service),
) -> ReferenceMatchRunDetail:
    try:
        run = reference_matching_service.get_run_detail(
            session,
            user_id=current_user.user_id,
            run_id=run_id,
        )
    except ReferenceSchemaNotReadyError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reference matching run not found.")
    return run


@router.get("/runs/{run_id}/results", response_model=ReferenceMatchRunEntryResultListResponse)
async def list_reference_matching_run_results(
    run_id: UUID,
    match_status: ReferenceStatusFilter = Query(default=ReferenceStatusFilter.ALL),
    target_scope: ReferenceMatchRunEntryResultScopeFilter = Query(
        default=ReferenceMatchRunEntryResultScopeFilter.ANY
    ),
    search: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    reference_matching_service: ReferenceMatchingService = Depends(get_reference_matching_service),
) -> ReferenceMatchRunEntryResultListResponse:
    try:
        run = reference_matching_service.get_user_run(
            session,
            user_id=current_user.user_id,
            run_id=run_id,
        )
        if run is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reference matching run not found.")
        items, total = reference_matching_service.list_run_reference_entry_results(
            session,
            user_id=current_user.user_id,
            run_id=run_id,
            search=search,
            match_status=match_status,
            target_scope=target_scope,
            limit=limit,
            offset=offset,
        )
    except ReferenceSchemaNotReadyError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return ReferenceMatchRunEntryResultListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/runs/{run_id}/results/{result_id}", response_model=ReferenceMatchRunEntryResultDetail)
async def get_reference_matching_run_result(
    run_id: UUID,
    result_id: UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    reference_matching_service: ReferenceMatchingService = Depends(get_reference_matching_service),
) -> ReferenceMatchRunEntryResultDetail:
    try:
        run = reference_matching_service.get_user_run(
            session,
            user_id=current_user.user_id,
            run_id=run_id,
        )
        if run is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reference matching run not found.")
        result = reference_matching_service.get_run_reference_entry_result_detail(
            session,
            user_id=current_user.user_id,
            run_id=run_id,
            result_id=result_id,
        )
    except ReferenceSchemaNotReadyError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reference matching run result not found.")
    return result


@router.get("/runs/{run_id}/target-results", response_model=ReferenceMatchRunResultListResponse)
async def list_reference_matching_run_target_results(
    run_id: UUID,
    match_status: ReferenceStatusFilter = Query(default=ReferenceStatusFilter.ALL),
    target_type: ReferenceMatchRunResultTargetTypeFilter = Query(
        default=ReferenceMatchRunResultTargetTypeFilter.ALL
    ),
    search: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    reference_matching_service: ReferenceMatchingService = Depends(get_reference_matching_service),
) -> ReferenceMatchRunResultListResponse:
    try:
        run = reference_matching_service.get_user_run(
            session,
            user_id=current_user.user_id,
            run_id=run_id,
        )
        if run is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reference matching run not found.")
        items, total = reference_matching_service.list_run_results(
            session,
            user_id=current_user.user_id,
            run_id=run_id,
            match_status=match_status,
            target_type=target_type,
            search=search,
            limit=limit,
            offset=offset,
        )
    except ReferenceSchemaNotReadyError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return ReferenceMatchRunResultListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/runs/{run_id}/target-results/{result_id}", response_model=ReferenceMatchRunResultDetail)
async def get_reference_matching_run_target_result(
    run_id: UUID,
    result_id: UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    reference_matching_service: ReferenceMatchingService = Depends(get_reference_matching_service),
) -> ReferenceMatchRunResultDetail:
    try:
        result = reference_matching_service.get_run_result_detail(
            session,
            user_id=current_user.user_id,
            run_id=run_id,
            result_id=result_id,
        )
    except ReferenceSchemaNotReadyError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reference matching run result not found.")
    return result


@router.get("/runs/{run_id}/events", response_model=JobStageEventListResponse)
async def list_reference_matching_run_events(
    run_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    reference_matching_service: ReferenceMatchingService = Depends(get_reference_matching_service),
    job_progress_service: JobProgressService = Depends(get_job_progress_service),
) -> JobStageEventListResponse:
    try:
        run = reference_matching_service.get_user_run(
            session,
            user_id=current_user.user_id,
            run_id=run_id,
        )
    except ReferenceSchemaNotReadyError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reference matching run not found.")
    events = job_progress_service.list_events(
        session,
        job_kind=JobKind.REFERENCE_MATCHING,
        job_id=run_id,
        user_id=current_user.user_id,
    )
    sliced_events = events[offset:offset + limit]
    return JobStageEventListResponse(
        items=[JobStageEventRead.model_validate(event) for event in sliced_events],
        total=len(events),
        limit=limit,
        offset=offset,
    )
