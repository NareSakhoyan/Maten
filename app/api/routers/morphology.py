from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db_session
from app.db.models import JobKind
from app.services.job_orchestrator import get_job_orchestrator
from app.schemas.morphology import MorphologyRunCreateRequest, MorphologyRunRead, MorphologyRunStartResponse
from app.services.auth_service import AuthenticatedUser
from app.services.long_running_job_service import LongRunningJobService, get_long_running_job_service
from app.services.morphology.morphology_service import MorphologyService, get_morphology_service


router = APIRouter(prefix="/morphology")


def start_morphology_run_or_raise(
    *,
    session: Session,
    current_user: AuthenticatedUser,
    request: MorphologyRunCreateRequest,
    morphology_service: MorphologyService,
    long_running_job_service: LongRunningJobService,
) -> MorphologyRunStartResponse:
    try:
        run = morphology_service.create_run(
            session,
            user_id=current_user.user_id,
            request=request,
        )
        try:
            get_job_orchestrator().enqueue(JobKind.MORPHOLOGY, run.id)
        except Exception as exc:
            run = morphology_service.mark_run_failed(
                session,
                run_id=run.id,
                error_message="Failed to enqueue morphology run.",
                error_message_user="The morphology run could not be started.",
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to enqueue morphology run.",
            ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    job = long_running_job_service.build_job_read(run, session=session)
    return MorphologyRunStartResponse(
        message="Morphology run started",
        run=morphology_service.build_run_read(run),
        job=job,
    )


@router.post("/runs", response_model=MorphologyRunStartResponse, status_code=status.HTTP_201_CREATED)
async def create_morphology_run(
    request: MorphologyRunCreateRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    morphology_service: MorphologyService = Depends(get_morphology_service),
    long_running_job_service: LongRunningJobService = Depends(get_long_running_job_service),
) -> MorphologyRunStartResponse:
    return start_morphology_run_or_raise(
        session=session,
        current_user=current_user,
        request=request,
        morphology_service=morphology_service,
        long_running_job_service=long_running_job_service,
    )


@router.get("/runs/{run_id}", response_model=MorphologyRunRead)
async def get_morphology_run(
    run_id: UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    morphology_service: MorphologyService = Depends(get_morphology_service),
) -> MorphologyRunRead:
    run = morphology_service.get_user_run(session, user_id=current_user.user_id, run_id=run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Morphology run not found.")
    return morphology_service.build_run_read(run)
