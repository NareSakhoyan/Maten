from __future__ import annotations

import enum
from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.db.models import OccurrenceScriptType
from app.schemas.common import APIModel, OffsetPagination
from app.schemas.lexicon import LexiconGroupState
from app.schemas.reference import ReferenceMatchBest
from app.db.models import ReferenceMatchType
from app.schemas.reference_enums import SupportedReferenceImportMethod


class WordEvidenceSourceType(str, enum.Enum):
    IMPORTED_BOOK = "imported_book"
    REFERENCE_SOURCE = "reference_source"
    LEXICON = "lexicon"
    TRUSTED_EXTERNAL = "trusted_external"
    EXTERNAL_REFERENCE = "trusted_external"


class WordSearchCategory(str, enum.Enum):
    LEXICON = "lexicon"
    IMPORTED_BOOKS = "imported_books"
    REFERENCE_SOURCES = "reference_sources"
    TRUSTED_EXTERNAL = "trusted_external"
    EXTERNAL_SOURCES = "trusted_external"


class WordSearchMode(str, enum.Enum):
    EXACT = "exact"
    NORMALIZED = "normalized"
    FUZZY = "fuzzy"


class TrustedExternalLookupStatus(str, enum.Enum):
    COMPLETED = "completed"
    NO_RESULTS = "no_results"
    UNAVAILABLE = "unavailable"


class DocumentTrustedExternalStatus(str, enum.Enum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    UNCHECKED = "unchecked"
    UNAVAILABLE = "unavailable"


class DocumentTrustedExternalCanonicalizationStatus(str, enum.Enum):
    DIRECT_MATCH = "direct_match"
    CANONICALIZED_BY_NAYIRI = "canonicalized_by_nayiri"
    MORPHOLOGY_ASSISTED = "morphology_assisted"
    UNRESOLVED = "unresolved"


class SourceWordStatusView(str, enum.Enum):
    ALL = "all"
    LINKED = "linked"
    UNLINKED = "unlinked"
    SUSPICIOUS = "suspicious"
    IGNORED = "ignored"


class WordEvidenceItem(APIModel):
    word_form: str
    normalized_form: str
    source_type: WordEvidenceSourceType
    source_id: str
    source_title: str
    source_subtitle: str | None = None
    page_number: int | None = None
    context_snippet: str | None = None
    reference_link: str | None = None
    reference_entry_id: UUID | None = None
    occurrence_id: UUID | None = None
    lexeme_id: UUID | None = None
    lexeme_canonical_form: str | None = None
    has_reference_match: bool = False
    best_reference_match: ReferenceMatchBest | None = None
    extraction_method: str | None = None
    source_import_method: SupportedReferenceImportMethod | None = None
    source_warning: str | None = None
    is_suspicious: bool = False
    suspicion_reasons: list[str] = Field(default_factory=list)
    matched_form: str | None = None
    provider_key: str | None = None
    provider_display_name: str | None = None
    match_type: ReferenceMatchType | None = None
    match_score: float | None = None
    fetched_at: datetime | None = None
    created_at: datetime | None = None


class WordSearchResultGroup(APIModel):
    category: WordSearchCategory
    items: list[WordEvidenceItem]
    total: int
    status: TrustedExternalLookupStatus | None = None


class WordSearchResponse(APIModel):
    query: str
    normalized_query: str
    mode: WordSearchMode
    groups: list[WordSearchResultGroup]


class RelatedLexemeSummary(APIModel):
    lexeme_id: UUID
    canonical_form: str
    canonical_normalized_form: str
    occurrence_count: int = 0


class WordEvidenceSummary(APIModel):
    total_hits: int
    source_count: int
    linked_lexeme_id: UUID | None = None
    linked_lexeme_canonical_form: str | None = None
    best_lemma: str | None = None
    lemma_candidates: list[str] = Field(default_factory=list)
    pos_candidates: list[str] = Field(default_factory=list)
    morphology_available: bool = False


class WordEvidenceExternalSummary(APIModel):
    total_hits: int
    provider_count: int
    status: TrustedExternalLookupStatus


class WordEvidenceResponse(OffsetPagination):
    normalized_form: str
    summary: WordEvidenceSummary
    evidence_items: list[WordEvidenceItem]
    external_summary: WordEvidenceExternalSummary | None = None
    external_evidence_items: list[WordEvidenceItem] = Field(default_factory=list)
    related_reference_matches: list[ReferenceMatchBest] | None = None
    related_lexeme_summary: RelatedLexemeSummary | None = None


class DocumentWordCandidateSummary(APIModel):
    source_type: WordEvidenceSourceType = WordEvidenceSourceType.IMPORTED_BOOK
    source_id: str
    source_title: str
    source_subtitle: str | None = None
    reference_link: str | None = None
    normalized_form: str
    occurrence_count: int
    page_count: int
    sample_tokens: list[str]
    sample_contexts: list[str]
    sample_pages: list[int]
    linked_lexeme_id: UUID | None
    linked_lexeme_canonical_form: str | None
    group_state: LexiconGroupState
    dominant_script_type: OccurrenceScriptType
    is_suspicious: bool
    suspicion_reasons: list[str]
    has_reference_match: bool = False
    reference_match_count: int = 0
    best_reference_match: ReferenceMatchBest | None = None
    trusted_external_status: DocumentTrustedExternalStatus = DocumentTrustedExternalStatus.UNCHECKED
    trusted_external_provider_display_name: str | None = None
    trusted_external_match_count: int = 0
    trusted_external_matched_form: str | None = None
    trusted_external_source_title: str | None = None
    trusted_external_reference_link: str | None = None
    trusted_external_snippet: str | None = None
    trusted_external_canonicalization_status: DocumentTrustedExternalCanonicalizationStatus = (
        DocumentTrustedExternalCanonicalizationStatus.UNRESOLVED
    )


class DocumentNayiriLookupSummary(APIModel):
    found_count: int = 0
    not_found_count: int = 0
    unchecked_count: int = 0
    unavailable_count: int = 0
    total_forms: int = 0


class DocumentNayiriLookupRunStartResponse(APIModel):
    message: str
    run_id: UUID
    job_id: UUID


class DocumentWordCandidateListResponse(OffsetPagination):
    items: list[DocumentWordCandidateSummary]


class ReferenceSourceWordCandidateSummary(APIModel):
    source_type: WordEvidenceSourceType = WordEvidenceSourceType.REFERENCE_SOURCE
    source_id: str
    source_title: str
    source_subtitle: str | None = None
    reference_link: str | None = None
    reference_entry_id: UUID
    surface_form: str
    normalized_form: str
    import_method: SupportedReferenceImportMethod | None = None
    warning_message: str | None = None
    linked_lexeme_id: UUID | None = None
    linked_lexeme_canonical_form: str | None = None
    matching_lexeme_count: int = 0


class ReferenceSourceWordCandidateSourceSummary(APIModel):
    source_type: WordEvidenceSourceType = WordEvidenceSourceType.REFERENCE_SOURCE
    source_id: str
    source_title: str
    source_subtitle: str | None = None
    reference_link: str | None = None
    import_method: SupportedReferenceImportMethod | None = None
    warning_message: str | None = None
    imported_entry_count: int | None = None
    matched_entry_count: int | None = None
    unmatched_entry_count: int | None = None


class ReferenceSourceWordCandidateListResponse(OffsetPagination):
    source: ReferenceSourceWordCandidateSourceSummary
    items: list[ReferenceSourceWordCandidateSummary]


class WordCheckLexeme(APIModel):
    lexeme_id: UUID
    canonical_form: str
    canonical_normalized_form: str


class TrustedExternalWordCheckSource(APIModel):
    provider_display_name: str
    matched_form: str
    reference_link: str | None = None


class WordCheckResponse(APIModel):
    query: str
    normalized_query: str
    exists_in_lexicon: bool
    matching_lexeme_count: int
    matching_lexemes: list[WordCheckLexeme]
    found_in_imported_books: bool = False
    found_in_reference_sources: bool = False
    found_in_trusted_external: bool = False
    trusted_external_status: TrustedExternalLookupStatus | None = None
    trusted_external_match_count: int = 0
    trusted_external_sources: list[TrustedExternalWordCheckSource] = Field(default_factory=list)


class NayiriCorpusMatchRead(APIModel):
    normalized_query: str
    canonical_form: str
    token_count: int
    source_count: int


class NayiriCorpusCheckResponse(APIModel):
    query: str
    normalized_query: str
    found: bool = False
    matches: list[NayiriCorpusMatchRead] = Field(default_factory=list)
