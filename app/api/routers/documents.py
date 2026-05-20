from __future__ import annotations

from io import BytesIO
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db_session
from app.core.config import get_settings
from app.db.models import DocumentPage, JobKind
from app.schemas.document import (
    DocumentListResponse,
    DocumentOptionListResponse,
    DocumentRead,
    DocumentStartResponse,
    DocumentStatusStatsRead,
)
from app.schemas.workflow import DocumentWorkflowRead
from app.schemas.morphology import DocumentMorphologySettingsResponse, MorphologyRunCreateRequest, MorphologySummaryResponse, MorphologySettingsUpdateRequest
from app.schemas.page import DocumentPageListResponse
from app.schemas.reference import ReferenceStatusFilter
from app.schemas.word import DocumentWordCandidateListResponse, SourceWordStatusView
from app.services.auth_service import AuthenticatedUser
from app.services.document_service import DocumentService, get_document_service
from app.services.document_workflow_service import DocumentWorkflowService, get_document_workflow_service
from app.services.ingestion_error_service import get_ingestion_error_service
from app.services.ingestion_job_service import IngestionJobService, get_ingestion_job_service
from app.services.job_orchestrator import get_job_orchestrator
from app.services.long_running_job_service import LongRunningJobService, get_long_running_job_service
from app.services.morphology.morphology_service import MorphologyService, get_morphology_service
from app.services.lexicon_index_rebuild_service import (
    LexiconIndexRebuildService,
    get_lexicon_index_rebuild_service,
)
from app.services.source_word_review_service import SourceWordReviewService, get_source_word_review_service
from app.services.storage_service import StorageService, get_storage_service
from app.schemas.lexicon import LexiconIndexRebuildResponse
from app.utils.file_hash import sha256_digest
from app.utils.mime import detect_mime_type
from app.api.routers.morphology import start_morphology_run_or_raise


router = APIRouter(prefix="/documents")


@router.post("/upload", response_model=DocumentStartResponse, status_code=status.HTTP_201_CREATED)
async def upload_document(
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
    language_stage: str | None = Form(default=None),
    morphology_profile: str | None = Form(default=None),
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    document_service: DocumentService = Depends(get_document_service),
    ingestion_job_service: IngestionJobService = Depends(get_ingestion_job_service),
) -> DocumentStartResponse:
    filename = file.filename or "upload.bin"
    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty.")

    settings = get_settings()
    if len(file_bytes) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Upload exceeds MAX_UPLOAD_MB={settings.max_upload_mb}.",
        )

    try:
        mime_type = detect_mime_type(filename, file_bytes, file.content_type)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    document, job = document_service.create_document_and_job(
        session,
        user_id=current_user.user_id,
        title=title,
        original_filename=filename,
        mime_type=mime_type,
        file_size_bytes=len(file_bytes),
        file_bytes=file_bytes,
        sha256=sha256_digest(file_bytes),
        language_stage=language_stage,
        morphology_profile=morphology_profile,
    )

    try:
        get_job_orchestrator().enqueue(JobKind.INGESTION, job.id)
        document = document_service.mark_document_queued(session, document_id=document.id)
        session.refresh(job)
    except Exception as exc:
        failure_info = get_ingestion_error_service().map_exception(exc)
        document_service.mark_job_failed(
            session,
            document_id=document.id,
            job_id=job.id,
            failure_info=failure_info,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to enqueue ingestion job.",
        ) from exc

    refreshed_job = ingestion_job_service.get_user_job(session, user_id=current_user.user_id, job_id=job.id)
    if refreshed_job is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Job not found after upload.")
    return DocumentStartResponse(
        message="Processing started",
        document=document_service.build_document_read(session, document),
        job=ingestion_job_service.build_job_read(session, refreshed_job),
    )


@router.get("/stats", response_model=DocumentStatusStatsRead)
async def get_document_stats(
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    document_service: DocumentService = Depends(get_document_service),
) -> DocumentStatusStatsRead:
    return DocumentStatusStatsRead(
        **document_service.document_status_stats(session, user_id=current_user.user_id),
    )


@router.get("/options", response_model=DocumentOptionListResponse)
async def list_document_options(
    search: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    document_service: DocumentService = Depends(get_document_service),
) -> DocumentOptionListResponse:
    items, total = document_service.list_document_options(
        session,
        user_id=current_user.user_id,
        limit=limit,
        offset=offset,
        search=search,
    )
    return DocumentOptionListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("", response_model=DocumentListResponse)
async def list_documents(
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    include_workspace_summary: bool = Query(default=False),
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    document_service: DocumentService = Depends(get_document_service),
) -> DocumentListResponse:
    items, total = document_service.list_documents(
        session,
        user_id=current_user.user_id,
        limit=limit,
        offset=offset,
    )
    return DocumentListResponse(
        items=document_service.build_documents_read(
            session,
            items,
            include_workspace_summary=include_workspace_summary,
        ),
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{document_id}", response_model=DocumentRead)
async def get_document(
    document_id: UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    document_service: DocumentService = Depends(get_document_service),
) -> DocumentRead:
    document = document_service.get_user_document(
        session,
        user_id=current_user.user_id,
        document_id=document_id,
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")
    return document_service.build_document_read(session, document, include_workspace_summary=True)


@router.get("/{document_id}/workflow", response_model=DocumentWorkflowRead)
async def get_document_workflow(
    document_id: UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    workflow_service: DocumentWorkflowService = Depends(get_document_workflow_service),
) -> DocumentWorkflowRead:
    workflow = workflow_service.get_document_workflow(
        session,
        user_id=current_user.user_id,
        document_id=document_id,
    )
    if workflow is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")
    return workflow


@router.get("/{document_id}/pages", response_model=DocumentPageListResponse)
async def list_document_pages(
    document_id: UUID,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    document_service: DocumentService = Depends(get_document_service),
) -> DocumentPageListResponse:
    document = document_service.get_user_document(
        session,
        user_id=current_user.user_id,
        document_id=document_id,
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")

    items, total = document_service.list_document_pages(
        session,
        user_id=current_user.user_id,
        document_id=document_id,
        limit=limit,
        offset=offset,
    )
    return DocumentPageListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/{document_id}/pages/{page_id}/image")
async def get_document_page_image(
    document_id: UUID,
    page_id: UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    document_service: DocumentService = Depends(get_document_service),
    storage_service: StorageService = Depends(get_storage_service),
) -> StreamingResponse:
    document = document_service.get_user_document(
        session,
        user_id=current_user.user_id,
        document_id=document_id,
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")

    page = session.scalar(
        select(DocumentPage).where(
            DocumentPage.id == page_id,
            DocumentPage.document_id == document_id,
        )
    )
    if page is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Page not found.")
    if not page.page_image_bucket or not page.page_image_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Page image not available.")

    image_bytes = storage_service.download_bytes(page.page_image_bucket, page.page_image_path)
    return StreamingResponse(
        BytesIO(image_bytes),
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=300"},
    )


@router.get("/{document_id}/word-candidates", response_model=DocumentWordCandidateListResponse)
async def list_document_word_candidates(
    document_id: UUID,
    search: str | None = Query(default=None),
    status_view: SourceWordStatusView = Query(default=SourceWordStatusView.UNLINKED),
    reference_status: ReferenceStatusFilter = Query(default=ReferenceStatusFilter.ALL),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    document_service: DocumentService = Depends(get_document_service),
    source_word_review_service: SourceWordReviewService = Depends(get_source_word_review_service),
) -> DocumentWordCandidateListResponse:
    document = document_service.get_user_document(
        session,
        user_id=current_user.user_id,
        document_id=document_id,
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")
    items, total = source_word_review_service.list_document_word_candidates(
        session,
        user_id=current_user.user_id,
        document=document,
        search=search,
        status_view=status_view,
        reference_status=reference_status,
        limit=limit,
        offset=offset,
    )
    return DocumentWordCandidateListResponse(items=items, total=total, limit=limit, offset=offset)


@router.post("/{document_id}/rebuild-index", response_model=LexiconIndexRebuildResponse)
async def rebuild_document_lexicon_index(
    document_id: UUID,
    background: bool = Query(default=False),
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    document_service: DocumentService = Depends(get_document_service),
    rebuild_service: LexiconIndexRebuildService = Depends(get_lexicon_index_rebuild_service),
) -> LexiconIndexRebuildResponse:
    document = document_service.get_user_document(
        session,
        user_id=current_user.user_id,
        document_id=document_id,
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")

    if background:
        task_id = rebuild_service.rebuild_document_async(
            user_id=current_user.user_id,
            document_id=document_id,
        )
        return LexiconIndexRebuildResponse(
            message="Lexicon index rebuild queued.",
            task_id=task_id,
        )

    try:
        form_count = rebuild_service.rebuild_document(
            session,
            user_id=current_user.user_id,
            document_id=document_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return LexiconIndexRebuildResponse(
        message="Lexicon index rebuilt.",
        form_count=form_count,
    )


@router.get("/{document_id}/morphology-summary", response_model=MorphologySummaryResponse)
async def get_document_morphology_summary(
    document_id: UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    document_service: DocumentService = Depends(get_document_service),
    morphology_service: MorphologyService = Depends(get_morphology_service),
) -> MorphologySummaryResponse:
    document = document_service.get_user_document(
        session,
        user_id=current_user.user_id,
        document_id=document_id,
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")
    return morphology_service.get_document_summary(
        session,
        user_id=current_user.user_id,
        document_id=document_id,
    )


@router.patch("/{document_id}/morphology-settings", response_model=DocumentMorphologySettingsResponse)
async def update_document_morphology_settings(
    document_id: UUID,
    request: MorphologySettingsUpdateRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    document_service: DocumentService = Depends(get_document_service),
    morphology_service: MorphologyService = Depends(get_morphology_service),
    long_running_job_service: LongRunningJobService = Depends(get_long_running_job_service),
) -> DocumentMorphologySettingsResponse:
    try:
        document = document_service.update_morphology_settings(
            session,
            user_id=current_user.user_id,
            document_id=document_id,
            language_stage=request.language_stage,
            morphology_profile=request.morphology_profile,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    run_response = None
    if request.run_morphology:
        run_response = start_morphology_run_or_raise(
            session=session,
            current_user=current_user,
            request=MorphologyRunCreateRequest(
                document_id=document_id,
                analyzer=request.analyzer,
            ),
            morphology_service=morphology_service,
            long_running_job_service=long_running_job_service,
        )

    return DocumentMorphologySettingsResponse(
        message="Document morphology settings updated",
        document=document_service.build_document_read(session, document),
        run=run_response.run if run_response is not None else None,
        job=run_response.job if run_response is not None else None,
    )
