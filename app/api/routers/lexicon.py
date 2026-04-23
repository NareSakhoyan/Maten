from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db_session
from app.schemas.lexicon import (
    LexiconGroupDetail,
    LexiconGroupIgnoreRequest,
    LexiconGroupListResponse,
    LexiconGroupReviewActionResponse,
    LexiconGroupState,
    LexiconGroupUnignoreRequest,
    LexiconGroupView,
)
from app.schemas.reference import ReferenceStatusFilter, ReferenceTargetMatchesResponse
from app.services.auth_service import AuthenticatedUser
from app.services.lexicon_review_service import LexiconReviewService, get_lexicon_review_service
from app.services.lexicon_service import LexiconService, get_lexicon_service
from app.services.reference_matching_service import (
    ReferenceMatchingService,
    ReferenceSchemaNotReadyError,
    get_reference_matching_service,
)


router = APIRouter(prefix="/lexicon")


@router.get("/groups", response_model=LexiconGroupListResponse)
async def list_lexicon_groups(
    search: str | None = Query(default=None),
    view: LexiconGroupView = Query(default=LexiconGroupView.CANDIDATES),
    reference_status: ReferenceStatusFilter = Query(default=ReferenceStatusFilter.ALL),
    document_id: UUID | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    lexicon_service: LexiconService = Depends(get_lexicon_service),
) -> LexiconGroupListResponse:
    items, total = lexicon_service.list_groups(
        session,
        user_id=current_user.user_id,
        limit=limit,
        offset=offset,
        search=search,
        view=view,
        document_id=document_id,
        reference_status=reference_status,
    )
    return LexiconGroupListResponse(items=items, total=total, limit=limit, offset=offset)


@router.post("/groups/ignore", response_model=LexiconGroupReviewActionResponse)
async def ignore_lexicon_groups(
    request: LexiconGroupIgnoreRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    lexicon_review_service: LexiconReviewService = Depends(get_lexicon_review_service),
) -> LexiconGroupReviewActionResponse:
    try:
        normalized_forms = lexicon_review_service.ignore_groups(
            session,
            user_id=current_user.user_id,
            normalized_forms=request.normalized_forms,
            reviewer_note=request.reviewer_note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return LexiconGroupReviewActionResponse(
        normalized_forms=normalized_forms,
        group_state=LexiconGroupState.IGNORED_NOISE,
    )


@router.post("/groups/unignore", response_model=LexiconGroupReviewActionResponse)
async def unignore_lexicon_groups(
    request: LexiconGroupUnignoreRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    lexicon_review_service: LexiconReviewService = Depends(get_lexicon_review_service),
) -> LexiconGroupReviewActionResponse:
    try:
        normalized_forms = lexicon_review_service.unignore_groups(
            session,
            user_id=current_user.user_id,
            normalized_forms=request.normalized_forms,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return LexiconGroupReviewActionResponse(
        normalized_forms=normalized_forms,
        group_state=LexiconGroupState.UNREVIEWED,
    )


@router.get("/groups/{normalized_form}", response_model=LexiconGroupDetail)
async def get_lexicon_group_detail(
    normalized_form: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    lexicon_service: LexiconService = Depends(get_lexicon_service),
) -> LexiconGroupDetail:
    group = lexicon_service.get_group_detail(
        session,
        user_id=current_user.user_id,
        normalized_form=normalized_form,
        occurrence_cap=100,
    )
    if group is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lexicon group not found.")
    return group


@router.get("/groups/{normalized_form}/reference-matches", response_model=ReferenceTargetMatchesResponse)
async def get_lexicon_group_reference_matches(
    normalized_form: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    lexicon_service: LexiconService = Depends(get_lexicon_service),
    reference_matching_service: ReferenceMatchingService = Depends(get_reference_matching_service),
) -> ReferenceTargetMatchesResponse:
    group = lexicon_service.get_group_detail(
        session,
        user_id=current_user.user_id,
        normalized_form=normalized_form,
        occurrence_cap=1,
    )
    if group is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lexicon group not found.")
    try:
        return reference_matching_service.match_group(
            session,
            user_id=current_user.user_id,
            normalized_form=group.normalized_form,
        )
    except ReferenceSchemaNotReadyError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
