from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db_session
from app.core.celery_app import celery_app
from app.db.models import JobKind, ReferenceImportStatus
from app.schemas.common import JobStageEventListResponse, JobStageEventRead
from app.schemas.reference import (
    ReferenceImportListResponse,
    ReferenceImportResponse,
    ReferenceImportStartResponse,
    ReferenceSourceEntryListResponse,
    ReferenceStatusFilter,
    ReferenceSourceCreateRequest,
    ReferenceSourceDetail,
    ReferenceSourceSummary,
)
from app.schemas.word import ReferenceSourceWordCandidateListResponse, ReferenceSourceWordCandidateSourceSummary
from app.services.auth_service import AuthenticatedUser
from app.services.job_progress_service import JobProgressService, get_job_progress_service
from app.services.long_running_job_service import LongRunningJobService, get_long_running_job_service
from app.services.reference_import_service import ReferenceImportService, get_reference_import_service
from app.services.reference_source_service import (
    ReferenceSourceSchemaNotReadyError,
    ReferenceSourceService,
    get_reference_source_service,
)
from app.services.reference_matching_service import ReferenceMatchingService, get_reference_matching_service
from app.services.retry_errors import RetryStartError
from app.services.source_word_review_service import SourceWordReviewService, get_source_word_review_service


router = APIRouter(prefix="/reference-sources")


@router.post("", response_model=ReferenceSourceDetail, status_code=status.HTTP_201_CREATED)
async def create_reference_source(
    request: ReferenceSourceCreateRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    reference_source_service: ReferenceSourceService = Depends(get_reference_source_service),
) -> ReferenceSourceDetail:
    try:
        return reference_source_service.create_source(
            session,
            user_id=current_user.user_id,
            request=request,
        )
    except ReferenceSourceSchemaNotReadyError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("", response_model=list[ReferenceSourceSummary])
async def list_reference_sources(
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    reference_source_service: ReferenceSourceService = Depends(get_reference_source_service),
) -> list[ReferenceSourceSummary]:
    try:
        return reference_source_service.list_sources(session, user_id=current_user.user_id)
    except ReferenceSourceSchemaNotReadyError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.get("/{source_id}", response_model=ReferenceSourceDetail)
async def get_reference_source(
    source_id: UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    reference_source_service: ReferenceSourceService = Depends(get_reference_source_service),
) -> ReferenceSourceDetail:
    try:
        source = reference_source_service.get_source_detail(
            session,
            user_id=current_user.user_id,
            source_id=source_id,
        )
    except ReferenceSourceSchemaNotReadyError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reference source not found.")
    return source


@router.post("/{source_id}/import", response_model=ReferenceImportStartResponse)
async def import_reference_source_entries(
    source_id: UUID,
    file: UploadFile = File(...),
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    reference_source_service: ReferenceSourceService = Depends(get_reference_source_service),
    reference_import_service: ReferenceImportService = Depends(get_reference_import_service),
    long_running_job_service: LongRunningJobService = Depends(get_long_running_job_service),
) -> ReferenceImportStartResponse:
    try:
        source = reference_source_service.get_user_source(
            session,
            user_id=current_user.user_id,
            source_id=source_id,
        )
    except ReferenceSourceSchemaNotReadyError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reference source not found.")
    if not file.filename:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Import file must include a filename.")

    try:
        content = await file.read()
        if not content:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Import file is empty.")
        import_run = reference_import_service.create_import_run(
            session,
            source=source,
            filename=file.filename,
            mime_type=file.content_type,
            file_size_bytes=len(content),
        )
        reference_import_service.store_import_file(
            session,
            source=source,
            content=content,
            import_run=import_run,
            content_type=file.content_type,
        )
        session.commit()
        session.refresh(import_run)
        refreshed_source = reference_source_service.get_source_detail(
            session,
            user_id=current_user.user_id,
            source_id=source_id,
        )
        if refreshed_source is None:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Reference source not found.")
        try:
            celery_app.send_task(
                "app.workers.tasks.process_reference_source_import",
                args=[str(import_run.id)],
                task_id=str(import_run.id),
            )
        except Exception as exc:
            import_run.status = ReferenceImportStatus.FAILED
            import_run.error_code = "reference_import_enqueue_failed"
            import_run.error_message = "Failed to enqueue reference import."
            import_run.error_message_user = "The reference import could not be started."
            import_run.next_steps = [
                "Try the import again.",
                "If it keeps failing, contact the administrator.",
            ]
            session.commit()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to enqueue reference import.",
            ) from exc
        return ReferenceImportStartResponse(
            message="Reference import started",
            source=refreshed_source,
            job=long_running_job_service.build_job_read(import_run, session=session),
            import_run=reference_import_service.build_import_response(source, import_run),
        )
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Reference import files must be UTF-8.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/{source_id}/imports", response_model=ReferenceImportListResponse)
async def list_reference_source_imports(
    source_id: UUID,
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    reference_source_service: ReferenceSourceService = Depends(get_reference_source_service),
    reference_import_service: ReferenceImportService = Depends(get_reference_import_service),
) -> ReferenceImportListResponse:
    try:
        source = reference_source_service.get_user_source(
            session,
            user_id=current_user.user_id,
            source_id=source_id,
        )
    except ReferenceSourceSchemaNotReadyError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reference source not found.")
    items, total = reference_import_service.list_imports(
        session,
        user_id=current_user.user_id,
        source_id=source_id,
        limit=limit,
        offset=offset,
    )
    return ReferenceImportListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/{source_id}/imports/{import_id}", response_model=ReferenceImportResponse)
async def get_reference_source_import(
    source_id: UUID,
    import_id: UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    reference_source_service: ReferenceSourceService = Depends(get_reference_source_service),
    reference_import_service: ReferenceImportService = Depends(get_reference_import_service),
) -> ReferenceImportResponse:
    try:
        source = reference_source_service.get_user_source(
            session,
            user_id=current_user.user_id,
            source_id=source_id,
        )
    except ReferenceSourceSchemaNotReadyError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reference source not found.")
    import_run = reference_import_service.get_user_import(
        session,
        user_id=current_user.user_id,
        source_id=source_id,
        import_id=import_id,
    )
    if import_run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reference import not found.")
    return reference_import_service.build_import_response(source, import_run)


@router.post("/{source_id}/imports/{import_id}/retry", response_model=ReferenceImportStartResponse)
async def retry_reference_source_import(
    source_id: UUID,
    import_id: UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    reference_source_service: ReferenceSourceService = Depends(get_reference_source_service),
    reference_import_service: ReferenceImportService = Depends(get_reference_import_service),
    long_running_job_service: LongRunningJobService = Depends(get_long_running_job_service),
) -> ReferenceImportStartResponse:
    try:
        source = reference_source_service.get_user_source(
            session,
            user_id=current_user.user_id,
            source_id=source_id,
        )
    except ReferenceSourceSchemaNotReadyError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reference source not found.")

    try:
        retry_import = reference_import_service.create_retry_import_run(
            session,
            user_id=current_user.user_id,
            source_id=source_id,
            failed_import_id=import_id,
        )
    except RetryStartError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    try:
        celery_app.send_task(
            "app.workers.tasks.process_reference_source_import",
            args=[str(retry_import.id)],
            task_id=str(retry_import.id),
        )
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
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to enqueue reference import.",
        ) from exc

    refreshed_source = reference_source_service.get_source_detail(
        session,
        user_id=current_user.user_id,
        source_id=source_id,
    )
    if refreshed_source is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Reference source not found.")
    return ReferenceImportStartResponse(
        message="Reference import retry started",
        source=refreshed_source,
        job=long_running_job_service.build_job_read(retry_import, session=session),
        import_run=reference_import_service.build_import_response(source, retry_import),
    )


@router.get("/{source_id}/imports/{import_id}/events", response_model=JobStageEventListResponse)
async def list_reference_source_import_events(
    source_id: UUID,
    import_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    reference_source_service: ReferenceSourceService = Depends(get_reference_source_service),
    reference_import_service: ReferenceImportService = Depends(get_reference_import_service),
    job_progress_service: JobProgressService = Depends(get_job_progress_service),
) -> JobStageEventListResponse:
    try:
        source = reference_source_service.get_user_source(
            session,
            user_id=current_user.user_id,
            source_id=source_id,
        )
    except ReferenceSourceSchemaNotReadyError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reference source not found.")
    import_run = reference_import_service.get_user_import(
        session,
        user_id=current_user.user_id,
        source_id=source_id,
        import_id=import_id,
    )
    if import_run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reference import not found.")
    events = job_progress_service.list_events(
        session,
        job_kind=JobKind.REFERENCE_IMPORT,
        job_id=import_id,
        user_id=current_user.user_id,
    )
    sliced_events = events[offset:offset + limit]
    return JobStageEventListResponse(
        items=[JobStageEventRead.model_validate(event) for event in sliced_events],
        total=len(events),
        limit=limit,
        offset=offset,
    )


@router.get("/{source_id}/word-candidates", response_model=ReferenceSourceWordCandidateListResponse)
async def list_reference_source_word_candidates(
    source_id: UUID,
    search: str | None = Query(default=None),
    reference_status: ReferenceStatusFilter = Query(default=ReferenceStatusFilter.ALL),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    reference_source_service: ReferenceSourceService = Depends(get_reference_source_service),
    source_word_review_service: SourceWordReviewService = Depends(get_source_word_review_service),
) -> ReferenceSourceWordCandidateListResponse:
    try:
        source = reference_source_service.get_user_source(
            session,
            user_id=current_user.user_id,
            source_id=source_id,
        )
    except ReferenceSourceSchemaNotReadyError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reference source not found.")
    source_detail = reference_source_service.get_source_detail(
        session,
        user_id=current_user.user_id,
        source_id=source_id,
    )
    if source_detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reference source not found.")
    items, total = source_word_review_service.list_reference_source_word_candidates(
        session,
        user_id=current_user.user_id,
        source=source,
        search=search,
        reference_status=reference_status,
        limit=limit,
        offset=offset,
    )
    return ReferenceSourceWordCandidateListResponse(
        source=ReferenceSourceWordCandidateSourceSummary(
            source_id=str(source_detail.id),
            source_title=source_detail.display_name,
            source_subtitle=source_detail.description,
            reference_link=f"/reference-sources/{source_detail.id}",
            import_method=source_detail.last_import_method,
            warning_message=source_detail.last_import_warning,
            imported_entry_count=source_detail.imported_entry_count,
            matched_entry_count=source_detail.matched_entry_count,
            unmatched_entry_count=source_detail.unmatched_entry_count,
        ),
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{source_id}/entries", response_model=ReferenceSourceEntryListResponse)
async def list_reference_source_entries(
    source_id: UUID,
    search: str | None = Query(default=None),
    match_status: ReferenceStatusFilter = Query(default=ReferenceStatusFilter.ALL),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    reference_source_service: ReferenceSourceService = Depends(get_reference_source_service),
    reference_matching_service: ReferenceMatchingService = Depends(get_reference_matching_service),
) -> ReferenceSourceEntryListResponse:
    try:
        source = reference_source_service.get_user_source(
            session,
            user_id=current_user.user_id,
            source_id=source_id,
        )
    except ReferenceSourceSchemaNotReadyError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reference source not found.")

    items, total = reference_matching_service.list_source_entries(
        session,
        user_id=current_user.user_id,
        source_id=source_id,
        search=search,
        match_status=match_status,
        limit=limit,
        offset=offset,
    )
    return ReferenceSourceEntryListResponse(items=items, total=total, limit=limit, offset=offset)
