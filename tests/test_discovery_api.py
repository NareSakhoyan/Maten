from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db_session
from app.db.models import Document, DocumentPage, DocumentStatus, Occurrence
from app.main import app
from app.services.auth_service import AuthenticatedUser
from app.services.discovery.discovery_candidate_service import DiscoveryCandidateService
from app.utils.token_classification import classify_token
from conftest import PRIMARY_USER_ID


def _seed_document(session: Session) -> tuple[Document, DocumentPage]:
    document = Document(
        id=uuid4(),
        user_id=PRIMARY_USER_ID,
        title="Discovery API Test",
        original_filename="discovery-api.pdf",
        mime_type="application/pdf",
        file_size_bytes=123,
        storage_bucket="book-originals",
        storage_path=f"{PRIMARY_USER_ID}/discovery-api.pdf",
        sha256="a" * 64,
        page_count=1,
        status=DocumentStatus.COMPLETED,
    )
    page = DocumentPage(
        id=uuid4(),
        document_id=document.id,
        page_number=1,
        extraction_method="pdf_text",
        extracted_text="Discovery API page",
        char_count=100,
    )
    session.add_all([document, page])
    session.flush()
    return document, page


def _add_occurrence(
    session: Session,
    *,
    document: Document,
    page: DocumentPage,
    token: str,
    normalized_token: str,
    context: str,
) -> None:
    classification = classify_token(token)
    session.add(
        Occurrence(
            id=uuid4(),
            document_id=document.id,
            page_id=page.id,
            page_number=page.page_number,
            token=token,
            normalized_token=normalized_token,
            script_type=classification.script_type,
            has_digits=classification.has_digits,
            has_latin=classification.has_latin,
            has_armenian=classification.has_armenian,
            token_length=classification.token_length,
            context_snippet=context,
            char_start=0,
            char_end=len(token),
        )
    )
    session.flush()


def test_discovery_candidates_detail_and_decision_api(db_session: Session) -> None:
    document, page = _seed_document(db_session)
    _add_occurrence(
        db_session,
        document=document,
        page=page,
        token="Անծանօթ",
        normalized_token="անծանօթ",
        context="Անծանօթ բառ",
    )
    db_session.commit()

    service = DiscoveryCandidateService()
    service.build_for_document(db_session, user_id=PRIMARY_USER_ID, document_id=document.id)
    candidate, _ = service.list_candidates(
        db_session,
        user_id=PRIMARY_USER_ID,
        document_id=document.id,
        limit=10,
        offset=0,
    )
    assert candidate
    candidate_id = candidate[0].id

    def _current_user() -> AuthenticatedUser:
        return AuthenticatedUser(
            user_id=PRIMARY_USER_ID,
            access_token="test-token",
            email="test@example.com",
        )

    def _session_override():
        yield db_session

    app.dependency_overrides[get_current_user] = _current_user
    app.dependency_overrides[get_db_session] = _session_override
    try:
        client = TestClient(app)
        list_response = client.get(f"/api/v1/documents/{document.id}/discovery/candidates")
        assert list_response.status_code == 200
        assert list_response.json()["items"]

        detail_response = client.get(f"/api/v1/documents/{document.id}/discovery/candidates/{candidate_id}")
        assert detail_response.status_code == 200
        detail_payload = detail_response.json()
        assert detail_payload["candidate"]["id"] == str(candidate_id)
        assert isinstance(detail_payload["provider_evidence"], list)
        assert isinstance(detail_payload["occurrence_evidence"], list)

        decision_response = client.post(
            f"/api/v1/documents/{document.id}/discovery/candidates/{candidate_id}/decision",
            json={"decision": "mark_interesting", "note": "api smoke"},
        )
        assert decision_response.status_code == 200
        assert decision_response.json()["candidate"]["reviewer_decision"] == "mark_interesting"
    finally:
        app.dependency_overrides.clear()
