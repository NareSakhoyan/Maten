from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.deps import get_db_session, require_admin_user
from app.schemas.lexeme import (
    LexemeCreateRequest,
    LexemeDetail,
    LexemeListResponse,
    LexemeMergeGroupsRequest,
    LexemePickerItem,
    LexemePickerListResponse,
    LexemeUpdateRequest,
)
from app.schemas.reference import ReferenceStatusFilter, ReferenceTargetMatchesResponse
from app.services.auth_service import AuthenticatedUser
from app.services.lexeme_service import LexemeConflictError, LexemeService, get_lexeme_service
from app.services.reference_matching_service import (
    ReferenceMatchingService,
    ReferenceSchemaNotReadyError,
    get_reference_matching_service,
)


router = APIRouter(prefix="/lexemes")


def _conflict_response(exc: LexemeConflictError) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_409_CONFLICT, content=exc.payload())


@router.post("", response_model=LexemeDetail, status_code=status.HTTP_201_CREATED)
async def create_lexeme(
    request: LexemeCreateRequest,
    current_user: AuthenticatedUser = Depends(require_admin_user),
    session: Session = Depends(get_db_session),
    lexeme_service: LexemeService = Depends(get_lexeme_service),
) -> LexemeDetail | JSONResponse:
    try:
        return lexeme_service.create_lexeme(session, user_id=current_user.user_id, request=request)
    except LexemeConflictError as exc:
        return _conflict_response(exc)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("", response_model=LexemeListResponse)
async def list_lexemes(
    search: str | None = Query(default=None),
    reference_status: ReferenceStatusFilter = Query(default=ReferenceStatusFilter.ALL),
    include_reference_summary: bool = Query(default=False),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: AuthenticatedUser = Depends(require_admin_user),
    session: Session = Depends(get_db_session),
    lexeme_service: LexemeService = Depends(get_lexeme_service),
) -> LexemeListResponse:
    items, total = lexeme_service.list_lexemes(
        session,
        user_id=current_user.user_id,
        limit=limit,
        offset=offset,
        search=search,
        reference_status=reference_status,
        include_reference_summary=include_reference_summary,
    )
    return LexemeListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/picker", response_model=LexemePickerListResponse)
async def list_lexeme_picker(
    search: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: AuthenticatedUser = Depends(require_admin_user),
    session: Session = Depends(get_db_session),
    lexeme_service: LexemeService = Depends(get_lexeme_service),
) -> LexemePickerListResponse:
    items, total = lexeme_service.list_lexeme_picker(
        session,
        user_id=current_user.user_id,
        limit=limit,
        offset=offset,
        search=search,
    )
    return LexemePickerListResponse(
        items=[
            LexemePickerItem(
                id=item.id,
                canonical_form=item.canonical_form,
                canonical_normalized_form=item.canonical_normalized_form,
                status=item.status,
            )
            for item in items
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{lexeme_id}", response_model=LexemeDetail)
async def get_lexeme(
    lexeme_id: UUID,
    current_user: AuthenticatedUser = Depends(require_admin_user),
    session: Session = Depends(get_db_session),
    lexeme_service: LexemeService = Depends(get_lexeme_service),
) -> LexemeDetail:
    lexeme = lexeme_service.get_user_lexeme(session, user_id=current_user.user_id, lexeme_id=lexeme_id)
    if lexeme is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lexeme not found.")
    return lexeme_service.get_lexeme_detail(session, user_id=current_user.user_id, lexeme_id=lexeme_id)


@router.get("/{lexeme_id}/reference-matches", response_model=ReferenceTargetMatchesResponse)
async def get_lexeme_reference_matches(
    lexeme_id: UUID,
    current_user: AuthenticatedUser = Depends(require_admin_user),
    session: Session = Depends(get_db_session),
    lexeme_service: LexemeService = Depends(get_lexeme_service),
    reference_matching_service: ReferenceMatchingService = Depends(get_reference_matching_service),
) -> ReferenceTargetMatchesResponse:
    lexeme = lexeme_service.get_user_lexeme(session, user_id=current_user.user_id, lexeme_id=lexeme_id)
    if lexeme is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lexeme not found.")
    try:
        return reference_matching_service.match_lexeme(
            session,
            user_id=current_user.user_id,
            lexeme=lexeme,
        )
    except ReferenceSchemaNotReadyError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.patch("/{lexeme_id}", response_model=LexemeDetail)
async def update_lexeme(
    lexeme_id: UUID,
    request: LexemeUpdateRequest,
    current_user: AuthenticatedUser = Depends(require_admin_user),
    session: Session = Depends(get_db_session),
    lexeme_service: LexemeService = Depends(get_lexeme_service),
) -> LexemeDetail:
    try:
        lexeme = lexeme_service.update_lexeme(
            session,
            user_id=current_user.user_id,
            lexeme_id=lexeme_id,
            request=request,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    if lexeme is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lexeme not found.")
    return lexeme


@router.post("/{lexeme_id}/merge-groups", response_model=LexemeDetail)
async def merge_lexeme_groups(
    lexeme_id: UUID,
    request: LexemeMergeGroupsRequest,
    current_user: AuthenticatedUser = Depends(require_admin_user),
    session: Session = Depends(get_db_session),
    lexeme_service: LexemeService = Depends(get_lexeme_service),
) -> LexemeDetail | JSONResponse:
    try:
        lexeme = lexeme_service.merge_groups(
            session,
            user_id=current_user.user_id,
            lexeme_id=lexeme_id,
            request=request,
        )
    except LexemeConflictError as exc:
        return _conflict_response(exc)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    if lexeme is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lexeme not found.")
    return lexeme
