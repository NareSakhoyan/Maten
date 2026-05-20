from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db_session
from app.schemas.morphology import MorphologyWordResponse
from app.schemas.word import (
    WordCheckResponse,
    WordEvidenceResponse,
    WordEvidenceSourceType,
    WordSearchCategory,
    WordSearchMode,
    WordSearchResponse,
)
from app.services.auth_service import AuthenticatedUser
from app.services.morphology.morphology_service import MorphologyService, get_morphology_service
from app.services.word_evidence_service import WordEvidenceService, get_word_evidence_service
from app.services.word_search_service import WordSearchService, get_word_search_service


router = APIRouter()


def _resolve_search_categories(
    *,
    include_categories: list[WordSearchCategory] | None,
    include_lexicon: bool | None,
    include_documents: bool | None,
    include_reference_sources: bool | None,
    include_trusted_external: bool | None,
) -> tuple[list[WordSearchCategory] | None, bool]:
    if include_categories is not None:
        resolved_categories = list(dict.fromkeys(include_categories))
        return resolved_categories, WordSearchCategory.TRUSTED_EXTERNAL in resolved_categories

    explicit_flags = [
        include_lexicon,
        include_documents,
        include_reference_sources,
        include_trusted_external,
    ]
    if not any(flag is not None for flag in explicit_flags):
        return None, False

    resolved_categories: list[WordSearchCategory] = []
    if include_lexicon:
        resolved_categories.append(WordSearchCategory.LEXICON)
    if include_documents:
        resolved_categories.append(WordSearchCategory.IMPORTED_BOOKS)
    if include_reference_sources:
        resolved_categories.append(WordSearchCategory.REFERENCE_SOURCES)
    if include_trusted_external:
        resolved_categories.append(WordSearchCategory.TRUSTED_EXTERNAL)
    return resolved_categories, bool(include_trusted_external)


@router.get("/word-evidence", response_model=WordEvidenceResponse)
async def get_word_evidence(
    normalized_form: str = Query(..., min_length=1),
    source_type: WordEvidenceSourceType | None = Query(default=None),
    source_id: str | None = Query(default=None),
    include_external: Annotated[bool, Query()] = False,
    provider_keys: Annotated[list[str] | None, Query()] = None,
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
            include_external=include_external,
            provider_keys=provider_keys,
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/words/{normalized_form}/morphology", response_model=MorphologyWordResponse)
async def get_word_morphology(
    normalized_form: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    morphology_service: MorphologyService = Depends(get_morphology_service),
) -> MorphologyWordResponse:
    try:
        return morphology_service.get_word_morphology(
            session,
            user_id=current_user.user_id,
            normalized_form=normalized_form,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/words/search", response_model=WordSearchResponse)
async def search_words(
    q: str = Query(..., min_length=1),
    mode: WordSearchMode = Query(default=WordSearchMode.NORMALIZED),
    include_categories: list[WordSearchCategory] | None = Query(default=None),
    include_lexicon: Annotated[bool | None, Query()] = None,
    include_documents: Annotated[bool | None, Query()] = None,
    include_reference_sources: Annotated[bool | None, Query()] = None,
    include_trusted_external: Annotated[bool | None, Query()] = None,
    include_external: Annotated[bool, Query()] = False,
    provider_keys: Annotated[list[str] | None, Query()] = None,
    limit_per_category: int = Query(default=20, ge=1, le=100),
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    word_search_service: WordSearchService = Depends(get_word_search_service),
) -> WordSearchResponse:
    try:
        resolved_categories, resolved_include_external = _resolve_search_categories(
            include_categories=include_categories,
            include_lexicon=include_lexicon,
            include_documents=include_documents,
            include_reference_sources=include_reference_sources,
            include_trusted_external=include_trusted_external,
        )
        return word_search_service.search(
            session,
            user_id=current_user.user_id,
            query=q,
            mode=mode,
            include_categories=resolved_categories,
            include_external=include_external or resolved_include_external,
            provider_keys=provider_keys,
            limit_per_category=limit_per_category,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/words/check", response_model=WordCheckResponse)
async def check_word(
    q: str = Query(..., min_length=1),
    include_external: Annotated[bool, Query()] = False,
    provider_keys: Annotated[list[str] | None, Query()] = None,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    word_search_service: WordSearchService = Depends(get_word_search_service),
) -> WordCheckResponse:
    try:
        return word_search_service.check(
            session,
            user_id=current_user.user_id,
            query=q,
            include_external=include_external,
            provider_keys=provider_keys,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
