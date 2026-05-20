from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Document, DocumentPage, DocumentStatus, LexiconGroupReview, LexiconGroupReviewStatus
from app.schemas.lexicon import LexiconActionRequest, LexiconActionType, LexiconGroupState
from app.services.lexicon_action_service import LexiconActionService
from app.services.occurrence_service import OccurrenceService
from conftest import PRIMARY_USER_ID


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
        sha256="c" * 64,
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


def test_lexicon_action_ignore_and_unignore(db_session: Session) -> None:
    document, page = _seed_document(db_session, title="Actions")
    OccurrenceService().store_page_occurrences(
        db_session,
        document_id=document.id,
        page_id=page.id,
        page_number=page.page_number,
        text="Երևան",
    )
    db_session.commit()

    service = LexiconActionService()
    ignored = service.run_action(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=LexiconActionRequest(
            action=LexiconActionType.IGNORE,
            normalized_forms=["երևան"],
            reviewer_note="noise",
        ),
    )
    assert ignored.group_state is LexiconGroupState.IGNORED_NOISE
    assert ignored.normalized_forms == ["երևան"]

    review = db_session.scalar(
        select(LexiconGroupReview).where(
            LexiconGroupReview.user_id == str(PRIMARY_USER_ID),
            LexiconGroupReview.normalized_form == "երևան",
        )
    )
    assert review is not None
    assert review.review_status is LexiconGroupReviewStatus.IGNORED_NOISE

    restored = service.run_action(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=LexiconActionRequest(
            action=LexiconActionType.UNIGNORE,
            normalized_forms=["երևան"],
        ),
    )
    assert restored.group_state is LexiconGroupState.UNREVIEWED
    assert (
        db_session.scalar(
            select(LexiconGroupReview).where(
                LexiconGroupReview.user_id == str(PRIMARY_USER_ID),
                LexiconGroupReview.normalized_form == "երևան",
            )
        )
        is None
    )


def test_lexicon_action_create_lexeme(db_session: Session) -> None:
    document, page = _seed_document(db_session, title="Create Action")
    OccurrenceService().store_page_occurrences(
        db_session,
        document_id=document.id,
        page_id=page.id,
        page_number=page.page_number,
        text="Գիրք",
    )
    db_session.commit()

    result = LexiconActionService().run_action(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=LexiconActionRequest(
            action=LexiconActionType.CREATE_LEXEME,
            normalized_forms=["գիրք"],
            canonical_form="Գիրք",
        ),
    )
    assert result.lexeme_id is not None
    assert result.lexeme_canonical_form == "Գիրք"
    assert result.group_state is LexiconGroupState.LINKED
