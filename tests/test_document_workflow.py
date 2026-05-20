from __future__ import annotations

from uuid import uuid4

from sqlalchemy.orm import Session

from app.db.models import Document, DocumentPage, DocumentStatus, DocumentWorkflowStage
from app.schemas.lexeme import LexemeCreateRequest
from app.services.document_workflow_service import DocumentWorkflowService
from app.services.lexeme_service import LexemeService
from app.services.occurrence_service import OccurrenceService
from app.utils.token_classification import classify_token
from conftest import PRIMARY_USER_ID, rebuild_lexicon_index_for_document


def _seed_document(session: Session, *, title: str) -> tuple[Document, DocumentPage]:
    document = Document(
        id=uuid4(),
        user_id=PRIMARY_USER_ID,
        title=title,
        original_filename=f"{title}.pdf",
        mime_type="application/pdf",
        file_size_bytes=123,
        storage_bucket="book-originals",
        storage_path=f"{PRIMARY_USER_ID}/{title}.pdf",
        sha256="a" * 64,
        page_count=1,
        status=DocumentStatus.COMPLETED,
    )
    page = DocumentPage(
        id=uuid4(),
        document_id=document.id,
        page_number=1,
        extraction_method="ocr",
        extracted_text=f"{title} page",
        char_count=100,
    )
    session.add_all([document, page])
    session.flush()
    return document, page


def test_workflow_ready_for_review_after_candidates(db_session: Session) -> None:
    session = db_session
    document, page = _seed_document(session, title="Workflow Ready")
    classification = classify_token("Հայաստան")
    OccurrenceService().store_page_occurrences(
        session,
        document_id=document.id,
        page_id=page.id,
        page_number=page.page_number,
        text="Հայաստան",
    )
    session.commit()
    rebuild_lexicon_index_for_document(session, user_id=PRIMARY_USER_ID, document=document)

    workflow = DocumentWorkflowService().sync_for_document(session, document_id=document.id)
    assert workflow is not None
    assert workflow.stage is DocumentWorkflowStage.READY_FOR_REVIEW
    assert workflow.candidate_count >= 1


def test_workflow_moves_to_in_review_after_lexeme_create(db_session: Session) -> None:
    session = db_session
    document, page = _seed_document(session, title="Workflow Linked")
    OccurrenceService().store_page_occurrences(
        session,
        document_id=document.id,
        page_id=page.id,
        page_number=page.page_number,
        text="Երևան Հայաստան",
    )
    session.commit()
    rebuild_lexicon_index_for_document(session, user_id=PRIMARY_USER_ID, document=document)

    LexemeService().create_lexeme(
        session,
        user_id=PRIMARY_USER_ID,
        request=LexemeCreateRequest(
            canonical_form="Երևան",
            normalized_forms=["երևան"],
            status="draft",
        ),
    )

    workflow = DocumentWorkflowService().sync_for_document(session, document_id=document.id)
    assert workflow is not None
    assert workflow.stage is DocumentWorkflowStage.IN_REVIEW
    assert workflow.linked_count >= 1
    assert workflow.candidate_count >= 1
