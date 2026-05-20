from __future__ import annotations

import asyncio
from contextlib import contextmanager
from io import BytesIO
from zipfile import ZipFile
from uuid import UUID, uuid4

import fitz
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Document,
    DocumentPage,
    DocumentStatus,
    LexemeStatus,
    Occurrence,
    ReferenceEntry,
    ReferenceImportMethod,
    ReferenceMatchingDirection,
    ReferenceMatch,
    ReferenceMatchRun,
    ReferenceMatchRunResult,
    ReferenceMatchRunScope,
    ReferenceMatchRunStatus,
    ReferenceMatchStatus,
    ReferenceMatchTargetScope,
    ReferenceMatchTargetType,
    ReferenceMatchType,
)
import app.services.reference_matching_service as reference_matching_service_module
from app.api.routers.reference_matching import get_reference_matching_run_result, list_reference_matching_run_results
from app.api.routers.reference_sources import get_reference_source, list_reference_source_entries
from app.services.auth_service import AuthenticatedUser
from app.schemas.lexeme import LexemeCreateRequest
from app.schemas.lexicon import LexiconGroupView
from app.schemas.reference import (
    ReferenceMatchRunCreateRequest,
    ReferenceMatchRunEntryResultScopeFilter,
    ReferenceSourceCreateRequest,
    ReferenceStatusFilter,
)
from app.schemas.reference_enums import SupportedReferenceImportMethod
from app.services.lexeme_service import LexemeService
from app.services.reference_import_service import ReferenceImportService
from app.services.reference_matching_service import ReferenceMatchingService
from app.services.reference_source_service import ReferenceSourceService
from app.utils.token_classification import classify_token
from conftest import PRIMARY_USER_ID, SECONDARY_USER_ID, rebuild_lexicon_index_for_document


def _current_user(user_id: UUID = PRIMARY_USER_ID) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id,
        access_token="test-token",
        email="test@example.com",
    )


class StubOCRService:
    def __init__(self, text: str) -> None:
        self.text = text

    def image_to_text(self, image_bytes: bytes) -> str:
        return self.text


def _seed_document(
    session: Session,
    *,
    user_id: UUID,
    title: str,
    page_number: int = 1,
) -> tuple[Document, DocumentPage]:
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
        page_count=1,
        status=DocumentStatus.COMPLETED,
    )
    page = DocumentPage(
        id=uuid4(),
        document_id=document.id,
        page_number=page_number,
        extraction_method="ocr",
        page_image_bucket=None,
        page_image_path=None,
        extracted_text=f"{title} page",
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
    context_snippet: str,
) -> Occurrence:
    classification = classify_token(token)
    occurrence = Occurrence(
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
        context_snippet=context_snippet,
        char_start=0,
        char_end=len(token),
    )
    session.add(occurrence)
    session.flush()
    return occurrence


def _build_docx_bytes(*paragraphs: str) -> bytes:
    document_xml = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>',
    ]
    for paragraph in paragraphs:
        document_xml.append(f"<w:p><w:r><w:t>{paragraph}</w:t></w:r></w:p>")
    document_xml.append("</w:body></w:document>")

    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "")
        archive.writestr("word/document.xml", "".join(document_xml))
    return buffer.getvalue()


def _build_pdf_bytes(*lines: str) -> bytes:
    pdf = fitz.open()
    page = pdf.new_page()
    text = "\n".join(lines)
    if text:
        page.insert_text((72, 72), text)
    data = pdf.tobytes()
    pdf.close()
    return data


def test_import_txt_reference_source(db_session: Session) -> None:
    source_service = ReferenceSourceService()
    import_service = ReferenceImportService()

    source_detail = source_service.create_source(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceSourceCreateRequest(display_name="Personal Wordlist"),
    )
    source = source_service.get_user_source(db_session, user_id=PRIMARY_USER_ID, source_id=source_detail.id)
    assert source is not None

    response = import_service.import_entries(
        db_session,
        source=source,
        filename="reference.txt",
        content="Հայաստան\nՀայաստան\nԳիրք\n\n".encode("utf-8"),
    )

    assert response.rows_read == 4
    assert response.rows_imported == 2
    assert response.rows_skipped == 2
    assert response.import_method is SupportedReferenceImportMethod.TXT
    assert response.warning_message is None
    assert response.source_display_name == "Personal Wordlist"
    assert {item.key for item in source_service.list_sources(db_session, user_id=PRIMARY_USER_ID)} >= {
        "manual_reference",
        "personal_wordlist",
    }


def test_import_csv_reference_source(db_session: Session) -> None:
    source_service = ReferenceSourceService()
    import_service = ReferenceImportService()

    source_detail = source_service.create_source(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceSourceCreateRequest(display_name="CSV Source"),
    )
    source = source_service.get_user_source(db_session, user_id=PRIMARY_USER_ID, source_id=source_detail.id)
    assert source is not None

    response = import_service.import_entries(
        db_session,
        source=source,
        filename="reference.csv",
        content="surface_form,normalized_form\nՀայաստան,\n,գիրք\nԲառ,բառ\n".encode("utf-8"),
    )

    assert response.rows_read == 3
    assert response.rows_imported == 3
    assert response.rows_skipped == 0
    assert response.import_method is SupportedReferenceImportMethod.CSV
    assert response.warning_message is None


def test_import_csv_reference_source_with_single_column_fallback(db_session: Session) -> None:
    source_service = ReferenceSourceService()
    import_service = ReferenceImportService()

    source_detail = source_service.create_source(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceSourceCreateRequest(display_name="Single Column CSV Source"),
    )
    source = source_service.get_user_source(db_session, user_id=PRIMARY_USER_ID, source_id=source_detail.id)
    assert source is not None

    response = import_service.import_entries(
        db_session,
        source=source,
        filename="reference.csv",
        content="word\nՀայաստան\nԲառ\n".encode("utf-8"),
    )

    assert response.rows_read == 2
    assert response.rows_imported == 2
    assert response.rows_skipped == 0
    assert response.import_method is SupportedReferenceImportMethod.CSV


def test_import_csv_single_column_fallback_skips_sentence_rows(db_session: Session) -> None:
    source_service = ReferenceSourceService()
    import_service = ReferenceImportService()

    source_detail = source_service.create_source(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceSourceCreateRequest(display_name="Sentence CSV Source"),
    )
    source = source_service.get_user_source(db_session, user_id=PRIMARY_USER_ID, source_id=source_detail.id)
    assert source is not None

    response = import_service.import_entries(
        db_session,
        source=source,
        filename="reference.csv",
        content="term\nՀայաստան\nՍա ամբողջական նախադասություն է։\n".encode("utf-8"),
    )

    assert response.rows_read == 2
    assert response.rows_imported == 1
    assert response.rows_skipped == 1


def test_import_docx_reference_source(db_session: Session) -> None:
    source_service = ReferenceSourceService()
    import_service = ReferenceImportService()

    source_detail = source_service.create_source(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceSourceCreateRequest(display_name="DOCX Source"),
    )
    source = source_service.get_user_source(db_session, user_id=PRIMARY_USER_ID, source_id=source_detail.id)
    assert source is not None

    response = import_service.import_entries(
        db_session,
        source=source,
        filename="reference.docx",
        content=_build_docx_bytes("Հայաստան", "Գիրք"),
    )

    assert response.import_method is SupportedReferenceImportMethod.DOCX
    assert response.rows_read == 2
    assert response.rows_imported == 2
    assert response.warning_message is None


def test_import_pdf_direct_text_reference_source(db_session: Session) -> None:
    source_service = ReferenceSourceService()
    import_service = ReferenceImportService()
    import_service.pdf_parser.settings.reference_pdf_text_min_length = 5

    source_detail = source_service.create_source(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceSourceCreateRequest(display_name="PDF Text Source"),
    )
    source = source_service.get_user_source(db_session, user_id=PRIMARY_USER_ID, source_id=source_detail.id)
    assert source is not None

    response = import_service.import_entries(
        db_session,
        source=source,
        filename="reference.pdf",
        content=_build_pdf_bytes("dictionary", "archive", "entry"),
    )

    assert response.import_method is SupportedReferenceImportMethod.PDF_TEXT
    assert response.warning_message is None
    assert response.rows_imported >= 2


def test_scanned_pdf_falls_back_to_ocr_and_stores_warning(db_session: Session) -> None:
    source_service = ReferenceSourceService()
    import_service = ReferenceImportService(ocr_service=StubOCRService("Հայաստան\nԳիրք\n"))

    source_detail = source_service.create_source(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceSourceCreateRequest(display_name="Scanned PDF Source"),
    )
    source = source_service.get_user_source(db_session, user_id=PRIMARY_USER_ID, source_id=source_detail.id)
    assert source is not None

    response = import_service.import_entries(
        db_session,
        source=source,
        filename="reference.pdf",
        content=_build_pdf_bytes(""),
    )

    assert response.import_method is SupportedReferenceImportMethod.PDF_OCR
    assert response.warning_message is not None
    assert "OCR noise" in response.warning_message

    detail = source_service.get_source_detail(db_session, user_id=PRIMARY_USER_ID, source_id=source.id)
    assert detail is not None
    assert detail.last_import_method is SupportedReferenceImportMethod.PDF_OCR
    assert detail.last_import_warning == response.warning_message
    assert detail.last_imported_at is not None
    assert detail.entry_count == 2


def test_normalized_reference_entry_generation(db_session: Session) -> None:
    source_service = ReferenceSourceService()
    import_service = ReferenceImportService()

    source = source_service.get_user_source(
        db_session,
        user_id=PRIMARY_USER_ID,
        source_id=source_service.create_source(
            db_session,
            user_id=PRIMARY_USER_ID,
            request=ReferenceSourceCreateRequest(display_name="Normalize Source"),
        ).id,
    )
    assert source is not None

    response = import_service.import_entries(
        db_session,
        source=source,
        filename="reference.txt",
        content="Հայաստան\n".encode("utf-8"),
    )
    assert response.rows_imported == 1

    entry = db_session.scalar(select(ReferenceEntry).where(ReferenceEntry.source_id == source.id))
    assert entry is not None
    assert entry.surface_form == "Հայաստան"
    assert entry.normalized_form == "հայաստան"


def test_exact_match_for_lexicon_group(db_session: Session) -> None:
    source_service = ReferenceSourceService()
    import_service = ReferenceImportService()
    matching_service = ReferenceMatchingService()

    source_detail = source_service.create_source(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceSourceCreateRequest(display_name="Exact Source"),
    )
    source = source_service.get_user_source(db_session, user_id=PRIMARY_USER_ID, source_id=source_detail.id)
    assert source is not None
    import_service.import_entries(
        db_session,
        source=source,
        filename="reference.txt",
        content="հայաստան\n".encode("utf-8"),
    )

    document, page = _seed_document(db_session, user_id=PRIMARY_USER_ID, title="exact-group")
    _add_occurrence(
        db_session,
        document=document,
        page=page,
        token="Հայաստան",
        normalized_token="հայաստան",
        context_snippet="exact group",
    )
    db_session.commit()

    response = matching_service.match_group(
        db_session,
        user_id=PRIMARY_USER_ID,
        normalized_form="հայաստան",
    )

    assert response.has_match is True
    assert any(match.match_type.value == "exact" for match in response.matches)
    assert response.matches[0].source_import_method is SupportedReferenceImportMethod.TXT
    assert response.matches[0].source_warning is None


def test_normalized_match_for_lexicon_group(db_session: Session) -> None:
    source_service = ReferenceSourceService()
    import_service = ReferenceImportService()
    matching_service = ReferenceMatchingService()

    source_detail = source_service.create_source(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceSourceCreateRequest(display_name="Normalized Source"),
    )
    source = source_service.get_user_source(db_session, user_id=PRIMARY_USER_ID, source_id=source_detail.id)
    assert source is not None
    import_service.import_entries(
        db_session,
        source=source,
        filename="reference.txt",
        content="Հայաստան\n".encode("utf-8"),
    )

    response = matching_service.match_group(
        db_session,
        user_id=PRIMARY_USER_ID,
        normalized_form="հայաստան",
    )

    assert response.has_match is True
    assert [match.match_type.value for match in response.matches] == ["normalized"]
    assert response.matches[0].source_import_method is SupportedReferenceImportMethod.TXT


def test_fuzzy_match_when_exact_and_normalized_absent(db_session: Session) -> None:
    source_service = ReferenceSourceService()
    import_service = ReferenceImportService()
    matching_service = ReferenceMatchingService()

    source_detail = source_service.create_source(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceSourceCreateRequest(display_name="Fuzzy Source"),
    )
    source = source_service.get_user_source(db_session, user_id=PRIMARY_USER_ID, source_id=source_detail.id)
    assert source is not None
    import_service.import_entries(
        db_session,
        source=source,
        filename="reference.txt",
        content="dictionarry\n".encode("utf-8"),
    )

    response = matching_service.match_group(
        db_session,
        user_id=PRIMARY_USER_ID,
        normalized_form="dictionary",
        allow_fuzzy=True,
    )

    assert response.has_match is True
    assert [match.match_type.value for match in response.matches] == ["fuzzy"]
    assert response.matches[0].match_score is not None
    assert response.matches[0].match_score >= 90


def test_ocr_derived_sources_disable_fuzzy_matching_and_expose_warning(db_session: Session) -> None:
    source_service = ReferenceSourceService()
    import_service = ReferenceImportService(ocr_service=StubOCRService("dictionarry\n"))
    matching_service = ReferenceMatchingService()

    source_detail = source_service.create_source(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceSourceCreateRequest(display_name="OCR Fuzzy Source"),
    )
    source = source_service.get_user_source(db_session, user_id=PRIMARY_USER_ID, source_id=source_detail.id)
    assert source is not None
    import_service.import_entries(
        db_session,
        source=source,
        filename="reference.pdf",
        content=_build_pdf_bytes(""),
    )

    response = matching_service.match_group(
        db_session,
        user_id=PRIMARY_USER_ID,
        normalized_form="dictionary",
        allow_fuzzy=True,
    )

    assert response.has_match is False


def test_ocr_derived_source_exact_match_is_still_allowed(db_session: Session) -> None:
    source_service = ReferenceSourceService()
    import_service = ReferenceImportService(ocr_service=StubOCRService("Հայաստան\n"))
    matching_service = ReferenceMatchingService()

    source_detail = source_service.create_source(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceSourceCreateRequest(display_name="OCR Exact Source"),
    )
    source = source_service.get_user_source(db_session, user_id=PRIMARY_USER_ID, source_id=source_detail.id)
    assert source is not None
    import_service.import_entries(
        db_session,
        source=source,
        filename="reference.pdf",
        content=_build_pdf_bytes(""),
    )

    response = matching_service.match_group(
        db_session,
        user_id=PRIMARY_USER_ID,
        normalized_form="հայաստան",
    )

    assert response.has_match is True
    assert response.matches[0].match_type.value == "normalized"
    assert response.matches[0].source_import_method is SupportedReferenceImportMethod.PDF_OCR
    assert response.matches[0].source_warning is not None


def test_lexeme_match_uses_canonical_normalized_form(db_session: Session) -> None:
    source_service = ReferenceSourceService()
    import_service = ReferenceImportService()
    matching_service = ReferenceMatchingService()
    lexeme_service = LexemeService(reference_matching_service=matching_service)

    source_detail = source_service.create_source(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceSourceCreateRequest(display_name="Lexeme Source"),
    )
    source = source_service.get_user_source(db_session, user_id=PRIMARY_USER_ID, source_id=source_detail.id)
    assert source is not None
    import_service.import_entries(
        db_session,
        source=source,
        filename="reference.txt",
        content="Հայաստան\n".encode("utf-8"),
    )

    lexeme = lexeme_service.create_lexeme(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=LexemeCreateRequest(
            canonical_form="Հայաստան",
            normalized_forms=["այլ"],
            status=LexemeStatus.DRAFT,
        ),
    )
    lexeme_model = lexeme_service.get_user_lexeme(db_session, user_id=PRIMARY_USER_ID, lexeme_id=lexeme.id)
    assert lexeme_model is not None

    response = matching_service.match_lexeme(
        db_session,
        user_id=PRIMARY_USER_ID,
        lexeme=lexeme_model,
    )

    assert response.has_match is True
    assert [match.match_type.value for match in response.matches] == ["normalized"]


def test_batch_run_creation_and_stored_results(db_session: Session) -> None:
    source_service = ReferenceSourceService()
    import_service = ReferenceImportService()
    matching_service = ReferenceMatchingService()
    lexeme_service = LexemeService(reference_matching_service=matching_service)

    source_detail = source_service.create_source(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceSourceCreateRequest(display_name="Batch Source"),
    )
    source = source_service.get_user_source(db_session, user_id=PRIMARY_USER_ID, source_id=source_detail.id)
    assert source is not None
    import_service.import_entries(
        db_session,
        source=source,
        filename="reference.txt",
        content="Հայաստան\nԳիրք\n".encode("utf-8"),
    )

    document, page = _seed_document(db_session, user_id=PRIMARY_USER_ID, title="batch-group")
    _add_occurrence(
        db_session,
        document=document,
        page=page,
        token="Հայաստան",
        normalized_token="հայաստան",
        context_snippet="batch group",
    )
    db_session.commit()

    lexeme = lexeme_service.create_lexeme(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=LexemeCreateRequest(
            canonical_form="Գիրք",
            normalized_forms=["տեքստ"],
            status=LexemeStatus.CURATED,
        ),
    )

    run = matching_service.create_run(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceMatchRunCreateRequest(
            matching_direction=ReferenceMatchingDirection.INTERNAL_TO_REFERENCE,
            run_scope=ReferenceMatchRunScope.ALL,
            view="candidates",
            include_fuzzy=False,
        ),
    )

    matching_service.process_run_in_session(
        db_session,
        run_id=run.id,
        view="candidates",
        include_fuzzy=False,
    )
    db_session.commit()
    db_session.expire_all()

    stored_run = db_session.get(ReferenceMatchRun, run.id)
    assert stored_run is not None
    assert stored_run.status is ReferenceMatchRunStatus.COMPLETED
    assert stored_run.total_items == 2
    assert stored_run.matched_items == 2

    stored_matches = list(
        db_session.scalars(
            select(ReferenceMatch).where(
                ReferenceMatch.user_id == str(PRIMARY_USER_ID),
                ReferenceMatch.target_key.in_(["հայաստան", str(lexeme.id)]),
            )
        )
    )
    assert len(stored_matches) == 2
    assert {match.target_type for match in stored_matches} == {
        ReferenceMatchTargetType.LEXICON_GROUP,
        ReferenceMatchTargetType.LEXEME,
    }


def test_reference_status_filtering(db_session: Session) -> None:
    source_service = ReferenceSourceService()
    import_service = ReferenceImportService()
    matching_service = ReferenceMatchingService()
    lexeme_service = LexemeService(reference_matching_service=matching_service)
    from app.services.lexicon_service import LexiconService

    source_detail = source_service.create_source(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceSourceCreateRequest(display_name="Filter Source"),
    )
    source = source_service.get_user_source(db_session, user_id=PRIMARY_USER_ID, source_id=source_detail.id)
    assert source is not None
    import_service.import_entries(
        db_session,
        source=source,
        filename="reference.txt",
        content="Հայաստան\nԳիրք\n".encode("utf-8"),
    )

    document, page = _seed_document(db_session, user_id=PRIMARY_USER_ID, title="filter-doc")
    _add_occurrence(
        db_session,
        document=document,
        page=page,
        token="Հայաստան",
        normalized_token="հայաստան",
        context_snippet="matched group",
    )
    _add_occurrence(
        db_session,
        document=document,
        page=page,
        token="Անհայտ",
        normalized_token="անհայտ",
        context_snippet="unmatched group",
    )
    db_session.commit()
    rebuild_lexicon_index_for_document(db_session, user_id=PRIMARY_USER_ID, document=document)

    matched_lexeme = lexeme_service.create_lexeme(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=LexemeCreateRequest(
            canonical_form="Գիրք",
            normalized_forms=["խումբ"],
            status=LexemeStatus.DRAFT,
        ),
    )
    lexeme_service.create_lexeme(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=LexemeCreateRequest(
            canonical_form="Անուն",
            normalized_forms=["այլխումբ"],
            status=LexemeStatus.DRAFT,
        ),
    )

    run = matching_service.create_run(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceMatchRunCreateRequest(
            matching_direction=ReferenceMatchingDirection.INTERNAL_TO_REFERENCE,
            run_scope=ReferenceMatchRunScope.ALL,
        ),
    )
    matching_service.process_run_in_session(db_session, run_id=run.id)
    db_session.commit()
    db_session.expire_all()

    lexicon_service = LexiconService(reference_matching_service=matching_service)
    matched_groups, matched_total = lexicon_service.list_groups(
        db_session,
        user_id=PRIMARY_USER_ID,
        limit=20,
        offset=0,
        reference_status=ReferenceStatusFilter.MATCHED,
    )
    unmatched_groups, unmatched_total = lexicon_service.list_groups(
        db_session,
        user_id=PRIMARY_USER_ID,
        limit=20,
        offset=0,
        reference_status=ReferenceStatusFilter.UNMATCHED,
    )
    assert matched_total == 1
    assert unmatched_total == 1
    assert [group.normalized_form for group in matched_groups] == ["հայաստան"]
    assert [group.normalized_form for group in unmatched_groups] == ["անհայտ"]
    assert matched_groups[0].has_reference_match is True
    assert matched_groups[0].reference_match_count == 1

    matched_lexemes, matched_lexeme_total = lexeme_service.list_lexemes(
        db_session,
        user_id=PRIMARY_USER_ID,
        limit=20,
        offset=0,
        reference_status=ReferenceStatusFilter.MATCHED,
    )
    unmatched_lexemes, unmatched_lexeme_total = lexeme_service.list_lexemes(
        db_session,
        user_id=PRIMARY_USER_ID,
        limit=20,
        offset=0,
        reference_status=ReferenceStatusFilter.UNMATCHED,
    )
    assert matched_lexeme_total == 1
    assert unmatched_lexeme_total == 1
    assert [item.id for item in matched_lexemes] == [matched_lexeme.id]


def test_source_to_internal_run_requires_source_id(db_session: Session) -> None:
    matching_service = ReferenceMatchingService()

    with pytest.raises(ValueError, match="source_id is required"):
        matching_service.create_run(
            db_session,
            user_id=PRIMARY_USER_ID,
            request=ReferenceMatchRunCreateRequest(),
        )


def test_reference_matching_run_results_store_one_row_per_reference_entry(db_session: Session) -> None:
    source_service = ReferenceSourceService()
    import_service = ReferenceImportService()
    matching_service = ReferenceMatchingService()

    source_detail = source_service.create_source(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceSourceCreateRequest(display_name="Run Results Source"),
    )
    source = source_service.get_user_source(db_session, user_id=PRIMARY_USER_ID, source_id=source_detail.id)
    assert source is not None
    import_service.import_entries(
        db_session,
        source=source,
        filename="reference.txt",
        content="հայաստան\nգիրք\n".encode("utf-8"),
    )

    document, page = _seed_document(db_session, user_id=PRIMARY_USER_ID, title="run-results-doc")
    _add_occurrence(
        db_session,
        document=document,
        page=page,
        token="հայաստան",
        normalized_token="հայաստան",
        context_snippet="matched group",
    )
    _add_occurrence(
        db_session,
        document=document,
        page=page,
        token="անհայտ",
        normalized_token="անհայտ",
        context_snippet="unmatched group",
    )
    LexemeService(reference_matching_service=matching_service).create_lexeme(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=LexemeCreateRequest(
            canonical_form="գիրք",
            normalized_forms=["գիրք"],
            status=LexemeStatus.CURATED,
        ),
    )
    db_session.commit()

    run = matching_service.create_run(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceMatchRunCreateRequest(
            matching_direction=ReferenceMatchingDirection.SOURCE_TO_INTERNAL,
            source_id=source.id,
            target_scope=ReferenceMatchTargetScope.ALL_INTERNAL,
        ),
    )
    matching_service.process_run_in_session(db_session, run_id=run.id)
    db_session.commit()
    db_session.expire_all()

    stored_run = db_session.get(ReferenceMatchRun, run.id)
    assert stored_run is not None
    assert stored_run.matching_direction is ReferenceMatchingDirection.SOURCE_TO_INTERNAL
    assert stored_run.source_id == source.id
    assert stored_run.target_scope is ReferenceMatchTargetScope.ALL_INTERNAL
    assert stored_run.total_items == 2
    assert stored_run.matched_items == 2
    assert stored_run.unmatched_items == 0
    assert stored_run.exact_match_count == 0
    assert stored_run.normalized_match_count == 2
    assert stored_run.fuzzy_match_count == 0

    result_rows = list(
        db_session.scalars(
            select(ReferenceMatchRunResult)
            .where(ReferenceMatchRunResult.run_id == run.id)
            .order_by(ReferenceMatchRunResult.target_type.asc(), ReferenceMatchRunResult.target_label.asc())
        )
    )
    assert len(result_rows) == 2

    grouped = {
        row.normalized_form: row
        for row in result_rows
    }

    book_match = grouped["հայաստան"]
    assert book_match.matching_direction is ReferenceMatchingDirection.SOURCE_TO_INTERNAL
    assert book_match.target_type is ReferenceMatchTargetType.REFERENCE_ENTRY
    assert book_match.reference_entry_id is not None
    assert book_match.target_label == "հայաստան"
    assert book_match.match_status is ReferenceMatchStatus.MATCHED
    assert book_match.match_count == 1
    assert book_match.exists_in_lexicon is False
    assert book_match.found_in_books is True
    assert book_match.matching_book_occurrence_count == 1
    assert book_match.best_document_title == "run-results-doc"
    assert book_match.best_context_snippet == "matched group"

    lexicon_match = grouped["գիրք"]
    assert lexicon_match.matching_direction is ReferenceMatchingDirection.SOURCE_TO_INTERNAL
    assert lexicon_match.target_type is ReferenceMatchTargetType.REFERENCE_ENTRY
    assert lexicon_match.reference_entry_id is not None
    assert lexicon_match.target_label == "գիրք"
    assert lexicon_match.match_status is ReferenceMatchStatus.MATCHED
    assert lexicon_match.match_count == 1
    assert lexicon_match.exists_in_lexicon is True
    assert lexicon_match.matching_lexeme_count == 1
    assert lexicon_match.best_lexeme_canonical_form == "գիրք"
    assert lexicon_match.found_in_books is False


def test_reference_matching_run_results_endpoints_support_filters_and_detail(db_session: Session) -> None:
    source_service = ReferenceSourceService()
    import_service = ReferenceImportService()
    matching_service = ReferenceMatchingService()
    lexeme_service = LexemeService(reference_matching_service=matching_service)

    source_detail = source_service.create_source(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceSourceCreateRequest(display_name="Run Result Endpoint Source"),
    )
    source = source_service.get_user_source(db_session, user_id=PRIMARY_USER_ID, source_id=source_detail.id)
    assert source is not None
    import_service.import_entries(
        db_session,
        source=source,
        filename="reference.txt",
        content="հայաստան\nգիրք\nաստուած\n".encode("utf-8"),
    )
    entry_rows = list(
        db_session.scalars(
            select(ReferenceEntry)
            .where(ReferenceEntry.source_id == source.id)
            .order_by(ReferenceEntry.normalized_form.asc(), ReferenceEntry.surface_form.asc())
        )
    )
    for entry in entry_rows:
        if entry.normalized_form == "հայաստան":
            entry.metadata_json = {"line_number": 1}
    source.last_import_method = ReferenceImportMethod.PDF_OCR
    source.last_import_warning = "OCR-derived source may contain noise."

    document, page = _seed_document(db_session, user_id=PRIMARY_USER_ID, title="run-results-endpoint")
    _add_occurrence(
        db_session,
        document=document,
        page=page,
        token="հայաստան",
        normalized_token="հայաստան",
        context_snippet="matched group",
    )
    _add_occurrence(
        db_session,
        document=document,
        page=page,
        token="անհայտ",
        normalized_token="անհայտ",
        context_snippet="unmatched group",
    )
    db_session.commit()

    matched_lexeme = lexeme_service.create_lexeme(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=LexemeCreateRequest(
            canonical_form="գիրք",
            normalized_forms=["գիրք"],
            status=LexemeStatus.CURATED,
        ),
    )
    unmatched_lexeme = lexeme_service.create_lexeme(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=LexemeCreateRequest(
            canonical_form="անուն",
            normalized_forms=["այլխումբ"],
            status=LexemeStatus.DRAFT,
        ),
    )

    run = matching_service.create_run(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceMatchRunCreateRequest(
            matching_direction=ReferenceMatchingDirection.SOURCE_TO_INTERNAL,
            source_id=source.id,
            target_scope=ReferenceMatchTargetScope.ALL_INTERNAL,
        ),
    )
    matching_service.process_run_in_session(db_session, run_id=run.id)
    db_session.commit()
    source.last_import_method = ReferenceImportMethod.TXT
    source.last_import_warning = "Later import warning that should not override stored run provenance."
    db_session.commit()

    all_response = asyncio.run(
        list_reference_matching_run_results(
            run_id=run.id,
            match_status=ReferenceStatusFilter.ALL,
            target_scope=ReferenceMatchRunEntryResultScopeFilter.ANY,
            search=None,
            limit=20,
            offset=0,
            current_user=_current_user(),
            session=db_session,
            reference_matching_service=matching_service,
        )
    )
    assert all_response.total == 3
    lexicon_item = next(item for item in all_response.items if item.normalized_form == "գիրք")
    assert lexicon_item.target_label == "գիրք"
    assert lexicon_item.exists_in_lexicon is True
    assert lexicon_item.best_lexeme_canonical_form == "գիրք"
    assert lexicon_item.found_in_books is False
    assert lexicon_item.source_import_method is SupportedReferenceImportMethod.PDF_OCR
    assert lexicon_item.source_warning == "OCR-derived source may contain noise."

    lexicon_matched_response = asyncio.run(
        list_reference_matching_run_results(
            run_id=run.id,
            match_status=ReferenceStatusFilter.MATCHED,
            target_scope=ReferenceMatchRunEntryResultScopeFilter.LEXICON_ONLY,
            search=None,
            limit=20,
            offset=0,
            current_user=_current_user(),
            session=db_session,
            reference_matching_service=matching_service,
        )
    )
    assert [item.normalized_form for item in lexicon_matched_response.items] == ["գիրք"]

    document_matched_response = asyncio.run(
        list_reference_matching_run_results(
            run_id=run.id,
            match_status=ReferenceStatusFilter.MATCHED,
            target_scope=ReferenceMatchRunEntryResultScopeFilter.BOOKS_ONLY,
            search=None,
            limit=20,
            offset=0,
            current_user=_current_user(),
            session=db_session,
            reference_matching_service=matching_service,
        )
    )
    assert [item.normalized_form for item in document_matched_response.items] == ["հայաստան"]

    searched_response = asyncio.run(
        list_reference_matching_run_results(
            run_id=run.id,
            match_status=ReferenceStatusFilter.ALL,
            target_scope=ReferenceMatchRunEntryResultScopeFilter.ANY,
            search="գիր",
            limit=20,
            offset=0,
            current_user=_current_user(),
            session=db_session,
            reference_matching_service=matching_service,
        )
    )
    assert searched_response.total == 1
    assert searched_response.items[0].normalized_form == "գիրք"
    assert searched_response.items[0].best_lexeme_id == matched_lexeme.id

    matched_group_result = next(item for item in all_response.items if item.normalized_form == "հայաստան")
    detail_response = asyncio.run(
        get_reference_matching_run_result(
            run_id=run.id,
            result_id=matched_group_result.id,
            current_user=_current_user(),
            session=db_session,
            reference_matching_service=matching_service,
        )
    )
    assert detail_response.normalized_form == "հայաստան"
    assert detail_response.exists_in_lexicon is False
    assert detail_response.found_in_books is True
    assert detail_response.matching_book_occurrence_count == 1
    assert detail_response.source_entry.source_import_method is SupportedReferenceImportMethod.PDF_OCR
    assert detail_response.source_entry.source_warning == "OCR-derived source may contain noise."
    assert detail_response.source_entry.source_metadata == {"line_number": 1}
    assert [context.document_title for context in detail_response.book_evidence] == ["run-results-endpoint"]
    assert detail_response.matching_lexemes == []

    unmatched_result = next(item for item in all_response.items if item.normalized_form == "աստուած")
    unmatched_detail_response = asyncio.run(
        get_reference_matching_run_result(
            run_id=run.id,
            result_id=unmatched_result.id,
            current_user=_current_user(),
            session=db_session,
            reference_matching_service=matching_service,
        )
    )
    assert unmatched_detail_response.exists_in_lexicon is False
    assert unmatched_detail_response.found_in_books is False
    assert unmatched_detail_response.book_evidence == []
    assert unmatched_detail_response.matching_lexemes == []


def test_reference_source_detail_and_entries_reflect_latest_source_first_run(db_session: Session) -> None:
    source_service = ReferenceSourceService()
    import_service = ReferenceImportService()
    matching_service = ReferenceMatchingService()
    lexeme_service = LexemeService(reference_matching_service=matching_service)

    source_detail = source_service.create_source(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceSourceCreateRequest(display_name="Source Detail Summary"),
    )
    source = source_service.get_user_source(db_session, user_id=PRIMARY_USER_ID, source_id=source_detail.id)
    assert source is not None
    import_service.import_entries(
        db_session,
        source=source,
        filename="reference.txt",
        content="հայաստան\nգիրք\nչկա\n".encode("utf-8"),
    )

    document, page = _seed_document(db_session, user_id=PRIMARY_USER_ID, title="source-detail-doc")
    _add_occurrence(
        db_session,
        document=document,
        page=page,
        token="Հայաստան",
        normalized_token="հայաստան",
        context_snippet="book evidence",
    )
    lexeme_service.create_lexeme(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=LexemeCreateRequest(
            canonical_form="գիրք",
            normalized_forms=["գիրք"],
            status=LexemeStatus.CURATED,
        ),
    )
    db_session.commit()

    run = matching_service.create_run(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceMatchRunCreateRequest(
            matching_direction=ReferenceMatchingDirection.SOURCE_TO_INTERNAL,
            source_id=source.id,
        ),
    )
    matching_service.process_run_in_session(db_session, run_id=run.id)
    db_session.commit()

    source_response = asyncio.run(
        get_reference_source(
            source_id=source.id,
            current_user=_current_user(),
            session=db_session,
            reference_source_service=source_service,
        )
    )
    assert source_response.entry_count == 3
    assert source_response.latest_match_run_id == run.id
    assert source_response.latest_match_run_status == ReferenceMatchRunStatus.COMPLETED.value
    assert source_response.matched_entry_count == 2
    assert source_response.unmatched_entry_count == 1

    entries_response = asyncio.run(
        list_reference_source_entries(
            source_id=source.id,
            search=None,
            match_status=ReferenceStatusFilter.ALL,
            limit=20,
            offset=0,
            current_user=_current_user(),
            session=db_session,
            reference_source_service=source_service,
            reference_matching_service=matching_service,
        )
    )
    assert entries_response.total == 3

    entries_by_form = {item.normalized_form: item for item in entries_response.items}
    assert entries_by_form["հայաստան"].latest_match_status is ReferenceMatchStatus.MATCHED
    assert entries_by_form["հայաստան"].found_in_books is True
    assert entries_by_form["գիրք"].exists_in_lexicon is True
    assert entries_by_form["գիրք"].best_lexeme_canonical_form == "գիրք"
    assert entries_by_form["չկա"].latest_match_status is ReferenceMatchStatus.UNMATCHED
    assert entries_by_form["չկա"].latest_match_count == 0

    unmatched_entries = asyncio.run(
        list_reference_source_entries(
            source_id=source.id,
            search="չ",
            match_status=ReferenceStatusFilter.UNMATCHED,
            limit=20,
            offset=0,
            current_user=_current_user(),
            session=db_session,
            reference_source_service=source_service,
            reference_matching_service=matching_service,
        )
    )
    assert unmatched_entries.total == 1
    assert [item.normalized_form for item in unmatched_entries.items] == ["չկա"]


def test_reference_matching_run_results_are_user_scoped(db_session: Session) -> None:
    source_service = ReferenceSourceService()
    import_service = ReferenceImportService()
    matching_service = ReferenceMatchingService()

    source_detail = source_service.create_source(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceSourceCreateRequest(display_name="Scoped Run Results Source"),
    )
    source = source_service.get_user_source(db_session, user_id=PRIMARY_USER_ID, source_id=source_detail.id)
    assert source is not None
    import_service.import_entries(
        db_session,
        source=source,
        filename="reference.txt",
        content="հայաստան\n".encode("utf-8"),
    )

    document, page = _seed_document(db_session, user_id=PRIMARY_USER_ID, title="scoped-run-results")
    _add_occurrence(
        db_session,
        document=document,
        page=page,
        token="հայաստան",
        normalized_token="հայաստան",
        context_snippet="scoped group",
    )
    db_session.commit()

    run = matching_service.create_run(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceMatchRunCreateRequest(
            matching_direction=ReferenceMatchingDirection.SOURCE_TO_INTERNAL,
            source_id=source.id,
        ),
    )
    matching_service.process_run_in_session(db_session, run_id=run.id)
    db_session.commit()

    result_row = db_session.scalar(
        select(ReferenceMatchRunResult).where(
            ReferenceMatchRunResult.run_id == run.id,
            ReferenceMatchRunResult.reference_entry_id.is_not(None),
        )
    )
    assert result_row is not None

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            list_reference_matching_run_results(
                run_id=run.id,
                match_status=ReferenceStatusFilter.ALL,
                target_scope=ReferenceMatchRunEntryResultScopeFilter.ANY,
                search=None,
                limit=20,
                offset=0,
                current_user=_current_user(user_id=SECONDARY_USER_ID),
                session=db_session,
                reference_matching_service=matching_service,
            )
        )
    assert exc_info.value.status_code == 404

    assert matching_service.get_run_reference_entry_result_detail(
        db_session,
        user_id=SECONDARY_USER_ID,
        run_id=run.id,
        result_id=result_row.id,
    ) is None


def test_user_isolation_across_reference_sources_and_matches(db_session: Session) -> None:
    source_service = ReferenceSourceService()
    import_service = ReferenceImportService()
    matching_service = ReferenceMatchingService()

    secondary_source_detail = source_service.create_source(
        db_session,
        user_id=SECONDARY_USER_ID,
        request=ReferenceSourceCreateRequest(display_name="Secondary Source"),
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
        filename="reference.txt",
        content="exclusive\n".encode("utf-8"),
    )

    primary_document, primary_page = _seed_document(db_session, user_id=PRIMARY_USER_ID, title="isolation-primary")
    _add_occurrence(
        db_session,
        document=primary_document,
        page=primary_page,
        token="exclusive",
        normalized_token="exclusive",
        context_snippet="primary group",
    )
    db_session.commit()

    primary_match_response = matching_service.match_group(
        db_session,
        user_id=PRIMARY_USER_ID,
        normalized_form="exclusive",
    )
    assert primary_match_response.has_match is False

    keys = {item.key for item in source_service.list_sources(db_session, user_id=PRIMARY_USER_ID)}
    assert "manual_reference" in keys
    assert "secondary_source" not in keys


def test_source_first_process_run_ignores_internal_view_parameter(
    session_factory,
    db_session: Session,
    monkeypatch,
) -> None:
    source_service = ReferenceSourceService()
    import_service = ReferenceImportService()
    matching_service = ReferenceMatchingService()

    source_detail = source_service.create_source(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceSourceCreateRequest(display_name="Worker View Independence"),
    )
    source = source_service.get_user_source(db_session, user_id=PRIMARY_USER_ID, source_id=source_detail.id)
    assert source is not None
    import_service.import_entries(
        db_session,
        source=source,
        filename="reference.txt",
        content="հայաստան\n".encode("utf-8"),
    )
    document, page = _seed_document(db_session, user_id=PRIMARY_USER_ID, title="worker-view-doc")
    _add_occurrence(
        db_session,
        document=document,
        page=page,
        token="Հայաստան",
        normalized_token="հայաստան",
        context_snippet="worker view evidence",
    )
    db_session.commit()

    run = matching_service.create_run(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=ReferenceMatchRunCreateRequest(source_id=source.id),
    )

    @contextmanager
    def fake_session_scope():
        session = session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(reference_matching_service_module, "session_scope", fake_session_scope)

    matching_service.process_run(str(run.id), view="definitely-not-a-lexicon-view", include_fuzzy=False)

    verification_session = session_factory()
    try:
        stored_run = verification_session.get(ReferenceMatchRun, run.id)
        assert stored_run is not None
        assert stored_run.status is ReferenceMatchRunStatus.COMPLETED
        assert stored_run.matching_direction is ReferenceMatchingDirection.SOURCE_TO_INTERNAL
    finally:
        verification_session.close()


