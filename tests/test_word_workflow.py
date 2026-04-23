from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

from sqlalchemy import delete

from app.api.routers.documents import get_document, list_document_word_candidates
from app.api.routers.reference_sources import get_reference_source, list_reference_source_word_candidates
from app.db.models import ReferenceMatchingDirection
from app.api.routers.words import check_word, get_word_evidence, search_words
from app.db.models import Document, DocumentPage, DocumentStatus, LexemeStatus, Occurrence
from app.schemas.lexeme import LexemeCreateRequest
from app.schemas.reference import ReferenceMatchRunCreateRequest, ReferenceSourceCreateRequest, ReferenceStatusFilter
from app.schemas.word import SourceWordStatusView, WordEvidenceSourceType, WordSearchCategory, WordSearchMode
from app.services.auth_service import AuthenticatedUser
from app.services.lexeme_service import LexemeService
from app.services.document_service import DocumentService
from app.services.reference_import_service import ReferenceImportService
from app.services.reference_matching_service import ReferenceMatchingService
from app.services.reference_source_service import ReferenceSourceService
from app.services.source_word_review_service import SourceWordReviewService
from app.services.word_evidence_service import WordEvidenceService
from app.services.word_search_service import WordSearchService
from app.utils.token_classification import classify_token
from conftest import PRIMARY_USER_ID, SECONDARY_USER_ID


def _current_user(user_id: UUID = PRIMARY_USER_ID) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id,
        access_token="test-token",
        email="test@example.com",
    )


def _seed_document(db_session, *, user_id: UUID, title: str) -> tuple[Document, DocumentPage, DocumentPage]:
    document = Document(
        id=uuid4(),
        user_id=user_id,
        title=title,
        original_filename=f"{title}.pdf",
        mime_type="application/pdf",
        file_size_bytes=123,
        storage_bucket="book-originals",
        storage_path=f"{user_id}/{title}.pdf",
        sha256="a" * 64,
        page_count=2,
        status=DocumentStatus.COMPLETED,
    )
    page_one = DocumentPage(
        id=uuid4(),
        document_id=document.id,
        page_number=1,
        extraction_method="pdf_text",
        extracted_text=f"{title} page one",
        char_count=120,
    )
    page_two = DocumentPage(
        id=uuid4(),
        document_id=document.id,
        page_number=2,
        extraction_method="ocr",
        extracted_text=f"{title} page two",
        char_count=90,
    )
    db_session.add_all([document, page_one, page_two])
    db_session.flush()
    return document, page_one, page_two


def _add_occurrence(db_session, *, document: Document, page: DocumentPage, token: str, normalized_token: str, snippet: str):
    classification = classify_token(token)
    db_session.add(
        Occurrence(
            id=uuid4(),
            document_id=document.id,
            page_id=page.id,
            lexeme_id=None,
            page_number=page.page_number,
            token=token,
            normalized_token=normalized_token,
            script_type=classification.script_type,
            has_digits=classification.has_digits,
            has_latin=classification.has_latin,
            has_armenian=classification.has_armenian,
            token_length=classification.token_length,
            context_snippet=snippet,
            char_start=0,
            char_end=len(token),
        )
    )
    db_session.flush()


def _seed_workspace(db_session):
    source_service = ReferenceSourceService()
    import_service = ReferenceImportService()
    matching_service = ReferenceMatchingService()
    lexeme_service = LexemeService(reference_matching_service=matching_service)

    source_detail = source_service.create_source(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceSourceCreateRequest(display_name="Evidence Source"),
    )
    source = source_service.get_user_source(db_session, user_id=PRIMARY_USER_ID, source_id=source_detail.id)
    assert source is not None
    import_service.import_entries(
        db_session,
        source=source,
        filename="evidence.txt",
        content="հայաստան\nբառ\n".encode("utf-8"),
    )

    document, page_one, page_two = _seed_document(db_session, user_id=PRIMARY_USER_ID, title="Evidence Book")
    _add_occurrence(
        db_session,
        document=document,
        page=page_one,
        token="Հայաստան",
        normalized_token="հայաստան",
        snippet="Հայաստան հին գիրքում",
    )
    _add_occurrence(
        db_session,
        document=document,
        page=page_two,
        token="Բառ",
        normalized_token="բառ",
        snippet="Բառ OCR էջում",
    )
    _add_occurrence(
        db_session,
        document=document,
        page=page_two,
        token="latin",
        normalized_token="latin",
        snippet="latin suspicious token",
    )
    db_session.commit()

    lexeme = lexeme_service.create_lexeme(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=LexemeCreateRequest(
            canonical_form="Հայաստան",
            normalized_forms=["հայաստան"],
            status=LexemeStatus.CURATED,
        ),
    )

    run = matching_service.create_run(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceMatchRunCreateRequest(
            matching_direction=ReferenceMatchingDirection.INTERNAL_TO_REFERENCE,
            run_scope="all",
        ),
    )
    matching_service.process_run_in_session(db_session, run_id=run.id)
    db_session.commit()
    return {
        "document": document,
        "source": source,
        "lexeme": lexeme,
    }


def test_document_word_candidates_endpoint_and_document_summary(db_session) -> None:
    workspace = _seed_workspace(db_session)
    document = workspace["document"]
    document_service = DocumentService()
    source_word_review_service = SourceWordReviewService()

    response = asyncio.run(
        list_document_word_candidates(
            document_id=document.id,
            search=None,
            status_view=SourceWordStatusView.ALL,
            reference_status=ReferenceStatusFilter.ALL,
            limit=20,
            offset=0,
            current_user=_current_user(),
            session=db_session,
            document_service=document_service,
            source_word_review_service=source_word_review_service,
        )
    )

    assert response.total == 3
    matched_item = next(item for item in response.items if item.normalized_form == "հայաստան")
    assert matched_item.source_title == "Evidence Book"
    assert matched_item.source_subtitle == "Evidence Book.pdf"
    assert matched_item.sample_pages == [1]
    assert matched_item.sample_contexts == ["Հայաստան հին գիրքում"]
    assert matched_item.linked_lexeme_canonical_form == "Հայաստան"
    suspicious_item = next(item for item in response.items if item.normalized_form == "latin")
    assert suspicious_item.is_suspicious is True
    assert suspicious_item.suspicion_reasons

    document_detail = asyncio.run(
        get_document(
            document_id=document.id,
            current_user=_current_user(),
            session=db_session,
            document_service=document_service,
        )
    )
    assert document_detail.word_candidate_count == 3
    assert document_detail.linked_candidate_count == 1
    assert document_detail.suspicious_candidate_count == 1
    assert document_detail.unmatched_candidate_count >= 1


def test_reference_source_word_candidates_endpoint_and_source_summary(db_session) -> None:
    workspace = _seed_workspace(db_session)
    source = workspace["source"]
    lexeme = workspace["lexeme"]
    reference_source_service = ReferenceSourceService()
    source_word_review_service = SourceWordReviewService()

    response = asyncio.run(
        list_reference_source_word_candidates(
            source_id=source.id,
            search=None,
            reference_status=ReferenceStatusFilter.ALL,
            limit=20,
            offset=0,
            current_user=_current_user(),
            session=db_session,
            reference_source_service=reference_source_service,
            source_word_review_service=source_word_review_service,
        )
    )
    assert response.total == 2
    assert response.source.source_id == str(source.id)
    assert response.source.source_title == "Evidence Source"
    assert response.source.reference_link == f"/reference-sources/{source.id}"
    assert response.source.imported_entry_count == 2
    assert response.source.matched_entry_count == 2
    assert response.source.unmatched_entry_count == 0
    matched_item = next(item for item in response.items if item.normalized_form == "հայաստան")
    assert matched_item.source_title == "Evidence Source"
    assert matched_item.reference_link == f"/reference-sources/{source.id}"
    assert matched_item.linked_lexeme_id == lexeme.id
    assert matched_item.linked_lexeme_canonical_form == "Հայաստան"
    assert matched_item.matching_lexeme_count == 1

    source_detail = asyncio.run(
        get_reference_source(
            source_id=source.id,
            current_user=_current_user(),
            session=db_session,
            reference_source_service=reference_source_service,
        )
    )
    assert source_detail.imported_entry_count == 2
    assert source_detail.matched_entry_count == 2
    assert source_detail.unmatched_entry_count == 0


def test_word_evidence_endpoint_returns_cross_source_evidence(db_session) -> None:
    workspace = _seed_workspace(db_session)
    document = workspace["document"]
    source = workspace["source"]
    lexeme = workspace["lexeme"]
    word_evidence_service = WordEvidenceService()

    response = asyncio.run(
        get_word_evidence(
            normalized_form="հայաստան",
            source_type=None,
            source_id=None,
            limit=50,
            offset=0,
            current_user=_current_user(),
            session=db_session,
            word_evidence_service=word_evidence_service,
        )
    )

    assert response.normalized_form == "հայաստան"
    assert response.summary.total_hits >= 3
    assert response.summary.source_count == 3
    assert response.summary.linked_lexeme_id == lexeme.id
    assert response.related_reference_matches

    by_source_type = {item.source_type for item in response.evidence_items}
    assert WordEvidenceSourceType.IMPORTED_BOOK in by_source_type
    assert WordEvidenceSourceType.REFERENCE_SOURCE in by_source_type
    assert WordEvidenceSourceType.LEXICON in by_source_type

    imported_book_item = next(item for item in response.evidence_items if item.source_type is WordEvidenceSourceType.IMPORTED_BOOK)
    assert imported_book_item.source_id == str(document.id)
    assert imported_book_item.source_title == "Evidence Book"
    assert imported_book_item.page_number == 1
    assert imported_book_item.context_snippet == "Հայաստան հին գիրքում"
    assert imported_book_item.reference_link == f"/documents/{document.id}?page=1"

    reference_item = next(item for item in response.evidence_items if item.source_type is WordEvidenceSourceType.REFERENCE_SOURCE)
    assert reference_item.source_id == str(source.id)
    assert reference_item.reference_link == f"/reference-sources/{source.id}"
    assert reference_item.reference_entry_id is not None
    assert reference_item.source_import_method is not None


def test_source_first_reference_status_overrides_legacy_match_fallback(db_session) -> None:
    source_service = ReferenceSourceService()
    import_service = ReferenceImportService()
    matching_service = ReferenceMatchingService()
    source_word_review_service = SourceWordReviewService()

    source_detail = source_service.create_source(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceSourceCreateRequest(display_name="Authoritative Source"),
    )
    source = source_service.get_user_source(db_session, user_id=PRIMARY_USER_ID, source_id=source_detail.id)
    assert source is not None
    import_service.import_entries(
        db_session,
        source=source,
        filename="authoritative.txt",
        content="հայաստան\n".encode("utf-8"),
    )

    document, page_one, _ = _seed_document(db_session, user_id=PRIMARY_USER_ID, title="Legacy Match Book")
    _add_occurrence(
        db_session,
        document=document,
        page=page_one,
        token="Հայաստան",
        normalized_token="հայաստան",
        snippet="Legacy matched evidence",
    )
    legacy_run = matching_service.create_run(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceMatchRunCreateRequest(
            matching_direction=ReferenceMatchingDirection.INTERNAL_TO_REFERENCE,
            run_scope="all",
        ),
    )
    matching_service.process_run_in_session(db_session, run_id=legacy_run.id)

    db_session.execute(delete(Occurrence).where(Occurrence.document_id == document.id))
    source_first_run = matching_service.create_run(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceMatchRunCreateRequest(source_id=source.id),
    )
    matching_service.process_run_in_session(db_session, run_id=source_first_run.id)
    db_session.commit()

    response = asyncio.run(
        list_reference_source_word_candidates(
            source_id=source.id,
            search=None,
            reference_status=ReferenceStatusFilter.UNMATCHED,
            limit=20,
            offset=0,
            current_user=_current_user(),
            session=db_session,
            reference_source_service=source_service,
            source_word_review_service=source_word_review_service,
        )
    )

    assert response.source.matched_entry_count == 0
    assert response.source.unmatched_entry_count == 1
    assert response.total == 1
    assert response.items[0].normalized_form == "հայաստան"


def test_global_word_search_and_quick_check(db_session) -> None:
    _seed_workspace(db_session)
    word_search_service = WordSearchService()

    search_response = asyncio.run(
        search_words(
            q="Հայաստան",
            mode=WordSearchMode.NORMALIZED,
            include_categories=[
                WordSearchCategory.LEXICON,
                WordSearchCategory.IMPORTED_BOOKS,
                WordSearchCategory.REFERENCE_SOURCES,
            ],
            limit_per_category=20,
            current_user=_current_user(),
            session=db_session,
            word_search_service=word_search_service,
        )
    )
    groups = {group.category: group for group in search_response.groups}
    assert groups[WordSearchCategory.LEXICON].total >= 1
    assert groups[WordSearchCategory.IMPORTED_BOOKS].total >= 1
    assert groups[WordSearchCategory.REFERENCE_SOURCES].total >= 1
    imported_item = groups[WordSearchCategory.IMPORTED_BOOKS].items[0]
    assert imported_item.source_title == "Evidence Book"
    assert imported_item.page_number is not None
    assert imported_item.context_snippet

    check_response = asyncio.run(
        check_word(
            q="Հայաստան",
            current_user=_current_user(),
            session=db_session,
            word_search_service=word_search_service,
        )
    )
    assert check_response.exists_in_lexicon is True
    assert check_response.matching_lexeme_count == 1
    assert check_response.found_in_imported_books is True
    assert check_response.found_in_reference_sources is True


def test_new_word_endpoints_are_user_scoped(db_session) -> None:
    _seed_workspace(db_session)
    source_service = ReferenceSourceService()
    import_service = ReferenceImportService()
    lexeme_service = LexemeService()
    word_search_service = WordSearchService()
    word_evidence_service = WordEvidenceService()

    secondary_source_detail = source_service.create_source(
        db_session,
        user_id=SECONDARY_USER_ID,
        request=ReferenceSourceCreateRequest(display_name="Secondary Evidence Source"),
    )
    secondary_source = source_service.get_user_source(
        db_session,
        user_id=SECONDARY_USER_ID,
        source_id=secondary_source_detail.id,
    )
    assert secondary_source is not None
    import_service.import_entries(
        db_session,
        source=secondary_source,
        filename="secondary.txt",
        content="գաղտնի\n".encode("utf-8"),
    )
    secondary_document, secondary_page, _ = _seed_document(db_session, user_id=SECONDARY_USER_ID, title="Secondary Book")
    _add_occurrence(
        db_session,
        document=secondary_document,
        page=secondary_page,
        token="Գաղտնի",
        normalized_token="գաղտնի",
        snippet="secondary only word",
    )
    lexeme_service.create_lexeme(
        db_session,
        user_id=SECONDARY_USER_ID,
        request=LexemeCreateRequest(
            canonical_form="Գաղտնի",
            normalized_forms=["գաղտնի"],
            status=LexemeStatus.DRAFT,
        ),
    )
    db_session.commit()

    primary_search = asyncio.run(
        search_words(
            q="գաղտնի",
            mode=WordSearchMode.NORMALIZED,
            include_categories=[
                WordSearchCategory.LEXICON,
                WordSearchCategory.IMPORTED_BOOKS,
                WordSearchCategory.REFERENCE_SOURCES,
            ],
            limit_per_category=20,
            current_user=_current_user(),
            session=db_session,
            word_search_service=word_search_service,
        )
    )
    assert all(group.total == 0 for group in primary_search.groups)

    secondary_search = asyncio.run(
        search_words(
            q="գաղտնի",
            mode=WordSearchMode.NORMALIZED,
            include_categories=[
                WordSearchCategory.LEXICON,
                WordSearchCategory.IMPORTED_BOOKS,
                WordSearchCategory.REFERENCE_SOURCES,
            ],
            limit_per_category=20,
            current_user=_current_user(user_id=SECONDARY_USER_ID),
            session=db_session,
            word_search_service=word_search_service,
        )
    )
    assert any(group.total > 0 for group in secondary_search.groups)

    primary_evidence = asyncio.run(
        get_word_evidence(
            normalized_form="գաղտնի",
            source_type=None,
            source_id=None,
            limit=50,
            offset=0,
            current_user=_current_user(),
            session=db_session,
            word_evidence_service=word_evidence_service,
        )
    )
    assert primary_evidence.summary.total_hits == 0
