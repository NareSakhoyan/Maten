from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db_session
from app.core.celery_app import celery_app
from app.core.config import get_settings
from app.schemas.document import DocumentListResponse, DocumentRead, DocumentStartResponse
from app.schemas.page import DocumentPageListResponse
from app.schemas.reference import ReferenceStatusFilter
from app.schemas.word import DocumentWordCandidateListResponse, SourceWordStatusView
from app.services.auth_service import AuthenticatedUser
from app.services.document_service import DocumentService, get_document_service
from app.services.ingestion_error_service import get_ingestion_error_service
from app.services.ingestion_job_service import IngestionJobService, get_ingestion_job_service
from app.services.source_word_review_service import SourceWordReviewService, get_source_word_review_service
from app.utils.file_hash import sha256_digest
from app.utils.mime import detect_mime_type


router = APIRouter(prefix="/documents")


@router.post("/upload", response_model=DocumentStartResponse, status_code=status.HTTP_201_CREATED)
async def upload_document(
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
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
    )

    try:
        celery_app.send_task(
            "app.workers.tasks.process_document_ingestion",
            args=[str(job.id)],
            task_id=str(job.id),
        )
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


@router.get("", response_model=DocumentListResponse)
async def list_documents(
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
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
        items=[document_service.build_document_read(session, item) for item in items],
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
    return document_service.build_document_read(session, document)


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
