from __future__ import annotations

from dataclasses import dataclass


LANGUAGE_PROFILES = {"classical", "eastern", "western", "mixed", "unknown"}


@dataclass(frozen=True, slots=True)
class ProviderMetadata:
    provider_key: str
    provider_type: str
    evidence_role: str
    language_profile: str
    priority: int
    can_validate_word: bool | str = False
    can_attest_usage: bool = False
    can_suggest_lemma: bool = False
    can_suggest_named_entity: bool = False
    requires_exact_match: bool = False
    requires_structured_headword: bool = False
    default_runtime: str = "disabled"
    independent_source_group: str | None = None
    source_kind: str = "external"
    notes: str | None = None


PROVIDER_METADATA: dict[str, ProviderMetadata] = {
    "internal_lexicon": ProviderMetadata(
        provider_key="internal_lexicon",
        provider_type="curated_lexicon",
        evidence_role="curated_truth",
        language_profile="mixed",
        priority=1,
        can_validate_word=True,
        requires_exact_match=True,
        default_runtime="local",
        independent_source_group="internal_lexicon",
        source_kind="curated",
    ),
    "imported_references": ProviderMetadata(
        provider_key="imported_references",
        provider_type="imported_dictionary",
        evidence_role="structured_dictionary_headword",
        language_profile="mixed",
        priority=2,
        can_validate_word=True,
        requires_exact_match=True,
        default_runtime="local",
        independent_source_group="imported_references",
        source_kind="dictionary",
    ),
    "imported_reference": ProviderMetadata(
        provider_key="imported_references",
        provider_type="imported_dictionary",
        evidence_role="structured_dictionary_headword",
        language_profile="mixed",
        priority=2,
        can_validate_word=True,
        requires_exact_match=True,
        default_runtime="local",
        independent_source_group="imported_references",
        source_kind="dictionary",
    ),
    "calfa_classical_lexical_db": ProviderMetadata(
        provider_key="calfa_classical_lexical_db",
        provider_type="dictionary",
        evidence_role="structured_dictionary_headword",
        language_profile="classical",
        priority=3,
        can_validate_word=True,
        requires_exact_match=True,
        requires_structured_headword=True,
        default_runtime="disabled",
        independent_source_group="calfa",
        source_kind="dictionary",
    ),
    "pie_classical_morphology": ProviderMetadata(
        provider_key="pie_classical_morphology",
        provider_type="morphology",
        evidence_role="lemma_pos_features",
        language_profile="classical",
        priority=10,
        can_suggest_lemma=True,
        default_runtime="local",
        independent_source_group="pie_dalih",
        source_kind="morphology",
    ),
    "pie_eastern_morphology": ProviderMetadata(
        provider_key="pie_eastern_morphology",
        provider_type="morphology",
        evidence_role="lemma_pos_features",
        language_profile="eastern",
        priority=11,
        can_suggest_lemma=True,
        default_runtime="local",
        independent_source_group="pie_dalih",
        source_kind="morphology",
    ),
    "eanc_eastern_corpus": ProviderMetadata(
        provider_key="eanc_eastern_corpus",
        provider_type="corpus",
        evidence_role="corpus_attestation",
        language_profile="eastern",
        priority=20,
        can_attest_usage=True,
        default_runtime="disabled",
        independent_source_group="eanc",
        source_kind="corpus",
    ),
    "nayiri_western_corpus": ProviderMetadata(
        provider_key="nayiri_western_corpus",
        provider_type="corpus",
        evidence_role="corpus_attestation",
        language_profile="western",
        priority=21,
        can_attest_usage=True,
        default_runtime="local",
        independent_source_group="nayiri_corpus",
        source_kind="corpus",
    ),
    "nayiri_corpus": ProviderMetadata(
        provider_key="nayiri_western_corpus",
        provider_type="corpus",
        evidence_role="corpus_attestation",
        language_profile="western",
        priority=21,
        can_attest_usage=True,
        default_runtime="local",
        independent_source_group="nayiri_corpus",
        source_kind="corpus",
    ),
    "pioner_ner": ProviderMetadata(
        provider_key="pioner_ner",
        provider_type="ner",
        evidence_role="named_entity_signal",
        language_profile="eastern",
        priority=30,
        can_suggest_named_entity=True,
        default_runtime="local",
        independent_source_group="pioner",
        source_kind="ner",
    ),
    "nayiri_web": ProviderMetadata(
        provider_key="nayiri_web",
        provider_type="external_reference",
        evidence_role="classified_external_result",
        language_profile="mixed",
        priority=50,
        can_validate_word="conditional",
        requires_structured_headword=True,
        default_runtime="cached_only",
        independent_source_group="nayiri_web",
        source_kind="external",
    ),
    "strict_validation": ProviderMetadata(
        provider_key="strict_validation",
        provider_type="validation",
        evidence_role="artifact_filter",
        language_profile="mixed",
        priority=0,
        default_runtime="local",
        independent_source_group="strict_validation",
        source_kind="external",
    ),
}


def normalize_language_profile(value: str | None) -> str:
    profile = (value or "").strip().lower()
    if profile in {"grabar", "old_armenian", "xcl", "classical_armenian"}:
        return "classical"
    if profile in {"modern", "ashkharhabar"}:
        return "unknown"
    return profile if profile in LANGUAGE_PROFILES else "unknown"


def provider_metadata(provider_key: str, *, provider_type: str | None = None) -> ProviderMetadata:
    key = (provider_key or "").strip()
    metadata = PROVIDER_METADATA.get(key)
    if metadata is not None:
        return metadata
    normalized_type = (provider_type or "external_reference").strip()
    is_dictionary = normalized_type in {"reference", "imported_dictionary", "dictionary", "external_dictionary", "web_dictionary"}
    return ProviderMetadata(
        provider_key=key or "unknown",
        provider_type="imported_dictionary" if normalized_type == "reference" else normalized_type,
        evidence_role="structured_dictionary_headword" if is_dictionary else "classified_external_result",
        language_profile="unknown",
        priority=100,
        can_validate_word=is_dictionary,
        requires_exact_match=is_dictionary,
        requires_structured_headword=is_dictionary,
        default_runtime="local" if is_dictionary else "disabled",
        independent_source_group=key or "unknown",
        source_kind="dictionary" if is_dictionary else "external",
    )


def profile_weight(source_profile: str | None, document_profile: str | None) -> float:
    source = normalize_language_profile(source_profile)
    document = normalize_language_profile(document_profile)
    if document in {"mixed", "unknown"} or source in {"mixed", "unknown"}:
        return 1.0
    if source == document:
        return 1.0
    if source == "western" and document == "eastern":
        return 0.7
    if source == "eastern" and document == "western":
        return 0.7
    return 0.55
