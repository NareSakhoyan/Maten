from __future__ import annotations

from dataclasses import dataclass
from typing import Final
import unicodedata

from app.db.models import OccurrenceScriptType


@dataclass(frozen=True, slots=True)
class TokenClassification:
    script_type: OccurrenceScriptType
    has_digits: bool
    has_latin: bool
    has_armenian: bool
    token_length: int


SUSPICIOUS_REASON_BY_SCRIPT_TYPE: Final[dict[OccurrenceScriptType, str]] = {
    OccurrenceScriptType.LATIN: "dominant script is Latin",
    OccurrenceScriptType.MIXED: "token mixes Armenian and Latin scripts",
    OccurrenceScriptType.DIGIT_MIXED: "token mixes digits and letters",
    OccurrenceScriptType.OTHER: "dominant script is not Armenian",
}


def classify_token(token: str) -> TokenClassification:
    letters = [char for char in token if unicodedata.category(char).startswith("L")]
    has_armenian = any(_is_armenian_letter(char) for char in letters)
    has_latin = any(_is_latin_letter(char) for char in letters)
    has_digits = any(char.isdigit() for char in token)

    if has_digits and letters:
        script_type = OccurrenceScriptType.DIGIT_MIXED
    elif letters and all(_is_armenian_letter(char) for char in letters):
        script_type = OccurrenceScriptType.ARMENIAN
    elif letters and all(_is_latin_letter(char) for char in letters):
        script_type = OccurrenceScriptType.LATIN
    elif has_armenian and has_latin:
        script_type = OccurrenceScriptType.MIXED
    else:
        script_type = OccurrenceScriptType.OTHER

    return TokenClassification(
        script_type=script_type,
        has_digits=has_digits,
        has_latin=has_latin,
        has_armenian=has_armenian,
        token_length=len(token),
    )


def is_suspicious_script_type(script_type: OccurrenceScriptType | str) -> bool:
    return OccurrenceScriptType(script_type) is not OccurrenceScriptType.ARMENIAN


def suspicion_reasons_for_script_type(script_type: OccurrenceScriptType | str) -> list[str]:
    normalized = OccurrenceScriptType(script_type)
    reason = SUSPICIOUS_REASON_BY_SCRIPT_TYPE.get(normalized)
    return [reason] if reason else []


def _is_armenian_letter(char: str) -> bool:
    if not unicodedata.category(char).startswith("L"):
        return False
    return "ARMENIAN" in unicodedata.name(char, "")


def _is_latin_letter(char: str) -> bool:
    if not unicodedata.category(char).startswith("L"):
        return False
    return "LATIN" in unicodedata.name(char, "")
