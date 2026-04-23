from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.db.models import ReferenceMatchType
from app.schemas.word import WordSearchMode


@dataclass(slots=True)
class ExternalEvidenceItem:
    provider_key: str
    provider_display_name: str
    matched_form: str
    normalized_form: str | None
    source_title: str | None = None
    source_subtitle: str | None = None
    snippet: str | None = None
    reference_link: str | None = None
    match_type: ReferenceMatchType = ReferenceMatchType.NORMALIZED
    match_score: float | None = None
    metadata_json: dict[str, Any] | None = field(default=None)
    fetched_at: datetime | None = None
    created_at: datetime | None = None


class ExternalLookupProviderError(RuntimeError):
    """Raised when a trusted external provider cannot complete a lookup."""


class ExternalLookupProvider(ABC):
    @abstractmethod
    def provider_key(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def provider_display_name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def search_word(
        self,
        *,
        query: str,
        normalized_query: str,
        mode: WordSearchMode,
    ) -> list[ExternalEvidenceItem]:
        raise NotImplementedError
