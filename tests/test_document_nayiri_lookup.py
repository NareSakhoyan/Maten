from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select

from app.api.routers.documents import get_document_nayiri_lookup_summary, list_document_word_candidates
from app.db.models import (
    Document,
    DocumentPage,
    DocumentStatus,
    ExternalLookupCache,
    ExternalLookupResult,
    ExternalLookupSearchMode,
    ExternalLookupStatus,
    ExternalProvider,
    Occurrence,
    ReferenceMatchType,
)
from app.schemas.reference import ReferenceStatusFilter
from app.schemas.word import DocumentTrustedExternalStatus, SourceWordStatusView, WordSearchMode
from app.services.document_nayiri_lookup_service import DocumentNayiriLookupService
from app.services.document_service import DocumentService
from app.services.document_trusted_external_service import DocumentTrustedExternalService
from app.services.external_lookup_service import ExternalLookupService
from app.services.external_sources.base import ExternalEvidenceItem, ExternalLookupProvider
from app.services.source_word_review_service import SourceWordReviewService
from conftest import PRIMARY_USER_ID, rebuild_lexicon_index_for_document
from test_word_workflow import StubExternalProvider, _add_occurrence, _current_user, _seed_workspace


def _seed_nayiri_cache(db_session, *, normalized_form: str, matched: bool) -> None:
    provider = db_session.scalar(select(ExternalProvider).where(ExternalProvider.key == "nayiri_web"))
    if provider is None:
        provider = ExternalProvider(key="nayiri_web", display_name="Nayiri", is_active=True)
        db_session.add(provider)
        db_session.flush()

    fetched_at = datetime(2026, 5, 20, tzinfo=timezone.utc)
    cache = ExternalLookupCache(
        user_id=None,
        provider_id=provider.id,
        query_text=normalized_form,
        normalized_query=normalized_form,
        search_mode=ExternalLookupSearchMode.NORMALIZED,
        status=ExternalLookupStatus.COMPLETED,
        fetched_at=fetched_at,
        expires_at=None,
    )
    db_session.add(cache)
    db_session.flush()

    if matched:
        db_session.add(
            ExternalLookupResult(
                cache_id=cache.id,
                provider_id=provider.id,
                matched_form=normalized_form,
                normalized_form=normalized_form,
                source_title="Nayiri Entry",
                snippet="snippet",
                reference_link="https://example.test/nayiri/1",
                match_type=ReferenceMatchType.NORMALIZED,
                match_score=100.0,
            )
        )
    db_session.commit()


def test_document_word_candidates_include_nayiri_metadata(db_session) -> None:
    workspace = _seed_workspace(db_session)
    document = workspace["document"]
    _seed_nayiri_cache(db_session, normalized_form="հայաստան", matched=True)
    _seed_nayiri_cache(db_session, normalized_form="latin", matched=False)

    response = asyncio.run(
        list_document_word_candidates(
            document_id=document.id,
            search=None,
            word_filter="all",
            status_view=None,
            reference_status=None,
            limit=20,
            offset=0,
            current_user=_current_user(),
            session=db_session,
            document_service=DocumentService(),
            source_word_review_service=SourceWordReviewService(),
        )
    )

    matched_item = next(item for item in response.items if item.normalized_form == "հայաստան")
    assert matched_item.trusted_external_status is DocumentTrustedExternalStatus.FOUND
    assert matched_item.trusted_external_match_count == 1
    assert matched_item.trusted_external_matched_form == "հայաստան"
    assert matched_item.has_reference_match is True

    missing_item = next(item for item in response.items if item.normalized_form == "latin")
    assert missing_item.trusted_external_status is DocumentTrustedExternalStatus.NOT_FOUND
    assert missing_item.has_reference_match is False


def test_matched_filter_includes_nayiri_found_words(db_session) -> None:
    workspace = _seed_workspace(db_session)
    document = workspace["document"]
    _seed_nayiri_cache(db_session, normalized_form="latin", matched=True)

    response = asyncio.run(
        list_document_word_candidates(
            document_id=document.id,
            search=None,
            word_filter="matched",
            status_view=None,
            reference_status=None,
            limit=20,
            offset=0,
            current_user=_current_user(),
            session=db_session,
            document_service=DocumentService(),
            source_word_review_service=SourceWordReviewService(),
        )
    )

    latin_item = next(item for item in response.items if item.normalized_form == "latin")
    assert latin_item.trusted_external_status is DocumentTrustedExternalStatus.FOUND
    assert latin_item.has_reference_match is True


def test_unmatched_filter_excludes_unchecked_words(db_session) -> None:
    workspace = _seed_workspace(db_session)
    document = workspace["document"]
    _seed_nayiri_cache(db_session, normalized_form="latin", matched=False)

    response = asyncio.run(
        list_document_word_candidates(
            document_id=document.id,
            search=None,
            word_filter="unmatched",
            status_view=None,
            reference_status=None,
            limit=20,
            offset=0,
            current_user=_current_user(),
            session=db_session,
            document_service=DocumentService(),
            source_word_review_service=SourceWordReviewService(),
        )
    )

    assert response.total == 1
    assert response.items[0].normalized_form == "latin"
    assert "հայաստան" not in {item.normalized_form for item in response.items}


def test_document_nayiri_lookup_summary_endpoint(db_session) -> None:
    workspace = _seed_workspace(db_session)
    document = workspace["document"]
    _seed_nayiri_cache(db_session, normalized_form="հայաստան", matched=True)
    _seed_nayiri_cache(db_session, normalized_form="latin", matched=False)

    summary = asyncio.run(
        get_document_nayiri_lookup_summary(
            document_id=document.id,
            current_user=_current_user(),
            session=db_session,
            document_service=DocumentService(),
            document_trusted_external_service=DocumentTrustedExternalService(),
        )
    )

    assert summary.total_forms == 3
    assert summary.found_count == 1
    assert summary.not_found_count == 1
    assert summary.unchecked_count == 1


def test_background_lookup_skips_fresh_cache_and_checks_stale_words(db_session, monkeypatch) -> None:
    document = Document(
        id=uuid4(),
        user_id=PRIMARY_USER_ID,
        title="Nayiri Doc",
        original_filename="Nayiri Doc.pdf",
        mime_type="application/pdf",
        file_size_bytes=100,
        storage_bucket="book-originals",
        storage_path=f"{PRIMARY_USER_ID}/nayiri.pdf",
        sha256="b" * 64,
        page_count=1,
        status=DocumentStatus.COMPLETED,
    )
    page = DocumentPage(
        id=uuid4(),
        document_id=document.id,
        page_number=1,
        extraction_method="pdf_text",
        extracted_text="cached fresh",
        char_count=20,
    )
    db_session.add_all([document, page])
    db_session.flush()

    for token, normalized in (("Հայ", "հայ"), ("Բար", "բար")):
        _add_occurrence(
            db_session,
            document=document,
            page=page,
            token=token,
            normalized_token=normalized,
            snippet=token,
        )
    db_session.commit()
    rebuild_lexicon_index_for_document(db_session, user_id=PRIMARY_USER_ID, document=document)

    _seed_nayiri_cache(db_session, normalized_form="հայ", matched=True)

    provider = StubExternalProvider(
        items=[
            ExternalEvidenceItem(
                provider_key="nayiri_web",
                provider_display_name="Nayiri",
                matched_form="Բար",
                normalized_form="բար",
                source_title="Nayiri Entry",
                snippet="բար",
                reference_link="https://example.test/nayiri/2",
                match_type=ReferenceMatchType.NORMALIZED,
                match_score=100.0,
            )
        ]
    )
    external_lookup_service = ExternalLookupService(providers=[provider])
    lookup_service = DocumentNayiriLookupService(
        external_lookup_service=external_lookup_service,
        document_trusted_external_service=DocumentTrustedExternalService(
            external_lookup_service=external_lookup_service,
        ),
    )

    @contextmanager
    def fake_session_scope():
        yield db_session

    monkeypatch.setattr(
        "app.services.document_nayiri_lookup_service.session_scope",
        fake_session_scope,
    )

    run = lookup_service.start_document_run(
        db_session,
        user_id=PRIMARY_USER_ID,
        document_id=document.id,
    )
    lookup_service.process_run(run.id)

    assert provider.calls == 1

    status_map = DocumentTrustedExternalService(
        external_lookup_service=external_lookup_service,
    ).nayiri_status_map(db_session, normalized_forms=["հայ", "բար"])
    assert status_map["հայ"].status is DocumentTrustedExternalStatus.FOUND
    assert status_map["բար"].status is DocumentTrustedExternalStatus.FOUND
    assert run.checked_count == 1
    assert run.skipped_count == 1
