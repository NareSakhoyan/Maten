from __future__ import annotations

from app.db.models import OccurrenceScriptType
from app.utils.token_classification import classify_token, suspicion_reasons_for_script_type


def test_classify_token_detects_armenian_latin_mixed_digit_mixed_and_other() -> None:
    assert classify_token("Հայաստան").script_type is OccurrenceScriptType.ARMENIAN
    assert classify_token("LATIN").script_type is OccurrenceScriptType.LATIN
    assert classify_token("MixԱ").script_type is OccurrenceScriptType.MIXED
    assert classify_token("A12").script_type is OccurrenceScriptType.DIGIT_MIXED
    assert classify_token("—").script_type is OccurrenceScriptType.OTHER


def test_classify_token_sets_flags_and_length() -> None:
    classification = classify_token("A12")

    assert classification.has_digits is True
    assert classification.has_latin is True
    assert classification.has_armenian is False
    assert classification.token_length == 3


def test_suspicion_reason_maps_from_script_type() -> None:
    assert suspicion_reasons_for_script_type(OccurrenceScriptType.LATIN) == ["dominant script is Latin"]
    assert suspicion_reasons_for_script_type(OccurrenceScriptType.ARMENIAN) == []
