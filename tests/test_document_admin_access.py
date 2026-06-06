from __future__ import annotations

from uuid import uuid4

from app.db.models import Document, DocumentPage, DocumentStatus, ExtractionMethod
from app.services.document_service import DocumentService

from .conftest import PRIMARY_USER_ID, SECONDARY_USER_ID


def _document(user_id, title: str) -> Document:
    return Document(
        user_id=user_id,
        title=title,
        original_filename=f"{title}.pdf",
        mime_type="application/pdf",
        file_size_bytes=123,
        storage_bucket="documents",
        storage_path=f"{user_id}/{uuid4()}.pdf",
        sha256=uuid4().hex + uuid4().hex,
        page_count=1,
        status=DocumentStatus.COMPLETED,
    )


def test_document_list_is_owner_scoped_by_default(db_session):
    service = DocumentService()
    own_document = _document(PRIMARY_USER_ID, "own")
    other_document = _document(SECONDARY_USER_ID, "other")
    db_session.add_all([own_document, other_document])
    db_session.commit()

    items, total = service.list_documents(
        db_session,
        user_id=PRIMARY_USER_ID,
        limit=20,
        offset=0,
    )

    assert total == 1
    assert [item.id for item in items] == [own_document.id]


def test_admin_document_list_can_include_all_users(db_session):
    service = DocumentService()
    own_document = _document(PRIMARY_USER_ID, "own")
    other_document = _document(SECONDARY_USER_ID, "other")
    db_session.add_all([own_document, other_document])
    db_session.commit()

    items, total = service.list_documents(
        db_session,
        user_id=PRIMARY_USER_ID,
        limit=20,
        offset=0,
        include_all_users=True,
    )

    assert total == 2
    assert {item.id for item in items} == {own_document.id, other_document.id}


def test_admin_can_read_other_user_document_and_pages(db_session):
    service = DocumentService()
    other_document = _document(SECONDARY_USER_ID, "other")
    db_session.add(other_document)
    db_session.flush()
    page = DocumentPage(
        document_id=other_document.id,
        page_number=1,
        text="Բարեւ",
        extraction_method=ExtractionMethod.PDF_TEXT,
        confidence=None,
    )
    db_session.add(page)
    db_session.commit()

    assert (
        service.get_user_document(
            db_session,
            user_id=PRIMARY_USER_ID,
            document_id=other_document.id,
        )
        is None
    )
    assert service.get_user_document(
        db_session,
        user_id=PRIMARY_USER_ID,
        document_id=other_document.id,
        include_all_users=True,
    )

    pages, total = service.list_document_pages(
        db_session,
        user_id=PRIMARY_USER_ID,
        document_id=other_document.id,
        limit=20,
        offset=0,
        include_all_users=True,
    )

    assert total == 1
    assert [item.id for item in pages] == [page.id]
