from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Document, DocumentPage, DocumentStatus, LexemeStatus, Occurrence
from app.schemas.lexeme import LexemeCreateRequest, LexemeMergeGroupsRequest
from app.schemas.lexicon import LexiconGroupView
from app.services.lexeme_service import LexemeConflictError, LexemeService
from app.services.lexicon_review_service import LexiconReviewService
from app.services.lexicon_service import LexiconService
from app.services.occurrence_service import OccurrenceService
from app.utils.token_classification import classify_token
from app.utils.text_reconstruction import reconstruct_page_text
from conftest import PRIMARY_USER_ID, SECONDARY_USER_ID


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
    page_number: int = 1,
) -> Occurrence:
    classification = classify_token(token)
    occurrence = Occurrence(
        id=uuid4(),
        document_id=document.id,
        page_id=page.id,
        lexeme_id=None,
        page_number=page_number,
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


def test_default_candidates_view_filters_to_reviewer_relevant_armenian_groups(db_session: Session) -> None:
    lexicon_service = LexiconService()
    review_service = LexiconReviewService()
    lexeme_service = LexemeService()
    document, page = _seed_document(db_session, user_id=PRIMARY_USER_ID, title="candidate-doc")

    _add_occurrence(
        db_session,
        document=document,
        page=page,
        token="Հայաստան",
        normalized_token="հայաստան",
        context_snippet="Հայաստան թեկնածու համատեքստ",
    )
    _add_occurrence(
        db_session,
        document=document,
        page=page,
        token="LATIN",
        normalized_token="latin",
        context_snippet="Latin should be suspicious",
    )
    _add_occurrence(
        db_session,
        document=document,
        page=page,
        token="Աղմուկ",
        normalized_token="աղմուկ",
        context_snippet="Ignored Armenian group",
    )
    _add_occurrence(
        db_session,
        document=document,
        page=page,
        token="Գիրք",
        normalized_token="գիրք",
        context_snippet="Linked Armenian group",
    )
    db_session.commit()

    review_service.ignore_groups(
        db_session,
        user_id=PRIMARY_USER_ID,
        normalized_forms=["աղմուկ"],
        reviewer_note="Noise",
    )
    lexeme_service.create_lexeme(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=LexemeCreateRequest(
            canonical_form="Գիրք",
            normalized_forms=["գիրք"],
            status=LexemeStatus.CURATED,
        ),
    )

    groups, total = lexicon_service.list_groups(
        db_session,
        user_id=PRIMARY_USER_ID,
        limit=20,
        offset=0,
    )

    assert total == 1
    assert [group.normalized_form for group in groups] == ["հայաստան"]
    assert groups[0].sample_document_titles == ["candidate-doc"]
    assert groups[0].group_state.value == "unreviewed"
    assert groups[0].dominant_script_type.value == "armenian"
    assert groups[0].is_suspicious is False
    assert groups[0].suspicion_reasons == []


def test_suspicious_view_returns_non_armenian_non_ignored_groups(db_session: Session) -> None:
    lexicon_service = LexiconService()
    review_service = LexiconReviewService()
    document, page = _seed_document(db_session, user_id=PRIMARY_USER_ID, title="suspicious-doc")

    _add_occurrence(
        db_session,
        document=document,
        page=page,
        token="LATIN",
        normalized_token="latin",
        context_snippet="latin token",
    )
    _add_occurrence(
        db_session,
        document=document,
        page=page,
        token="MixԱ",
        normalized_token="mixա",
        context_snippet="mixed token",
    )
    _add_occurrence(
        db_session,
        document=document,
        page=page,
        token="A12",
        normalized_token="a12",
        context_snippet="digit mixed token",
    )
    _add_occurrence(
        db_session,
        document=document,
        page=page,
        token="SKIP",
        normalized_token="skip",
        context_snippet="ignored suspicious token",
    )
    db_session.commit()

    review_service.ignore_groups(db_session, user_id=PRIMARY_USER_ID, normalized_forms=["skip"])

    groups, total = lexicon_service.list_groups(
        db_session,
        user_id=PRIMARY_USER_ID,
        limit=20,
        offset=0,
        view=LexiconGroupView.SUSPICIOUS,
    )

    assert total == 3
    assert [group.normalized_form for group in groups] == ["a12", "latin", "mixա"]
    assert all(group.is_suspicious for group in groups)
    assert {group.group_state.value for group in groups} == {"unreviewed"}
    assert groups[0].suspicion_reasons == ["token mixes digits and letters"]


def test_ignore_and_unignore_groups_support_bulk_forms(db_session: Session) -> None:
    lexicon_service = LexiconService()
    review_service = LexiconReviewService()
    document, page = _seed_document(db_session, user_id=PRIMARY_USER_ID, title="review-doc")

    for token, normalized in [("Բառ", "բառ"), ("Տերմին", "տերմին")]:
        _add_occurrence(
            db_session,
            document=document,
            page=page,
            token=token,
            normalized_token=normalized,
            context_snippet=f"{token} համատեքստ",
        )
    db_session.commit()

    ignored = review_service.ignore_groups(
        db_session,
        user_id=PRIMARY_USER_ID,
        normalized_forms=["բառ", " տերմին "],
        reviewer_note="bulk ignore",
    )
    assert ignored == ["բառ", "տերմին"]

    ignored_groups, ignored_total = lexicon_service.list_groups(
        db_session,
        user_id=PRIMARY_USER_ID,
        limit=20,
        offset=0,
        view=LexiconGroupView.IGNORED,
    )
    assert ignored_total == 2
    assert {group.normalized_form for group in ignored_groups} == {"բառ", "տերմին"}

    unignored = review_service.unignore_groups(
        db_session,
        user_id=PRIMARY_USER_ID,
        normalized_forms=["բառ"],
    )
    assert unignored == ["բառ"]

    ignored_groups, ignored_total = lexicon_service.list_groups(
        db_session,
        user_id=PRIMARY_USER_ID,
        limit=20,
        offset=0,
        view=LexiconGroupView.IGNORED,
    )
    assert ignored_total == 1
    assert [group.normalized_form for group in ignored_groups] == ["տերմին"]

    candidate_groups, candidate_total = lexicon_service.list_groups(
        db_session,
        user_id=PRIMARY_USER_ID,
        limit=20,
        offset=0,
        view=LexiconGroupView.CANDIDATES,
    )
    assert candidate_total == 1
    assert [group.normalized_form for group in candidate_groups] == ["բառ"]


def test_linked_state_takes_precedence_over_ignored_review(db_session: Session) -> None:
    lexicon_service = LexiconService()
    review_service = LexiconReviewService()
    lexeme_service = LexemeService()
    document, page = _seed_document(db_session, user_id=PRIMARY_USER_ID, title="precedence-doc")

    _add_occurrence(
        db_session,
        document=document,
        page=page,
        token="Գիրք",
        normalized_token="գիրք",
        context_snippet="linked and ignored token",
    )
    db_session.commit()

    review_service.ignore_groups(db_session, user_id=PRIMARY_USER_ID, normalized_forms=["գիրք"])
    detail = lexeme_service.create_lexeme(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=LexemeCreateRequest(
            canonical_form="Գիրք",
            normalized_forms=["գիրք"],
        ),
    )

    linked_groups, linked_total = lexicon_service.list_groups(
        db_session,
        user_id=PRIMARY_USER_ID,
        limit=20,
        offset=0,
        view=LexiconGroupView.LINKED,
    )
    assert linked_total == 1
    assert [group.normalized_form for group in linked_groups] == ["գիրք"]
    assert linked_groups[0].group_state.value == "linked"
    assert linked_groups[0].linked_lexeme_id == detail.id

    ignored_groups, ignored_total = lexicon_service.list_groups(
        db_session,
        user_id=PRIMARY_USER_ID,
        limit=20,
        offset=0,
        view=LexiconGroupView.IGNORED,
    )
    assert ignored_total == 0
    assert ignored_groups == []


def test_group_detail_includes_document_title_page_and_snippet(db_session: Session) -> None:
    lexicon_service = LexiconService()
    document, page = _seed_document(db_session, user_id=PRIMARY_USER_ID, title="detail-doc", page_number=7)
    _add_occurrence(
        db_session,
        document=document,
        page=page,
        token="Հայաստան",
        normalized_token="հայաստան",
        context_snippet="Մանրամասն համատեքստ",
        page_number=7,
    )
    db_session.commit()

    detail = lexicon_service.get_group_detail(db_session, user_id=PRIMARY_USER_ID, normalized_form="հայաստան")

    assert detail is not None
    assert detail.group_state.value == "unreviewed"
    assert detail.occurrences[0].document_title == "detail-doc"
    assert detail.occurrences[0].original_filename == "detail-doc.pdf"
    assert detail.occurrences[0].page_number == 7
    assert detail.occurrences[0].context_snippet == "Մանրամասն համատեքստ"


def test_create_lexeme_from_suspicious_group_still_works(db_session: Session) -> None:
    lexicon_service = LexiconService()
    lexeme_service = LexemeService()
    document, page = _seed_document(db_session, user_id=PRIMARY_USER_ID, title="suspicious-create-doc")
    occurrence = _add_occurrence(
        db_session,
        document=document,
        page=page,
        token="LATIN",
        normalized_token="latin",
        context_snippet="Suspicious Latin context",
    )
    db_session.commit()

    detail = lexeme_service.create_lexeme(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=LexemeCreateRequest(
            canonical_form="LATIN",
            normalized_forms=["latin"],
        ),
    )
    db_session.refresh(occurrence)

    assert occurrence.lexeme_id == detail.id

    linked_groups, linked_total = lexicon_service.list_groups(
        db_session,
        user_id=PRIMARY_USER_ID,
        limit=20,
        offset=0,
        view=LexiconGroupView.LINKED,
    )
    assert linked_total == 1
    assert linked_groups[0].normalized_form == "latin"
    assert linked_groups[0].group_state.value == "linked"
    assert linked_groups[0].dominant_script_type.value == "latin"
    assert linked_groups[0].is_suspicious is True


def test_merge_additional_groups_and_conflicts_still_work(db_session: Session) -> None:
    lexeme_service = LexemeService()
    document, page = _seed_document(db_session, user_id=PRIMARY_USER_ID, title="merge-doc")
    _add_occurrence(
        db_session,
        document=document,
        page=page,
        token="Հայաստան",
        normalized_token="հայաստան",
        context_snippet="Հայաստան սկզբնական համատեքստ",
    )
    merge_occurrence = _add_occurrence(
        db_session,
        document=document,
        page=page,
        token="Հայկական",
        normalized_token="հայկական",
        context_snippet="Հայկական լրացուցիչ համատեքստ",
    )
    _add_occurrence(
        db_session,
        document=document,
        page=page,
        token="Գիրք",
        normalized_token="գիրք",
        context_snippet="Գիրք հակամարտության համատեքստ",
    )
    db_session.commit()

    first = lexeme_service.create_lexeme(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=LexemeCreateRequest(
            canonical_form="Հայաստան",
            normalized_forms=["հայաստան"],
        ),
    )
    merged = lexeme_service.merge_groups(
        db_session,
        user_id=PRIMARY_USER_ID,
        lexeme_id=first.id,
        request=LexemeMergeGroupsRequest(normalized_forms=["հայկական"]),
    )
    assert merged is not None
    db_session.refresh(merge_occurrence)
    assert merge_occurrence.lexeme_id == first.id

    second = lexeme_service.create_lexeme(
        db_session,
        user_id=PRIMARY_USER_ID,
        request=LexemeCreateRequest(
            canonical_form="Գիրք",
            normalized_forms=["գիրք"],
        ),
    )

    try:
        lexeme_service.merge_groups(
            db_session,
            user_id=PRIMARY_USER_ID,
            lexeme_id=second.id,
            request=LexemeMergeGroupsRequest(normalized_forms=["հայաստան"]),
        )
    except LexemeConflictError as exc:
        payload = exc.payload()
    else:  # pragma: no cover
        raise AssertionError("Expected a LexemeConflictError.")

    assert payload["conflicting_normalized_forms"] == ["հայաստան"]
    assert payload["conflicting_lexeme_ids"] == [str(first.id)]


def test_user_isolation_across_review_states(db_session: Session) -> None:
    lexicon_service = LexiconService()
    review_service = LexiconReviewService()
    primary_document, primary_page = _seed_document(db_session, user_id=PRIMARY_USER_ID, title="primary-doc")
    secondary_document, secondary_page = _seed_document(db_session, user_id=SECONDARY_USER_ID, title="secondary-doc")

    _add_occurrence(
        db_session,
        document=primary_document,
        page=primary_page,
        token="Բառ",
        normalized_token="բառ",
        context_snippet="Առաջին օգտվողի բառ",
    )
    _add_occurrence(
        db_session,
        document=secondary_document,
        page=secondary_page,
        token="Բառ",
        normalized_token="բառ",
        context_snippet="Երկրորդ օգտվողի բառ",
    )
    db_session.commit()

    review_service.ignore_groups(db_session, user_id=PRIMARY_USER_ID, normalized_forms=["բառ"])

    primary_candidates, primary_candidate_total = lexicon_service.list_groups(
        db_session,
        user_id=PRIMARY_USER_ID,
        limit=20,
        offset=0,
    )
    assert primary_candidate_total == 0
    assert primary_candidates == []

    primary_ignored, primary_ignored_total = lexicon_service.list_groups(
        db_session,
        user_id=PRIMARY_USER_ID,
        limit=20,
        offset=0,
        view=LexiconGroupView.IGNORED,
    )
    assert primary_ignored_total == 1
    assert [group.normalized_form for group in primary_ignored] == ["բառ"]

    secondary_candidates, secondary_candidate_total = lexicon_service.list_groups(
        db_session,
        user_id=SECONDARY_USER_ID,
        limit=20,
        offset=0,
    )
    assert secondary_candidate_total == 1
    assert [group.normalized_form for group in secondary_candidates] == ["բառ"]
    assert secondary_candidates[0].group_state.value == "unreviewed"


def test_occurrences_are_built_from_reconstructed_text_not_raw_fragments(db_session: Session) -> None:
    occurrence_service = OccurrenceService()
    document, page = _seed_document(db_session, user_id=PRIMARY_USER_ID, title="reconstructed-doc")
    raw_text = "աստու-\nած գիրք"
    reconstructed_text = reconstruct_page_text(raw_text)

    page.raw_extracted_text = raw_text
    page.reconstructed_text = reconstructed_text
    page.extracted_text = reconstructed_text
    page.char_count = len(reconstructed_text)
    db_session.flush()

    occurrence_service.store_page_occurrences(
        db_session,
        document_id=document.id,
        page_id=page.id,
        page_number=page.page_number,
        text=page.reconstructed_text or page.extracted_text,
    )
    db_session.commit()

    db_session.refresh(page)
    assert page.raw_extracted_text == "աստու-\nած գիրք"
    assert page.reconstructed_text == "աստուած գիրք"

    occurrences = list(
        db_session.scalars(
            select(Occurrence)
            .where(Occurrence.document_id == document.id)
            .order_by(Occurrence.char_start.asc())
        )
    )
    assert [occurrence.token for occurrence in occurrences] == ["աստուած", "գիրք"]
    assert {occurrence.normalized_token for occurrence in occurrences} == {"աստուած", "գիրք"}
