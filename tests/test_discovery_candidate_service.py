from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    DiscoveryBuildRun,
    DiscoveryCandidate,
    Document,
    DocumentPage,
    DocumentStatus,
    ExternalLookupCache,
    ExternalLookupResult,
    ExternalLookupSearchMode,
    ExternalLookupStatus,
    ExternalProvider,
    JobKind,
    JobStageEvent,
    Lexeme,
    LexemeForm,
    LexemeStatus,
    MorphologyRunStatus,
    NerEntityEntry,
    NerSource,
    Occurrence,
    ReferenceEntry,
    ReferenceSource,
)
from app.services.discovery.discovery_candidate_service import DiscoveryCandidateService
import app.services.discovery.discovery_candidate_service as discovery_candidate_service_module
from app.utils.token_classification import classify_token
from conftest import PRIMARY_USER_ID


class EmptyCorpusService:
    def lookup(self, query: str, *, limit: int = 8):  # noqa: ARG002
        return []


class StubCorpusMatch:
    def __init__(self, *, normalized_query: str, canonical_form: str, token_count: int, source_count: int) -> None:
        self.normalized_query = normalized_query
        self.canonical_form = canonical_form
        self.token_count = token_count
        self.source_count = source_count


class StubCorpusService:
    def __init__(self, matches: dict[str, list[StubCorpusMatch]]) -> None:
        self.matches = matches

    def lookup(self, query: str, *, limit: int = 8):  # noqa: ARG002
        return self.matches.get(query, [])


def _seed_document(session: Session) -> tuple[Document, DocumentPage]:
    document = Document(
        id=uuid4(),
        user_id=PRIMARY_USER_ID,
        title="Discovery Test",
        original_filename="discovery.pdf",
        mime_type="application/pdf",
        file_size_bytes=123,
        storage_bucket="book-originals",
        storage_path=f"{PRIMARY_USER_ID}/discovery.pdf",
        sha256="a" * 64,
        page_count=1,
        status=DocumentStatus.COMPLETED,
    )
    page = DocumentPage(
        id=uuid4(),
        document_id=document.id,
        page_number=1,
        extraction_method="pdf_text",
        extracted_text="Discovery page",
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


def _add_plausible_candidate(
    session: Session,
    *,
    document: Document,
    page: DocumentPage,
    normalized_form: str,
    token: str,
    occurrence_count: int,
    page_count: int,
) -> None:
    _add_occurrence(
        session,
        document=document,
        page=page,
        token=token,
        normalized_token=normalized_form,
        context=f"{token} context",
    )
    session.add(
        DiscoveryCandidate(
            id=uuid4(),
            user_id=PRIMARY_USER_ID,
            document_id=document.id,
            normalized_form=normalized_form,
            occurrence_count=occurrence_count,
            page_count=page_count,
            resolution_status="unknown_plausible",
            candidate_type="unknown_plausible",
            interest_score=50.0,
            review_status="unreviewed",
        )
    )
    session.flush()


def test_discovery_stage_checkpoint_commits_outside_build_session(
    db_session: Session,
    session_factory,
    monkeypatch,
) -> None:
    document, _page = _seed_document(db_session)
    run = DiscoveryBuildRun(
        id=uuid4(),
        user_id=str(PRIMARY_USER_ID),
        document_id=document.id,
        status=MorphologyRunStatus.RUNNING,
        current_stage_code="discovery_running",
        current_stage_label="Discovery running",
        progress_percent=5,
    )
    db_session.add(run)
    db_session.commit()

    @contextmanager
    def isolated_session_scope():
        session = session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(discovery_candidate_service_module, "session_scope", isolated_session_scope)

    service = DiscoveryCandidateService(nayiri_corpus_service=EmptyCorpusService())
    service._commit_run_stage_checkpoint(
        run.id,
        stage_code="collecting_evidence",
        progress_percent=45,
    )

    db_session.expire_all()
    refreshed_run = db_session.get(DiscoveryBuildRun, run.id)
    assert refreshed_run is not None
    assert refreshed_run.current_stage_code == "collecting_evidence"
    assert refreshed_run.progress_percent == 45

    event = db_session.scalar(
        select(JobStageEvent).where(
            JobStageEvent.job_kind == JobKind.DISCOVERY_BUILD,
            JobStageEvent.job_id == str(run.id),
            JobStageEvent.stage_code == "collecting_evidence",
        )
    )
    assert event is not None
    assert event.progress_percent == 45


def test_build_ranks_unknowns_and_suppresses_known_and_noise(db_session: Session) -> None:
    document, page = _seed_document(db_session)
    _add_occurrence(
        db_session,
        document=document,
        page=page,
        token="Գիրք",
        normalized_token="գիրք",
        context="Known internal lexicon word",
    )
    _add_occurrence(
        db_session,
        document=document,
        page=page,
        token="Անծանօթ",
        normalized_token="անծանօթ",
        context="Անծանօթ բառ",
    )
    _add_occurrence(
        db_session,
        document=document,
        page=page,
        token="Անծանօթ",
        normalized_token="անծանօթ",
        context="Անծանօթ կրկնուած",
    )
    _add_occurrence(
        db_session,
        document=document,
        page=page,
        token="2ն",
        normalized_token="2ն",
        context="OCR digit noise",
    )

    lexeme = Lexeme(
        id=uuid4(),
        user_id=str(PRIMARY_USER_ID),
        canonical_form="Գիրք",
        canonical_normalized_form="գիրք",
        status=LexemeStatus.CURATED,
    )
    db_session.add(lexeme)
    db_session.flush()
    db_session.add(
        LexemeForm(
            id=uuid4(),
            lexeme_id=lexeme.id,
            user_id=str(PRIMARY_USER_ID),
            normalized_form="գիրք",
        )
    )
    db_session.commit()

    service = DiscoveryCandidateService(nayiri_corpus_service=EmptyCorpusService())
    summary = service.build_for_document(db_session, user_id=PRIMARY_USER_ID, document_id=document.id)

    assert summary.total_grouped_forms == 3
    assert summary.resolved_known == 1
    assert summary.unknown_plausible == 1
    assert summary.probable_ocr_noise == 1
    assert summary.shown_in_queue == 0

    rows = {
        row.normalized_form: row
        for row in db_session.scalars(
            select(DiscoveryCandidate).where(DiscoveryCandidate.document_id == document.id)
        )
    }
    assert rows["գիրք"].candidate_type == "known_suppressed"
    assert rows["անծանօթ"].resolution_status == "unknown_plausible"
    assert rows["անծանօթ"].interest_score == 0
    assert rows["2ն"].candidate_type == "noise_suppressed"

    visible, total = service.list_candidates(
        db_session,
        user_id=PRIMARY_USER_ID,
        document_id=document.id,
        limit=20,
        offset=0,
    )
    assert total == 0
    assert visible == []


def test_strong_local_corpus_attestation_is_suppressed_without_web_lookup(db_session: Session) -> None:
    document, page = _seed_document(db_session)
    _add_occurrence(
        db_session,
        document=document,
        page=page,
        token="Վկայուած",
        normalized_token="վկայուած",
        context="Վկայուած բառ տեղական կորպուսին մէջ",
    )
    db_session.commit()

    service = DiscoveryCandidateService(
        nayiri_corpus_service=StubCorpusService(
            {
                "վկայուած": [
                    StubCorpusMatch(
                        normalized_query="վկայուած",
                        canonical_form="վկայուած",
                        token_count=4,
                        source_count=2,
                    )
                ]
            }
        )
    )
    summary = service.build_for_document(db_session, user_id=PRIMARY_USER_ID, document_id=document.id)

    assert summary.attested_in_corpus == 1
    assert summary.suppressed == 1
    assert summary.shown_in_queue == 0
    candidate = db_session.scalar(
        select(DiscoveryCandidate).where(
            DiscoveryCandidate.document_id == document.id,
            DiscoveryCandidate.normalized_form == "վկայուած",
        )
    )
    assert candidate is not None
    assert candidate.resolution_status == "attested_in_corpus"
    assert candidate.candidate_type == "attested_suppressed"
    assert candidate.canonical_form_candidate == "վկայուած"
    assert candidate.best_evidence_summary["provider_key"] == "nayiri_western_corpus"
    assert candidate.best_evidence_summary["definition_quality"] == "unknown"


def test_pioner_named_entity_evidence_stays_visible_and_non_validating(db_session: Session) -> None:
    document, page = _seed_document(db_session)
    _add_occurrence(
        db_session,
        document=document,
        page=page,
        token="Երեւան",
        normalized_token="երեւան",
        context="Երեւան քաղաքը յիշատակուած է",
    )
    source = NerSource(
        id=uuid4(),
        provider_key="pioner_ner",
        display_name="pioNER gold test",
        source_kind="gold",
        dataset_split="test",
        source_url="https://huggingface.co/datasets/Karavet/pioNER-Armenian-Named-Entity",
        license="Apache-2.0",
        is_active=True,
    )
    db_session.add(source)
    db_session.flush()
    db_session.add(
        NerEntityEntry(
            id=uuid4(),
            source_id=source.id,
            entity_surface="Երեւան",
            normalized_surface="երեւան",
            entity_type="LOC",
            occurrence_count=3,
            confidence=0.85,
            sample_contexts=["Երեւան քաղաքը"],
        )
    )
    db_session.commit()

    service = DiscoveryCandidateService(nayiri_corpus_service=EmptyCorpusService())
    summary = service.build_for_document(db_session, user_id=PRIMARY_USER_ID, document_id=document.id)

    assert summary.possible_named_entity == 1
    candidate = db_session.scalar(
        select(DiscoveryCandidate).where(
            DiscoveryCandidate.document_id == document.id,
            DiscoveryCandidate.normalized_form == "երեւան",
        )
    )
    assert candidate is not None
    assert candidate.resolution_status == "possible_named_entity"
    assert candidate.candidate_type == "named_entity_candidate"
    assert candidate.best_evidence_summary["provider_type"] == "ner"
    assert candidate.best_evidence_summary["validation_strength"] != "validates_word"


def test_imported_reference_without_definition_is_suppressed_as_attested(db_session: Session) -> None:
    document, page = _seed_document(db_session)
    _add_occurrence(
        db_session,
        document=document,
        page=page,
        token="Բառ",
        normalized_token="բառ",
        context="Բառ աղբիւրի մէջ",
    )
    source = ReferenceSource(
        id=uuid4(),
        user_id=str(PRIMARY_USER_ID),
        key="test-ref",
        display_name="Test Reference",
    )
    db_session.add(source)
    db_session.flush()
    db_session.add(
        ReferenceEntry(
            id=uuid4(),
            source_id=source.id,
            surface_form="Բառ",
            normalized_form="բառ",
            metadata_json={},
        )
    )
    db_session.commit()

    service = DiscoveryCandidateService(nayiri_corpus_service=EmptyCorpusService())
    summary = service.build_for_document(db_session, user_id=PRIMARY_USER_ID, document_id=document.id)

    assert summary.poorly_defined == 1
    assert summary.suppressed == 1
    assert summary.shown_in_queue == 0
    candidate = db_session.scalar(
        select(DiscoveryCandidate).where(
            DiscoveryCandidate.document_id == document.id,
            DiscoveryCandidate.normalized_form == "բառ",
        )
    )
    assert candidate is not None
    assert candidate.resolution_status == "poorly_defined"
    assert candidate.interest_score == 0
    assert candidate.best_evidence_summary["definition_quality"] == "missing"


def test_decision_persists_on_candidate(db_session: Session) -> None:
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

    service = DiscoveryCandidateService(nayiri_corpus_service=EmptyCorpusService())
    service.build_for_document(db_session, user_id=PRIMARY_USER_ID, document_id=document.id)
    candidate = db_session.scalar(
        select(DiscoveryCandidate).where(
            DiscoveryCandidate.document_id == document.id,
            DiscoveryCandidate.normalized_form == "անծանօթ",
        )
    )
    assert candidate is not None

    updated = service.record_decision(
        db_session,
        user_id=PRIMARY_USER_ID,
        document_id=document.id,
        candidate_id=candidate.id,
        decision="mark_interesting",
        note="Needs research",
    )

    assert updated.review_status == "reviewed"
    assert updated.reviewer_decision == "mark_interesting"
    assert updated.reviewer_note == "Needs research"


def test_mark_uncertain_marks_candidate_reviewed(db_session: Session) -> None:
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

    service = DiscoveryCandidateService(nayiri_corpus_service=EmptyCorpusService())
    service.build_for_document(db_session, user_id=PRIMARY_USER_ID, document_id=document.id)
    candidate = db_session.scalar(
        select(DiscoveryCandidate).where(
            DiscoveryCandidate.document_id == document.id,
            DiscoveryCandidate.normalized_form == "անծանօթ",
        )
    )
    assert candidate is not None

    updated = service.record_decision(
        db_session,
        user_id=PRIMARY_USER_ID,
        document_id=document.id,
        candidate_id=candidate.id,
        decision="mark_uncertain",
    )

    assert updated.review_status == "reviewed"
    assert updated.reviewer_decision == "mark_uncertain"


def test_create_lexeme_decision_links_candidate_and_reuses_existing_conflict(db_session: Session) -> None:
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

    service = DiscoveryCandidateService(nayiri_corpus_service=EmptyCorpusService())
    service.build_for_document(db_session, user_id=PRIMARY_USER_ID, document_id=document.id)
    candidate = db_session.scalar(
        select(DiscoveryCandidate).where(
            DiscoveryCandidate.document_id == document.id,
            DiscoveryCandidate.normalized_form == "անծանօթ",
        )
    )
    assert candidate is not None

    created = service.record_decision(
        db_session,
        user_id=PRIMARY_USER_ID,
        document_id=document.id,
        candidate_id=candidate.id,
        decision="create_lexeme",
    )

    assert created.review_status == "reviewed"
    assert created.reviewer_decision == "create_lexeme"
    assert created.linked_lexeme_id is not None

    lexeme = db_session.get(Lexeme, created.linked_lexeme_id)
    assert lexeme is not None
    assert lexeme.canonical_normalized_form == "անծանօթ"
    forms = list(
        db_session.scalars(
            select(LexemeForm.normalized_form).where(
                LexemeForm.lexeme_id == lexeme.id,
            )
        )
    )
    assert "անծանօթ" in forms


def test_candidate_detail_uses_latest_external_cache(db_session: Session) -> None:
    document, page = _seed_document(db_session)
    _add_occurrence(
        db_session,
        document=document,
        page=page,
        token="Նմուշ",
        normalized_token="նմուշ",
        context="Նմուշ բառ",
    )
    provider = ExternalProvider(
        id=uuid4(),
        key="nayiri_web",
        display_name="Nayiri Web",
        is_active=True,
    )
    db_session.add(provider)
    db_session.flush()

    older_cache = ExternalLookupCache(
        id=uuid4(),
        user_id=None,
        provider_id=provider.id,
        query_text="նմուշ",
        normalized_query="նմուշ",
        search_mode=ExternalLookupSearchMode.NORMALIZED,
        status=ExternalLookupStatus.COMPLETED,
        fetched_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        expires_at=None,
    )
    newer_cache = ExternalLookupCache(
        id=uuid4(),
        user_id=None,
        provider_id=provider.id,
        query_text="նմուշ",
        normalized_query="նմուշ",
        search_mode=ExternalLookupSearchMode.NORMALIZED,
        status=ExternalLookupStatus.COMPLETED,
        fetched_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
        expires_at=None,
    )
    db_session.add_all([older_cache, newer_cache])
    db_session.flush()
    older_cache.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    newer_cache.created_at = datetime(2026, 2, 1, tzinfo=timezone.utc)

    db_session.add_all(
        [
            ExternalLookupResult(
                id=uuid4(),
                cache_id=older_cache.id,
                provider_id=provider.id,
                matched_form="հին",
                normalized_form="նմուշ",
                source_title="Old cache",
                snippet="Old",
                reference_link=None,
                match_type="exact",
                match_score=90,
            ),
            ExternalLookupResult(
                id=uuid4(),
                cache_id=newer_cache.id,
                provider_id=provider.id,
                matched_form="նոր",
                normalized_form="նմուշ",
                source_title="New cache",
                snippet="New",
                reference_link=None,
                match_type="exact",
                match_score=95,
            ),
        ]
    )
    db_session.commit()

    service = DiscoveryCandidateService(nayiri_corpus_service=EmptyCorpusService())
    service.build_for_document(db_session, user_id=PRIMARY_USER_ID, document_id=document.id)
    candidate = db_session.scalar(
        select(DiscoveryCandidate).where(
            DiscoveryCandidate.document_id == document.id,
            DiscoveryCandidate.normalized_form == "նմուշ",
        )
    )
    assert candidate is not None
    detail = service.get_candidate_detail(
        db_session,
        user_id=PRIMARY_USER_ID,
        document_id=document.id,
        candidate_id=candidate.id,
    )
    assert detail is not None
    _, provider_evidence, occurrence_evidence, _ = detail
    web_evidence = [row for row in provider_evidence if row.provider_type == "web_dictionary"]
    assert web_evidence
    assert web_evidence[0].matched_form == "նոր"
    assert occurrence_evidence
    assert occurrence_evidence[0].normalized_token == "նմուշ"

def test_summary_uses_projection_counts_and_latest_build(db_session: Session) -> None:
    document, page = _seed_document(db_session)
    _add_occurrence(
        db_session,
        document=document,
        page=page,
        token="Գիրք",
        normalized_token="գիրք",
        context="Known internal lexicon word",
    )
    _add_occurrence(
        db_session,
        document=document,
        page=page,
        token="Անծանօթ",
        normalized_token="անծանօթ",
        context="Անծանօթ բառ",
    )
    lexeme = Lexeme(
        id=uuid4(),
        user_id=str(PRIMARY_USER_ID),
        canonical_form="Գիրք",
        canonical_normalized_form="գիրք",
        status=LexemeStatus.CURATED,
    )
    db_session.add(lexeme)
    db_session.flush()
    db_session.add(
        LexemeForm(
            id=uuid4(),
            lexeme_id=lexeme.id,
            user_id=str(PRIMARY_USER_ID),
            normalized_form="գիրք",
        )
    )
    db_session.commit()

    service = DiscoveryCandidateService(nayiri_corpus_service=EmptyCorpusService())
    build_summary = service.build_for_document(db_session, user_id=PRIMARY_USER_ID, document_id=document.id)
    run = DiscoveryBuildRun(
        id=uuid4(),
        user_id=str(PRIMARY_USER_ID),
        document_id=document.id,
        status=MorphologyRunStatus.COMPLETED,
        candidate_count=build_summary.total_grouped_forms,
        shown_count=build_summary.shown_in_queue,
        suppressed_count=build_summary.suppressed,
        progress_percent=100,
    )
    db_session.add(run)
    db_session.commit()

    candidate = db_session.scalar(
        select(DiscoveryCandidate).where(
            DiscoveryCandidate.document_id == document.id,
            DiscoveryCandidate.normalized_form == "անծանօթ",
        )
    )
    assert candidate is not None
    service.record_decision(
        db_session,
        user_id=PRIMARY_USER_ID,
        document_id=document.id,
        candidate_id=candidate.id,
        decision="mark_interesting",
    )

    summary = service.get_summary(db_session, user_id=PRIMARY_USER_ID, document_id=document.id)

    assert summary.total_candidates == 2
    assert summary.visible_candidates == 0
    assert summary.suppressed_candidates == 2
    assert summary.reviewed_candidates == 1
    assert summary.unreviewed_candidates == 1
    assert summary.by_candidate_type["known_suppressed"] == 1
    assert summary.by_resolution_status[candidate.resolution_status] == 1
    assert summary.by_review_status["reviewed"] == 1
    assert summary.latest_build is not None
    assert summary.latest_build.id == run.id
    assert summary.latest_build.status == "completed"


def test_rare_unknown_plausible_remains_visible_in_default_queue(db_session: Session) -> None:
    document, page = _seed_document(db_session)
    _add_plausible_candidate(
        db_session,
        document=document,
        page=page,
        normalized_form="նորաբառ",
        token="Նորաբառ",
        occurrence_count=3,
        page_count=2,
    )
    db_session.commit()

    service = DiscoveryCandidateService(nayiri_corpus_service=EmptyCorpusService())
    visible, total = service.list_candidates(
        db_session,
        user_id=PRIMARY_USER_ID,
        document_id=document.id,
        limit=20,
        offset=0,
    )

    assert total == 1
    assert len(visible) == 1
    assert visible[0].normalized_form == "նորաբառ"

    summary = service.get_summary(db_session, user_id=PRIMARY_USER_ID, document_id=document.id)
    assert summary.visible_candidates == 1
    assert summary.suppressed_candidates == 0


def test_default_candidate_order_shows_lower_occurrences_first(db_session: Session) -> None:
    document, page = _seed_document(db_session)
    _add_plausible_candidate(
        db_session,
        document=document,
        page=page,
        normalized_form="երեք",
        token="Երեք",
        occurrence_count=3,
        page_count=2,
    )
    _add_plausible_candidate(
        db_session,
        document=document,
        page=page,
        normalized_form="մէկ",
        token="Մէկ",
        occurrence_count=1,
        page_count=1,
    )
    db_session.commit()

    service = DiscoveryCandidateService(nayiri_corpus_service=EmptyCorpusService())
    visible, total = service.list_candidates(
        db_session,
        user_id=PRIMARY_USER_ID,
        document_id=document.id,
        limit=20,
        offset=0,
    )

    assert total == 2
    assert [candidate.normalized_form for candidate in visible] == ["մէկ", "երեք"]


def test_high_frequency_unknown_plausible_hidden_from_default_queue(db_session: Session) -> None:
    document, page = _seed_document(db_session)
    _add_plausible_candidate(
        db_session,
        document=document,
        page=page,
        normalized_form="կը",
        token="Կը",
        occurrence_count=4,
        page_count=3,
    )
    db_session.commit()

    service = DiscoveryCandidateService(nayiri_corpus_service=EmptyCorpusService())
    visible, total = service.list_candidates(
        db_session,
        user_id=PRIMARY_USER_ID,
        document_id=document.id,
        limit=20,
        offset=0,
    )

    assert total == 0
    assert visible == []

    summary = service.get_summary(db_session, user_id=PRIMARY_USER_ID, document_id=document.id)
    assert summary.total_candidates == 1
    assert summary.visible_candidates == 0
    assert summary.suppressed_candidates == 1


def test_high_frequency_unknown_plausible_visible_when_include_suppressed(db_session: Session) -> None:
    document, page = _seed_document(db_session)
    _add_plausible_candidate(
        db_session,
        document=document,
        page=page,
        normalized_form="մը",
        token="Մը",
        occurrence_count=12,
        page_count=7,
    )
    db_session.commit()

    service = DiscoveryCandidateService(nayiri_corpus_service=EmptyCorpusService())
    visible, total = service.list_candidates(
        db_session,
        user_id=PRIMARY_USER_ID,
        document_id=document.id,
        limit=20,
        offset=0,
        include_suppressed=True,
    )

    assert total == 1
    assert len(visible) == 1
    assert visible[0].normalized_form == "մը"
    assert visible[0].resolution_status == "unknown_plausible"
