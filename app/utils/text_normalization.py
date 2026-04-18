from __future__ import annotations

from collections.abc import Sequence
import unicodedata

import regex


INLINE_WHITESPACE_RE = regex.compile(r"[^\S\r\n]+")
MULTI_BLANK_LINE_RE = regex.compile(r"\n{3,}")


def normalize_unicode(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def normalize_extracted_text(value: str) -> str:
    normalized = normalize_unicode(value).replace("\r\n", "\n").replace("\r", "\n")

    characters: list[str] = []
    for char in normalized:
        if char in {"\n", "\t"}:
            characters.append(char)
            continue
        if char in {"\u00a0", "\u2007", "\u202f"}:
            characters.append(" ")
            continue
        if unicodedata.category(char).startswith("C"):
            characters.append(" ")
            continue
        characters.append(char)

    cleaned = "".join(characters).replace("\t", " ")
    cleaned = "\n".join(INLINE_WHITESPACE_RE.sub(" ", line).strip() for line in cleaned.split("\n"))
    cleaned = MULTI_BLANK_LINE_RE.sub("\n\n", cleaned)
    return cleaned.strip()


def normalize_token(value: str) -> str:
    normalized = normalize_unicode(value)
    normalized = INLINE_WHITESPACE_RE.sub(" ", normalized)
    return normalized.strip().lower()


def normalize_token_list(values: Sequence[str]) -> list[str]:
    normalized_values: list[str] = []
    seen_values: set[str] = set()
    for value in values:
        normalized = normalize_token(value)
        if not normalized or normalized in seen_values:
            continue
        seen_values.add(normalized)
        normalized_values.append(normalized)
    return normalized_values
