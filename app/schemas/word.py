from __future__ import annotations

import enum
from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.db.models import OccurrenceScriptType
from app.schemas.common import APIModel, OffsetPagination
from app.schemas.lexicon import LexiconGroupState
from app.schemas.reference import ReferenceMatchBest
from app.schemas.reference_enums import SupportedReferenceImportMethod


class WordEvidenceSourceType(str, enum.Enum):
    IMPORTED_BOOK = "imported_book"
    REFERENCE_SOURCE = "reference_source"
    LEXICON = "lexicon"
    EXTERNAL_REFERENCE = "external_reference"


class WordSearchCategory(str, enum.Enum):
    LEXICON = "lexicon"
    IMPORTED_BOOKS = "imported_books"
    REFERENCE_SOURCES = "reference_sources"
    EXTERNAL_SOURCES = "external_sources"


class WordSearchMode(str, enum.Enum):
    EXACT = "exact"
    NORMALIZED = "normalized"
    FUZZY = "fuzzy"


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
    created_at: datetime | None = None


class WordSearchResultGroup(APIModel):
    category: WordSearchCategory
    items: list[WordEvidenceItem]
    total: int


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


class WordEvidenceResponse(OffsetPagination):
    normalized_form: str
    summary: WordEvidenceSummary
    evidence_items: list[WordEvidenceItem]
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


class WordCheckResponse(APIModel):
    query: str
    normalized_query: str
    exists_in_lexicon: bool
    matching_lexeme_count: int
    matching_lexemes: list[WordCheckLexeme]
    found_in_imported_books: bool = False
    found_in_reference_sources: bool = False
