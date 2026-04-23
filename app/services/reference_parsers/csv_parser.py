from __future__ import annotations

import csv
from io import StringIO
import re

from app.db.models import ReferenceImportMethod
from app.utils.text_normalization import normalize_unicode
from app.utils.text_normalization import normalize_token

from .base import ParsedReferenceEntry, ReferenceParseResult


HEADER_NORMALIZER_RE = re.compile(r"[\s\-]+")
SENTENCE_END_RE = re.compile(r"[.!?։…]")


class CsvReferenceParser:
    def parse(self, content: bytes) -> ReferenceParseResult:
        text = content.decode("utf-8-sig")
        reader = csv.DictReader(StringIO(text))
        normalized_header_map = self._normalized_header_map(reader.fieldnames or [])
        surface_field = normalized_header_map.get("surface_form")
        normalized_field = normalized_header_map.get("normalized_form")
        implicit_surface_field = None
        if surface_field is None and normalized_field is None:
            if len(normalized_header_map) == 1:
                implicit_surface_field = next(iter(normalized_header_map.values()))
            else:
                raise ValueError("CSV reference imports require a 'surface_form' or 'normalized_form' column.")
        if surface_field is None and normalized_field is None and implicit_surface_field is None:
            raise ValueError("CSV reference imports require a 'surface_form' or 'normalized_form' column.")

        rows: list[ParsedReferenceEntry] = []
        seen_pairs: set[tuple[str, str]] = set()
        rows_read = 0
        for row in reader:
            rows_read += 1
            row_data = row or {}
            surface_form = ((row_data.get(surface_field) if surface_field is not None else None) or "").strip()
            normalized_input = ((row_data.get(normalized_field) if normalized_field is not None else None) or "").strip()
            if implicit_surface_field is not None:
                surface_form = (row_data.get(implicit_surface_field) or "").strip()
                if surface_form and not self._looks_like_word_entry(surface_form):
                    continue
            if not surface_form and not normalized_input:
                continue

            if not surface_form:
                surface_form = normalized_input
            normalized_form = normalize_token(normalized_input or surface_form)
            if not surface_form or not normalized_form:
                continue
            pair = (surface_form, normalized_form)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            rows.append(
                ParsedReferenceEntry(
                    surface_form=surface_form,
                    normalized_form=normalized_form,
                )
            )

        return ReferenceParseResult(
            rows=rows,
            rows_read=rows_read,
            import_method=ReferenceImportMethod.CSV,
        )

    @staticmethod
    def _normalized_header_map(fieldnames: list[str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for fieldname in fieldnames:
            cleaned = (fieldname or "").strip()
            if not cleaned:
                continue
            normalized_name = HEADER_NORMALIZER_RE.sub("_", cleaned.lower())
            normalized[normalized_name] = cleaned
        return normalized

    @staticmethod
    def _looks_like_word_entry(value: str) -> bool:
        cleaned = normalize_unicode(value).strip()
        if not cleaned:
            return False
        if SENTENCE_END_RE.search(cleaned):
            return False
        token_count = len([part for part in cleaned.split() if part])
        return token_count <= 1
