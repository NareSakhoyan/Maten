from __future__ import annotations

from uuid import uuid4

from sqlalchemy.orm import Session

from app.db.models import Lexeme, LexemeForm, LexemeFormMapping, LexemeStatus, ReferenceEntry, ReferenceSource
from app.services.lexeme_resolution.lexeme_resolver import AnalyzerResult, LexemeResolver, OcrCorrectionCandidate
from conftest import PRIMARY_USER_ID


def _pie_analysis(surface: str, lemma: str) -> AnalyzerResult:
    return AnalyzerResult(
        surface_form=surface,
        normalized_surface_form=surface,
        lemma=lemma,
        normalized_lemma=lemma,
        pos="AUX",
        features={"VerbForm": "Fin"},
        language_profile="eastern",
        source_key="pie_eastern_morphology",
        confidence=0.35,
        raw_payload={"fixture": "pie"},
    )


def test_pie_returns_em_without_forcing_linel_unless_mapping_exists(db_session: Session) -> None:
    resolution = LexemeResolver().resolve(
        db_session,
        user_id=PRIMARY_USER_ID,
        surface_form="եմ",
        normalized_form="եմ",
        morphological_analyses=[_pie_analysis("եմ", "եմ")],
    )

    assert resolution.morphological_lemma == "եմ"
    assert resolution.dictionary_lemma is None
    assert resolution.resolution_type == "morphology_fallback_only"
    assert resolution.has_structured_dictionary_lemma is False


def test_fuzzy_match_em_to_el_is_only_ocr_candidate(db_session: Session) -> None:
    db_session.add(
        LexemeFormMapping(
            id=uuid4(),
            user_id=str(PRIMARY_USER_ID),
            surface_form="եմ",
            normalized_surface_form="եմ",
            dictionary_lemma="ել",
            normalized_dictionary_lemma="ել",
            source_key="fuzzy_ocr",
            mapping_type="ocr_correction_candidate",
            source_type="future_resource",
            review_status="approved",
            language_profile="eastern",
            confidence=0.72,
        )
    )
    db_session.commit()

    resolution = LexemeResolver().resolve(
        db_session,
        user_id=PRIMARY_USER_ID,
        surface_form="եմ",
        normalized_form="եմ",
        ocr_correction_candidates=[
            OcrCorrectionCandidate(
                surface_form="եմ",
                normalized_form="եմ",
                candidate="ել",
                normalized_candidate="ել",
                source_key="fuzzy_ocr",
                confidence=0.72,
            )
        ],
    )

    assert resolution.dictionary_lemma is None
    assert [candidate.normalized_candidate for candidate in resolution.ocr_correction_candidates] == ["ել"]


def test_curated_mapping_em_to_linel_resolves_dictionary_lemma(db_session: Session) -> None:
    lexeme = Lexeme(
        id=uuid4(),
        user_id=str(PRIMARY_USER_ID),
        canonical_form="լինել",
        canonical_normalized_form="լինել",
        status=LexemeStatus.CURATED,
    )
    db_session.add(lexeme)
    db_session.flush()
    db_session.add(
        LexemeForm(
            id=uuid4(),
            lexeme_id=lexeme.id,
            user_id=str(PRIMARY_USER_ID),
            normalized_form="եմ",
        )
    )
    db_session.commit()

    resolution = LexemeResolver().resolve(
        db_session,
        user_id=PRIMARY_USER_ID,
        surface_form="եմ",
        normalized_form="եմ",
        morphological_analyses=[_pie_analysis("եմ", "եմ")],
    )

    assert resolution.selected_dictionary_lemma == "լինել"
    assert resolution.selected_source == "internal_curated_lexeme_forms"
    assert resolution.resolution_status == "resolved_by_curated_lexeme_form"


def test_imported_mapping_form_to_headword_resolves_dictionary_lemma(db_session: Session) -> None:
    source = ReferenceSource(
        id=uuid4(),
        user_id=str(PRIMARY_USER_ID),
        key="imported",
        display_name="Imported Reference",
    )
    db_session.add(source)
    db_session.flush()
    db_session.add(
        ReferenceEntry(
            id=uuid4(),
            source_id=source.id,
            surface_form="եմ",
            normalized_form="եմ",
            metadata_json={"headword": "լինել", "definition": "to be"},
        )
    )
    db_session.commit()

    resolution = LexemeResolver().resolve(
        db_session,
        user_id=PRIMARY_USER_ID,
        surface_form="եմ",
        normalized_form="եմ",
    )

    assert resolution.selected_dictionary_lemma == "լինել"
    assert resolution.selected_source == "imported_reference"
    assert resolution.has_structured_dictionary_lemma is True


def test_morphology_only_does_not_validate_dictionary_lemma(db_session: Session) -> None:
    resolution = LexemeResolver().resolve(
        db_session,
        user_id=PRIMARY_USER_ID,
        surface_form="գրոց",
        normalized_form="գրոց",
        morphological_analyses=[_pie_analysis("գրոց", "գիրք")],
    )

    assert resolution.morphological_lemma == "գիրք"
    assert resolution.selected_dictionary_lemma is None
    assert resolution.has_structured_dictionary_lemma is False


def test_approved_mapping_resolves_before_pie_fallback(db_session: Session) -> None:
    db_session.add(
        LexemeFormMapping(
            id=uuid4(),
            user_id=None,
            surface_form="եմ",
            normalized_surface_form="եմ",
            dictionary_lemma="լինել",
            normalized_dictionary_lemma="լինել",
            pos="AUX",
            language_profile="eastern",
            mapping_type="auxiliary_paradigm",
            source_type="seed_curated",
            source_key="armenian_aux_linel_mvp2",
            review_status="approved",
            confidence=1.0,
        )
    )
    db_session.commit()

    resolution = LexemeResolver().resolve(
        db_session,
        user_id=PRIMARY_USER_ID,
        surface_form="եմ",
        normalized_form="եմ",
        morphological_analyses=[_pie_analysis("եմ", "եմ")],
        language_profile="eastern",
    )

    assert resolution.morphological_lemma == "եմ"
    assert resolution.dictionary_lemma == "լինել"
    assert resolution.dictionary_lemma_source == "armenian_aux_linel_mvp2"
    assert resolution.resolution_type == "resolved_by_approved_lexeme_mapping"
