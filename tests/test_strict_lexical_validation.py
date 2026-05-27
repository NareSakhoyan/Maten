from __future__ import annotations

from app.db.models import OccurrenceScriptType
from app.services.discovery.resolution_engine import EvidenceResult, ResolutionEngine, ResolutionInput
from app.services.validation.canonical_form_resolver import CanonicalFormResolver
from app.services.validation.lexical_match_classifier import LexicalMatchClassifier


def _resolve(normalized_form: str, evidence: list[EvidenceResult], **overrides):
    data = {
        "normalized_form": normalized_form,
        "occurrence_count": 1,
        "page_count": 1,
        "dominant_script_type": OccurrenceScriptType.ARMENIAN,
        "evidence": evidence,
        "has_armenian": True,
        "has_digits": False,
        "morphology_plausible": False,
        "morphology_lemma_known": False,
    }
    data.update(overrides)
    return ResolutionEngine().resolve(ResolutionInput(**data))


def test_single_armenian_letter_web_snippet_does_not_validate() -> None:
    classification = LexicalMatchClassifier().classify(
        query_form="բ",
        provider_role="web_dictionary",
        snippet="Բառեր որոնք կը պարունակեն բ տառը",
    )

    assert classification.match_type == "rejected_artifact"
    assert classification.validation_strength == "rejects"

    result = _resolve(
        "բ",
        [
            EvidenceResult(
                provider_key="nayiri_web",
                provider_type="ambiguous_external",
                evidence_role="ambiguous_external",
                query_form="բ",
                match_type=classification.match_type.value,
                validation_strength=classification.validation_strength.value,
                evidence_strength=classification.evidence_strength,
                definition_quality=classification.definition_quality,
                confidence=classification.confidence_score,
            )
        ],
    )
    assert result.resolution_status == "probable_ocr_noise"


def test_single_armenian_letter_curated_exact_validates() -> None:
    classification = LexicalMatchClassifier().classify(
        query_form="բ",
        provider_role="curated_lexicon",
        result_headword="բ",
        definition_quality="good",
        allow_short_token=True,
    )

    assert classification.match_type == "exact_headword_match"
    assert classification.validation_strength == "validates_word"


def test_substring_inside_longer_word_does_not_validate() -> None:
    classification = LexicalMatchClassifier().classify(
        query_form="բառ",
        provider_role="web_dictionary",
        result_headword="բառարան",
        snippet="բառ appears in a longer article title",
    )

    assert classification.match_type == "substring_match"
    assert classification.validation_strength == "does_not_validate"


def test_imported_reference_exact_headword_validates() -> None:
    classification = LexicalMatchClassifier().classify(
        query_form="գիրք",
        provider_role="imported_reference",
        result_headword="Գիրք",
        definition_quality="good",
    )

    assert classification.match_type == "exact_headword_match"
    assert classification.confidence_score == 0.95


def test_local_corpus_exact_boundary_is_attestation_not_dictionary() -> None:
    classification = LexicalMatchClassifier().classify(
        query_form="վկայուած",
        provider_role="corpus_attestation",
        matched_form="վկայուած",
        corpus_token_count=4,
        corpus_source_count=1,
    )

    assert classification.match_type == "corpus_token_attestation"
    assert classification.validation_strength == "supports_word"


def test_fuzzy_match_is_only_ocr_suggestion() -> None:
    classification = LexicalMatchClassifier().classify(
        query_form="գիրք",
        provider_role="web_dictionary",
        matched_form="գիր",
        source_match_type="fuzzy",
    )

    assert classification.match_type == "fuzzy_ocr_candidate"
    assert classification.validation_strength == "suggests_candidate"


def test_named_entity_signal_is_not_lexical_validation() -> None:
    classification = LexicalMatchClassifier().classify(
        query_form="երեւան",
        provider_role="named_entity_signal",
        matched_form="Երեւան",
    )

    assert classification.match_type == "named_entity_signal"
    assert classification.validation_strength == "suggests_candidate"
    assert classification.validation_strength != "validates_word"


def test_morphology_without_confirming_source_is_unknown_plausible() -> None:
    result = _resolve(
        "գրոց",
        [
            EvidenceResult(
                provider_key="pie_classical_morphology",
                provider_type="morphology",
                evidence_role="morphology_analysis",
                query_form="գրոց",
                lemma="գիրք",
                match_type="morphology_analysis_only",
                validation_strength="suggests_candidate",
                evidence_strength="weak",
                definition_quality="unknown",
                confidence=0.35,
            )
        ],
        morphology_plausible=True,
    )

    assert result.resolution_status == "unknown_plausible"


def test_morphology_lemma_plus_dictionary_headword_resolves_by_lemma() -> None:
    result = _resolve(
        "գրոց",
        [
            EvidenceResult(
                provider_key="imported_references",
                provider_type="imported_dictionary",
                evidence_role="structured_dictionary_headword",
                query_form="գրոց",
                lemma="գիրք",
                result_headword="գիրք",
                match_type="exact_lemma_match",
                validation_strength="supports_word",
                evidence_strength="medium",
                definition_quality="good",
                confidence=0.8,
            )
        ],
        occurrence_count=2,
        morphology_plausible=True,
    )

    assert result.resolution_status == "resolved_by_lemma"


def test_canonical_resolver_reports_conflicting_sources() -> None:
    resolution = CanonicalFormResolver().resolve(
        normalized_form="ձեւ",
        evidence=[
            EvidenceResult(
                provider_key="reference_a",
                provider_type="reference",
                evidence_role="imported_reference",
                query_form="ձեւ",
                matched_form="ձեւ",
                match_type="exact_headword_match",
                validation_strength="validates_word",
                evidence_strength="strong",
                definition_quality="good",
                confidence=0.95,
            ),
            EvidenceResult(
                provider_key="reference_b",
                provider_type="reference",
                evidence_role="imported_reference",
                query_form="ձեւ",
                matched_form="ձեւել",
                match_type="exact_headword_match",
                validation_strength="validates_word",
                evidence_strength="strong",
                definition_quality="good",
                confidence=0.95,
            ),
        ],
    )

    assert resolution.reason == "conflicting_sources"
    assert resolution.canonical_form is None


def test_digit_mixed_token_is_rejected_unless_curated_exact() -> None:
    rejected = LexicalMatchClassifier().classify(
        query_form="2ն",
        provider_role="web_dictionary",
        result_headword="2ն",
        has_digits=True,
    )
    curated = LexicalMatchClassifier().classify(
        query_form="2ն",
        provider_role="curated_lexicon",
        result_headword="2ն",
        definition_quality="good",
        has_digits=True,
        allow_short_token=True,
    )

    assert rejected.match_type == "rejected_artifact"
    assert curated.validation_strength == "validates_word"
