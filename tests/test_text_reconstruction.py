from __future__ import annotations

from app.utils.text_reconstruction import reconstruct_page_text


def test_reconstructs_armenian_hyphenated_line_break_words() -> None:
    assert reconstruct_page_text("աստու-\nած") == "աստուած"
    assert reconstruct_page_text("նախա-\nգիծ") == "նախագիծ"


def test_non_hyphenated_adjacent_lines_do_not_join() -> None:
    assert reconstruct_page_text("աստուած\nգիրք") == "աստուած\nգիրք"


def test_latin_only_line_endings_do_not_join_by_default() -> None:
    assert reconstruct_page_text("LAT-\nIN") == "LAT-\nIN"


def test_mixed_or_punctuation_lines_do_not_over_join() -> None:
    assert reconstruct_page_text("MixԱ-\nգիր") == "MixԱ-\nգիր"
    assert reconstruct_page_text("---\nգիրք") == "---\nգիրք"
