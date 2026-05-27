from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.deps import get_db_session, require_admin_user
from app.schemas.lexicon import (
    LexiconActionRequest,
    LexiconActionResponse,
    LexiconGroupDetail,
    LexiconGroupListResponse,
    LexiconGroupSortDirection,
    LexiconGroupSortKey,
    LexiconGroupView,
    LexiconIndexRebuildResponse,
)
from app.services.lexicon_action_service import LexiconActionService, get_lexicon_action_service
from app.services.lexicon_index_rebuild_service import (
    LexiconIndexRebuildService,
    get_lexicon_index_rebuild_service,
)
from app.schemas.reference import ReferenceStatusFilter, ReferenceTargetMatchesResponse
from app.services.auth_service import AuthenticatedUser
from app.services.lexeme_service import LexemeConflictError
from app.services.lexicon_service import LexiconService, get_lexicon_service
from app.services.reference_matching_service import (
    ReferenceMatchingService,
    ReferenceSchemaNotReadyError,
    get_reference_matching_service,
)


router = APIRouter(prefix="/lexicon")


def _conflict_response(exc: LexemeConflictError) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_409_CONFLICT, content=exc.payload())


@router.get("/groups", response_model=LexiconGroupListResponse)
async def list_lexicon_groups(
    search: str | None = Query(default=None),
    view: LexiconGroupView = Query(default=LexiconGroupView.CANDIDATES),
    reference_status: ReferenceStatusFilter = Query(default=ReferenceStatusFilter.ALL),
    document_id: UUID | None = Query(default=None),
    sort_by: LexiconGroupSortKey | None = Query(default=None),
    sort_dir: LexiconGroupSortDirection = Query(default=LexiconGroupSortDirection.DESC),
    include_reference_summary: bool = Query(default=False),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: AuthenticatedUser = Depends(require_admin_user),
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
        sort_by=sort_by,
        sort_dir=sort_dir,
        include_reference_summary=include_reference_summary,
    )
    return LexiconGroupListResponse(items=items, total=total, limit=limit, offset=offset)


@router.post("/actions", response_model=LexiconActionResponse)
async def apply_lexicon_action(
    request: LexiconActionRequest,
    current_user: AuthenticatedUser = Depends(require_admin_user),
    session: Session = Depends(get_db_session),
    action_service: LexiconActionService = Depends(get_lexicon_action_service),
) -> LexiconActionResponse | JSONResponse:
    try:
        return action_service.run_action(
            session,
            user_id=current_user.user_id,
            request=request,
        )
    except LexemeConflictError as exc:
        return _conflict_response(exc)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/groups/{normalized_form}", response_model=LexiconGroupDetail)
async def get_lexicon_group_detail(
    normalized_form: str,
    current_user: AuthenticatedUser = Depends(require_admin_user),
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


@router.post("/rebuild-index", response_model=LexiconIndexRebuildResponse)
async def rebuild_user_lexicon_index(
    background: bool = Query(default=False),
    current_user: AuthenticatedUser = Depends(require_admin_user),
    session: Session = Depends(get_db_session),
    rebuild_service: LexiconIndexRebuildService = Depends(get_lexicon_index_rebuild_service),
) -> LexiconIndexRebuildResponse:
    if background:
        task_id = rebuild_service.rebuild_user_async(user_id=current_user.user_id)
        return LexiconIndexRebuildResponse(
            message="Lexicon index rebuild queued.",
            task_id=task_id,
        )

    document_count = rebuild_service.rebuild_user(session, user_id=current_user.user_id)
    return LexiconIndexRebuildResponse(
        message="Lexicon index rebuilt.",
        document_count=document_count,
    )


@router.get("/groups/{normalized_form}/reference-matches", response_model=ReferenceTargetMatchesResponse)
async def get_lexicon_group_reference_matches(
    normalized_form: str,
    current_user: AuthenticatedUser = Depends(require_admin_user),
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
