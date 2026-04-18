from __future__ import annotations

from app.utils.text_normalization import normalize_extracted_text, normalize_token, normalize_unicode


def test_normalize_unicode_applies_nfc() -> None:
    decomposed = "ե\u0582"
    assert normalize_unicode(decomposed) == "եւ"


def test_normalize_extracted_text_cleans_control_chars_and_whitespace() -> None:
    raw = "  Բարև\u0007\tաշխարհ \r\n\r\n\r\n Նոր\tտող  "
    normalized = normalize_extracted_text(raw)
    assert normalized == "Բարև աշխարհ\n\nՆոր տող"


def test_normalize_token_lowercases() -> None:
    assert normalize_token("  ՀԱՅԱՍՏԱՆ  ") == "հայաստան"
