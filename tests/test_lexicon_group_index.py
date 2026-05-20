from __future__ import annotations

from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Document, DocumentPage, DocumentStatus, LexiconGroupIndex, LexiconGroupIndexDocument, Occurrence
from app.schemas.lexeme import LexemeCreateRequest
from app.schemas.lexicon import LexiconGroupView
from app.services.lexeme_service import LexemeService
from app.services.lexicon_group_index_service import LexiconGroupIndexService
from app.services.lexicon_service import LexiconService
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
        sha256="b" * 64,
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


def test_index_backfill_and_list_groups(db_session: Session) -> None:
    document, page = _seed_document(db_session, title="Index List")
    occurrences = OccurrenceService().store_page_occurrences(
        db_session,
        document_id=document.id,
        page_id=page.id,
        page_number=page.page_number,
        text="Հայաստան Երևան",
    )
    db_session.commit()

    index_service = LexiconGroupIndexService()
    occurrence_count = db_session.scalar(
        select(func.count()).select_from(Occurrence).where(Occurrence.document_id == document.id)
    )
    assert occurrence_count and occurrence_count > 0

    index_service.rebuild_document(
        db_session,
        user_id=PRIMARY_USER_ID,
        document_id=document.id,
        document_title=document.title,
    )
    db_session.flush()
    slice_count = db_session.scalar(
        select(func.count())
        .select_from(LexiconGroupIndexDocument)
        .where(
            LexiconGroupIndexDocument.user_id == PRIMARY_USER_ID,
            LexiconGroupIndexDocument.document_id == document.id,
        )
    )
    assert slice_count and slice_count > 0, f"expected index slices, occurrences={occurrence_count}"

    global_count = db_session.scalar(
        select(func.count()).select_from(LexiconGroupIndex).where(LexiconGroupIndex.user_id == PRIMARY_USER_ID)
    )
    assert global_count and global_count >= 1

    items, total = LexiconService().list_groups(
        db_session,
        user_id=PRIMARY_USER_ID,
        limit=20,
        offset=0,
        view=LexiconGroupView.CANDIDATES,
        document_id=document.id,
    )
    assert total >= 1
    assert any(item.normalized_form == occurrences[0].normalized_token for item in items)


def test_incremental_page_index_without_full_rebuild(db_session: Session) -> None:
    document, page = _seed_document(db_session, title="Incremental")
    index_service = LexiconGroupIndexService()

    first_occurrences = OccurrenceService().store_page_occurrences(
        db_session,
        document_id=document.id,
        page_id=page.id,
        page_number=page.page_number,
        text="Երևան",
    )
    index_service.apply_page_occurrences(
        db_session,
        user_id=PRIMARY_USER_ID,
        document_id=document.id,
        document_title=document.title,
        page_id=page.id,
        occurrences=first_occurrences,
    )
    db_session.commit()

    normalized_form = first_occurrences[0].normalized_token

    second_page = DocumentPage(
        id=uuid4(),
        document_id=document.id,
        page_number=2,
        extraction_method="ocr",
        extracted_text="Երևան again",
        char_count=100,
    )
    db_session.add(second_page)
    db_session.flush()

    second_occurrences = OccurrenceService().store_page_occurrences(
        db_session,
        document_id=document.id,
        page_id=second_page.id,
        page_number=second_page.page_number,
        text="Երևան",
    )
    index_service.apply_page_occurrences(
        db_session,
        user_id=PRIMARY_USER_ID,
        document_id=document.id,
        document_title=document.title,
        page_id=second_page.id,
        occurrences=second_occurrences,
    )
    db_session.commit()

    row = db_session.get(
        LexiconGroupIndex,
        {"user_id": PRIMARY_USER_ID, "normalized_form": normalized_form},
    )
    assert row is not None
    assert row.occurrence_count >= 2
    assert row.document_count == 1

    slice_row = db_session.get(
        LexiconGroupIndexDocument,
        {
            "user_id": PRIMARY_USER_ID,
            "normalized_form": normalized_form,
            "document_id": document.id,
        },
    )
    assert slice_row is not None
    assert slice_row.script_counts


def test_clear_document_index_removes_slices_and_updates_globals(db_session: Session) -> None:
    document, page = _seed_document(db_session, title="Clear Index")
    occurrences = OccurrenceService().store_page_occurrences(
        db_session,
        document_id=document.id,
        page_id=page.id,
        page_number=page.page_number,
        text="Երևան",
    )
    index_service = LexiconGroupIndexService()
    index_service.apply_page_occurrences(
        db_session,
        user_id=PRIMARY_USER_ID,
        document_id=document.id,
        document_title=document.title,
        page_id=page.id,
        occurrences=occurrences,
    )
    db_session.commit()

    index_service.clear_document_index(
        db_session,
        user_id=PRIMARY_USER_ID,
        document_id=document.id,
    )
    db_session.commit()

    slice_count = db_session.scalar(
        select(func.count())
        .select_from(LexiconGroupIndexDocument)
        .where(
            LexiconGroupIndexDocument.user_id == PRIMARY_USER_ID,
            LexiconGroupIndexDocument.document_id == document.id,
        )
    )
    assert slice_count == 0
    global_row = db_session.get(
        LexiconGroupIndex,
        {"user_id": PRIMARY_USER_ID, "normalized_form": occurrences[0].normalized_token},
    )
    assert global_row is None


def test_list_groups_empty_without_index_rows(db_session: Session) -> None:
    items, total = LexiconService().list_groups(
        db_session,
        user_id=PRIMARY_USER_ID,
        limit=20,
        offset=0,
    )
    assert total == 0
    assert items == []


def test_index_metadata_updates_on_lexeme_create(db_session: Session) -> None:
    document, page = _seed_document(db_session, title="Index Lexeme")
    OccurrenceService().store_page_occurrences(
        db_session,
        document_id=document.id,
        page_id=page.id,
        page_number=page.page_number,
        text="Երևան",
    )
    db_session.commit()

    LexiconGroupIndexService().rebuild_document(
        db_session,
        user_id=PRIMARY_USER_ID,
        document_id=document.id,
        document_title=document.title,
    )
    db_session.commit()

    LexemeService().create_lexeme(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=LexemeCreateRequest(
            canonical_form="Երևան",
            normalized_forms=["երևան"],
            status="draft",
        ),
    )

    row = db_session.get(LexiconGroupIndex, {"user_id": PRIMARY_USER_ID, "normalized_form": "երևան"})
    assert row is not None
    assert row.group_state == "linked"
    assert row.linked_lexeme_canonical_form == "Երևան"
