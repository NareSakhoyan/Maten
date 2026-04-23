from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db_session
from app.schemas.word import (
    WordCheckResponse,
    WordEvidenceResponse,
    WordEvidenceSourceType,
    WordSearchCategory,
    WordSearchMode,
    WordSearchResponse,
)
from app.services.auth_service import AuthenticatedUser
from app.services.word_evidence_service import WordEvidenceService, get_word_evidence_service
from app.services.word_search_service import WordSearchService, get_word_search_service


router = APIRouter()


@router.get("/word-evidence", response_model=WordEvidenceResponse)
async def get_word_evidence(
    normalized_form: str = Query(..., min_length=1),
    source_type: WordEvidenceSourceType | None = Query(default=None),
    source_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    word_evidence_service: WordEvidenceService = Depends(get_word_evidence_service),
) -> WordEvidenceResponse:
    try:
        return word_evidence_service.get_word_evidence(
            session,
            user_id=current_user.user_id,
            normalized_form=normalized_form,
            source_type=source_type,
            source_id=source_id,
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/words/search", response_model=WordSearchResponse)
async def search_words(
    q: str = Query(..., min_length=1),
    mode: WordSearchMode = Query(default=WordSearchMode.NORMALIZED),
    include_categories: list[WordSearchCategory] | None = Query(default=None),
    limit_per_category: int = Query(default=20, ge=1, le=100),
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    word_search_service: WordSearchService = Depends(get_word_search_service),
) -> WordSearchResponse:
    try:
        return word_search_service.search(
            session,
            user_id=current_user.user_id,
            query=q,
            mode=mode,
            include_categories=include_categories or [],
            limit_per_category=limit_per_category,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/words/check", response_model=WordCheckResponse)
async def check_word(
    q: str = Query(..., min_length=1),
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    word_search_service: WordSearchService = Depends(get_word_search_service),
) -> WordCheckResponse:
    try:
        return word_search_service.check(
            session,
            user_id=current_user.user_id,
            query=q,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
