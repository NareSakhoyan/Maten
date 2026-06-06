from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db_session, require_admin_user
from app.core.security import forbidden
from app.schemas.discovery import (
    DiscoveryBuildStartResponse,
    DiscoveryCandidateDecisionRequest,
    DiscoveryCandidateDecisionResponse,
    DiscoveryCandidateDetailResponse,
    DiscoveryCandidateListResponse,
    DiscoveryCandidateRead,
    DiscoverySummaryResponse,
)
from app.services.auth_service import AuthenticatedUser
from app.services.backpressure_service import BackpressureLimitError
from app.services.discovery.discovery_candidate_service import (
    DiscoveryCandidateService,
    get_discovery_candidate_service,
)
from app.services.document_service import DocumentService, get_document_service


router = APIRouter(prefix="/documents/{document_id}/discovery")


@router.post("/build", response_model=DiscoveryBuildStartResponse, status_code=status.HTTP_202_ACCEPTED)
def build_document_discovery_queue(
    document_id: UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    document_service: DocumentService = Depends(get_document_service),
    discovery_service: DiscoveryCandidateService = Depends(get_discovery_candidate_service),
) -> DiscoveryBuildStartResponse:
    is_admin = current_user.role == "admin"
    document = document_service.get_user_document(
        session,
        user_id=current_user.user_id,
        document_id=document_id,
        include_all_users=is_admin,
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")
    try:
        run = discovery_service.start_build_run(session, user_id=document.user_id, document_id=document_id)
    except BackpressureLimitError as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return DiscoveryBuildStartResponse(
        message="Discovery build queued for this document.",
        run_id=run.id,
        job_id=run.id,
    )


@router.post("/reference-evidence/update", response_model=DiscoveryBuildStartResponse, status_code=status.HTTP_202_ACCEPTED)
def update_document_reference_evidence(
    document_id: UUID,
    reference_source_id: UUID | None = Query(default=None),
    current_user: AuthenticatedUser = Depends(require_admin_user),
    session: Session = Depends(get_db_session),
    document_service: DocumentService = Depends(get_document_service),
    discovery_service: DiscoveryCandidateService = Depends(get_discovery_candidate_service),
) -> DiscoveryBuildStartResponse:
    document = document_service.get_user_document(
        session,
        user_id=current_user.user_id,
        document_id=document_id,
        include_all_users=True,
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")
    try:
        run = discovery_service.start_reference_evidence_refresh_run(
            session,
            user_id=document.user_id,
            document_id=document_id,
            reference_source_id=reference_source_id,
        )
    except BackpressureLimitError as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)) from exc
    except ValueError as exc:
        message = str(exc)
        status_code = status.HTTP_400_BAD_REQUEST if "No imported reference dataset" in message else status.HTTP_404_NOT_FOUND
        raise HTTPException(status_code=status_code, detail=message) from exc
    return DiscoveryBuildStartResponse(
        message="Reference evidence refresh queued for this document.",
        run_id=run.id,
        job_id=run.id,
    )


@router.get("/candidates", response_model=DiscoveryCandidateListResponse)
def list_document_discovery_candidates(
    document_id: UUID,
    search: str | None = Query(default=None),
    candidate_type: str | None = Query(default=None),
    resolution_status: str | None = Query(default=None),
    review_status: str | None = Query(default=None),
    min_interest_score: float | None = Query(default=None, ge=0),
    include_suppressed: bool = Query(default=False),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    sort: str = Query(default="occurrence_count_asc"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    document_service: DocumentService = Depends(get_document_service),
    discovery_service: DiscoveryCandidateService = Depends(get_discovery_candidate_service),
) -> DiscoveryCandidateListResponse:
    is_admin = current_user.role == "admin"
    document = document_service.get_user_document(
        session,
        user_id=current_user.user_id,
        document_id=document_id,
        include_all_users=is_admin,
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")
    items, total = discovery_service.list_candidates(
        session,
        user_id=document.user_id,
        document_id=document_id,
        search=search,
        candidate_type=candidate_type,
        resolution_status=resolution_status,
        review_status=review_status,
        min_interest_score=min_interest_score,
        include_suppressed=include_suppressed,
        limit=limit,
        offset=offset,
        sort=sort,
    )
    return DiscoveryCandidateListResponse(
        items=[DiscoveryCandidateRead.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/summary", response_model=DiscoverySummaryResponse)
def get_document_discovery_summary(
    document_id: UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    document_service: DocumentService = Depends(get_document_service),
    discovery_service: DiscoveryCandidateService = Depends(get_discovery_candidate_service),
) -> DiscoverySummaryResponse:
    is_admin = current_user.role == "admin"
    document = document_service.get_user_document(
        session,
        user_id=current_user.user_id,
        document_id=document_id,
        include_all_users=is_admin,
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")
    return discovery_service.get_summary(session, user_id=document.user_id, document_id=document_id)


@router.get("/candidates/{candidate_id}", response_model=DiscoveryCandidateDetailResponse)
def get_document_discovery_candidate(
    document_id: UUID,
    candidate_id: UUID,
    include_technical: bool = Query(default=False),
    include_raw_payload: bool = Query(default=False),
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    document_service: DocumentService = Depends(get_document_service),
    discovery_service: DiscoveryCandidateService = Depends(get_discovery_candidate_service),
) -> DiscoveryCandidateDetailResponse:
    is_admin = current_user.role == "admin"
    document = document_service.get_user_document(
        session,
        user_id=current_user.user_id,
        document_id=document_id,
        include_all_users=is_admin,
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")
    payload = discovery_service.get_candidate_detail(
        session,
        user_id=document.user_id,
        document_id=document_id,
        candidate_id=candidate_id,
    )
    if payload is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Discovery candidate not found.")
    candidate, provider_evidence, occurrence_evidence, decision = payload
    if include_raw_payload and current_user.role != "admin":
        raise forbidden("Admin access is required to view raw provider payloads.")
    if not include_technical and not include_raw_payload:
        provider_evidence = []
        morphology = {}
    else:
        provider_evidence = [
            item if include_raw_payload else item.model_copy(update={"payload": {}})
            for item in provider_evidence
        ]
        morphology = dict(candidate.best_evidence_summary.get("morphology", {}))
        morphology["lexeme_resolution"] = candidate.best_evidence_summary.get("lexeme_resolution", {})
    why_shown_raw = candidate.best_evidence_summary.get("reasons")
    why_shown = (
        [item for item in why_shown_raw if isinstance(item, str)]
        if isinstance(why_shown_raw, list)
        else []
    )
    return DiscoveryCandidateDetailResponse(
        candidate=DiscoveryCandidateRead.model_validate(candidate),
        why_shown=why_shown,
        provider_evidence=provider_evidence,
        occurrence_evidence=occurrence_evidence,
        morphology=morphology,
        decision=decision,
    )


@router.post("/candidates/{candidate_id}/decision", response_model=DiscoveryCandidateDecisionResponse)
def decide_document_discovery_candidate(
    document_id: UUID,
    candidate_id: UUID,
    request: DiscoveryCandidateDecisionRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: Session = Depends(get_db_session),
    document_service: DocumentService = Depends(get_document_service),
    discovery_service: DiscoveryCandidateService = Depends(get_discovery_candidate_service),
) -> DiscoveryCandidateDecisionResponse:
    is_admin = current_user.role == "admin"
    document = document_service.get_user_document(
        session,
        user_id=current_user.user_id,
        document_id=document_id,
        include_all_users=is_admin,
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")
    try:
        candidate = discovery_service.record_decision(
            session,
            user_id=document.user_id,
            document_id=document_id,
            candidate_id=candidate_id,
            decision=request.decision,
            note=request.note,
            linked_lexeme_id=request.linked_lexeme_id,
        create_lexeme_canonical_form=request.create_lexeme_canonical_form,
        create_lexeme_definition=request.create_lexeme_definition,
        )
    except ValueError as exc:
        message = str(exc)
        bad_request_markers = (
            "Unsupported decision",
            "linked_lexeme_id is required",
        )
        status_code = (
            status.HTTP_400_BAD_REQUEST
            if message.startswith(bad_request_markers)
            else status.HTTP_404_NOT_FOUND
        )
        raise HTTPException(status_code=status_code, detail=message) from exc
    return DiscoveryCandidateDecisionResponse(
        candidate=DiscoveryCandidateRead.model_validate(candidate),
        message="Discovery candidate decision saved.",
    )
