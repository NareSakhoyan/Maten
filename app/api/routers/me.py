from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db_session
from app.schemas.workflow import ReviewQueueListResponse
from app.services.auth_service import AuthenticatedUser
from app.services.document_workflow_service import DocumentWorkflowService, get_document_workflow_service
from app.services.job_stream_service import get_job_stream_service
from app.services.long_running_job_service import LongRunningJobService, get_long_running_job_service


router = APIRouter(prefix="/me")


@router.get("/review-queue", response_model=ReviewQueueListResponse)
async def list_review_queue(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    workflow_service: DocumentWorkflowService = Depends(get_document_workflow_service),
) -> ReviewQueueListResponse:
    items, total = workflow_service.list_review_queue(
        session,
        user_id=current_user.user_id,
        limit=limit,
        offset=offset,
    )
    return ReviewQueueListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/active-jobs/count")
async def count_active_jobs(
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    long_running_job_service: LongRunningJobService = Depends(get_long_running_job_service),
) -> dict[str, int]:
    return {
        "count": long_running_job_service.count_active_jobs(session, user_id=current_user.user_id),
    }


@router.get("/active-jobs/stream")
async def stream_active_jobs(
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> StreamingResponse:
    stream_service = get_job_stream_service()

    async def event_generator():
        async for chunk in stream_service.stream_active_jobs(user_id=current_user.user_id):
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
