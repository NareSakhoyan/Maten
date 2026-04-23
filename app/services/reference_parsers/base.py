from __future__ import annotations

from dataclasses import dataclass
import re

from app.core.config import Settings, get_settings
from app.db.models import ReferenceImportMethod
from app.utils.text_normalization import normalize_token, normalize_unicode


MOSTLY_PUNCTUATION_RE = re.compile(r"^[\W_]+$", re.UNICODE)
SIMPLE_SEPARATOR_RE = re.compile(r"\s*(?:;|,|\||/)\s*")


@dataclass(frozen=True, slots=True)
class ParsedReferenceEntry:
    surface_form: str
    normalized_form: str
    metadata_json: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class ReferenceParseResult:
    rows: list[ParsedReferenceEntry]
    rows_read: int
    import_method: ReferenceImportMethod
    warning_message: str | None = None


class ConservativeReferenceTextExtractor:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def entries_from_text_blocks(
        self,
        blocks: list[str],
        *,
        split_simple_separators: bool = True,
    ) -> list[ParsedReferenceEntry]:
        entries: list[ParsedReferenceEntry] = []
        seen_pairs: set[tuple[str, str]] = set()

        for block in blocks:
            for candidate in self._candidate_strings(block, split_simple_separators=split_simple_separators):
                normalized_form = normalize_token(candidate)
                if not normalized_form:
                    continue
                pair = (candidate, normalized_form)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                entries.append(
                    ParsedReferenceEntry(
                        surface_form=candidate,
                        normalized_form=normalized_form,
                    )
                )
        return entries

    def _candidate_strings(self, block: str, *, split_simple_separators: bool) -> list[str]:
        cleaned = normalize_unicode(block).strip()
        if not cleaned:
            return []
        if len(cleaned) > self.settings.reference_import_max_line_length:
            return []
        if MOSTLY_PUNCTUATION_RE.match(cleaned):
            return []

        parts = [cleaned]
        if split_simple_separators and self._can_split_simply(cleaned):
            parts = [part.strip() for part in SIMPLE_SEPARATOR_RE.split(cleaned) if part.strip()]

        candidates: list[str] = []
        for part in parts:
            if not part:
                continue
            if len(part) > self.settings.reference_import_max_line_length:
                continue
            if MOSTLY_PUNCTUATION_RE.match(part):
                continue
            candidates.append(part)
        return candidates

    @staticmethod
    def _can_split_simply(value: str) -> bool:
        if any(separator in value for separator in [";", "|", "/"]):
            return True
        comma_count = value.count(",")
        return comma_count == 1 and len(value.split()) <= 6
