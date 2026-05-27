from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.schemas.common import APIModel, OffsetPagination


class DiscoveryBuildSummary(APIModel):
    total_grouped_forms: int = 0
    resolved_known: int = 0
    resolved_by_dictionary: int = 0
    attested_in_corpus: int = 0
    resolved_by_lemma: int = 0
    resolved_as_variant: int = 0
    weakly_attested: int = 0
    poorly_defined: int = 0
    unknown_plausible: int = 0
    possible_ocr_noise: int = 0
    probable_ocr_noise: int = 0
    possible_named_entity: int = 0
    conflicting_sources: int = 0
    needs_linguist_research: int = 0
    suppressed: int = 0
    shown_in_queue: int = 0


class DiscoveryBuildResponse(APIModel):
    message: str
    summary: DiscoveryBuildSummary


class DiscoveryBuildStartResponse(APIModel):
    message: str
    run_id: UUID
    job_id: UUID


class DiscoveryCandidateRead(APIModel):
    id: UUID
    document_id: UUID
    normalized_form: str
    canonical_form_candidate: str | None = None
    occurrence_count: int
    page_count: int
    sample_tokens: list[str] = Field(default_factory=list)
    sample_contexts: list[str] = Field(default_factory=list)
    sample_pages: list[int] = Field(default_factory=list)
    resolution_status: str
    candidate_type: str
    interest_score: float
    confidence_score: float | None = None
    ocr_risk_score: float | None = None
    morphology_plausibility_score: float | None = None
    definition_quality_score: float | None = None
    best_evidence_summary: dict[str, object] = Field(default_factory=dict)
    review_status: str
    reviewer_decision: str | None = None
    reviewer_note: str | None = None
    linked_lexeme_id: UUID | None = None
    created_at: datetime
    updated_at: datetime


class DiscoveryCandidateListResponse(OffsetPagination):
    items: list[DiscoveryCandidateRead]


class DiscoveryBuildRunRead(APIModel):
    id: UUID
    status: str
    build_mode: str = "full"
    reference_source_id: UUID | None = None
    reference_source_import_id: UUID | None = None
    candidate_count: int
    shown_count: int
    suppressed_count: int
    matched_count: int = 0
    unmatched_count: int = 0
    progress_percent: int
    current_stage_code: str | None = None
    current_stage_label: str | None = None
    stage_message_user: str | None = None
    error_message_user: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class DiscoverySummaryResponse(APIModel):
    total_candidates: int
    visible_candidates: int
    suppressed_candidates: int
    reviewed_candidates: int
    unreviewed_candidates: int
    by_candidate_type: dict[str, int] = Field(default_factory=dict)
    by_resolution_status: dict[str, int] = Field(default_factory=dict)
    by_review_status: dict[str, int] = Field(default_factory=dict)
    latest_build: DiscoveryBuildRunRead | None = None
    reference_evidence_states: list["DocumentReferenceEvidenceState"] = Field(default_factory=list)


class DocumentReferenceEvidenceState(APIModel):
    document_id: UUID
    reference_source_id: UUID
    reference_source_import_id: UUID
    source_display_name: str
    status: str
    last_checked_at: datetime | None = None
    matched_count: int = 0
    unmatched_count: int = 0
    error: str | None = None


class DiscoveryCandidateDetailResponse(APIModel):
    candidate: DiscoveryCandidateRead
    why_shown: list[str] = Field(default_factory=list)
    provider_evidence: list["DiscoveryEvidenceItem"] = Field(default_factory=list)
    occurrence_evidence: list["DiscoveryOccurrenceEvidence"] = Field(default_factory=list)
    morphology: dict[str, object] = Field(default_factory=dict)
    decision: dict[str, object] = Field(default_factory=dict)


class DiscoveryEvidenceItem(APIModel):
    provider_key: str
    provider_type: str
    evidence_role: str
    role: str
    query_form: str
    matched_form: str | None = None
    result_headword: str | None = None
    lemma: str | None = None
    match_type: str = "none"
    validation_strength: str = "does_not_validate"
    evidence_strength: str = "none"
    definition_quality: str = "unknown"
    language_profile: str | None = None
    priority: int | None = None
    can_validate_word: bool | str | None = None
    can_attest_usage: bool | None = None
    can_suggest_lemma: bool | None = None
    can_suggest_named_entity: bool | None = None
    requires_exact_match: bool | None = None
    requires_structured_headword: bool | None = None
    default_runtime: str | None = None
    independent_source_group: str | None = None
    source_kind: str | None = None
    confidence: float | None = None
    is_exact_match: bool = False
    is_substring_match: bool = False
    is_fuzzy_match: bool = False
    is_canonical_match: bool = False
    citation: str | None = None
    payload: dict[str, object] = Field(default_factory=dict)


class DiscoveryOccurrenceEvidence(APIModel):
    token: str
    normalized_token: str
    page_number: int
    context_snippet: str
    char_start: int | None = None
    char_end: int | None = None
    context_highlight_start: int | None = None
    context_highlight_end: int | None = None


class DiscoveryCandidateDecisionRequest(APIModel):
    decision: str
    note: str | None = None
    linked_lexeme_id: UUID | None = None
    create_lexeme_canonical_form: str | None = None
    create_lexeme_definition: str | None = None


class DiscoveryCandidateDecisionResponse(APIModel):
    candidate: DiscoveryCandidateRead
    message: str
