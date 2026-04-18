from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db_session
from app.schemas.occurrence import OccurrenceListResponse
from app.services.auth_service import AuthenticatedUser
from app.services.document_service import DocumentService, get_document_service
from app.utils.text_normalization import normalize_token


router = APIRouter(prefix="/documents")


@router.get("/{document_id}/occurrences", response_model=OccurrenceListResponse)
async def list_occurrences(
    document_id: UUID,
    page_number: int | None = Query(default=None, ge=1),
    normalized_token: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    document_service: DocumentService = Depends(get_document_service),
) -> OccurrenceListResponse:
    document = document_service.get_user_document(
        session,
        user_id=current_user.user_id,
        document_id=document_id,
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")

    normalized_filter = normalize_token(normalized_token) if normalized_token else None
    items, total = document_service.list_occurrences(
        session,
        user_id=current_user.user_id,
        document_id=document_id,
        limit=limit,
        offset=offset,
        page_number=page_number,
        normalized_token=normalized_filter,
    )
    return OccurrenceListResponse(items=items, total=total, limit=limit, offset=offset)
