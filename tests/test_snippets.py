from __future__ import annotations

from app.utils.snippets import (
    build_context_snippet_with_highlight,
    context_snippet_highlight_range,
)


def test_highlight_range_avoids_substring_inside_longer_word() -> None:
    text = "homework is a work that has to be done for school"
    char_start = text.index("work", text.index("homework") + 1)
    char_end = char_start + len("work")

    snippet, highlight_start, highlight_end = build_context_snippet_with_highlight(
        text,
        char_start,
        char_end,
    )

    assert snippet[highlight_start:highlight_end] == "work"
    assert "homework" not in snippet[highlight_start:highlight_end]


def test_context_snippet_highlight_range_matches_stored_snippet() -> None:
    text = "homework is a work that has to be done for school"
    char_start = text.index("work", text.index("homework") + 1)
    char_end = char_start + len("work")
    snippet, expected_start, expected_end = build_context_snippet_with_highlight(
        text,
        char_start,
        char_end,
    )

    highlight_start, highlight_end = context_snippet_highlight_range(
        text,
        char_start,
        char_end,
        snippet,
        token="work",
    )

    assert highlight_start == expected_start
    assert highlight_end == expected_end
    assert snippet[highlight_start:highlight_end] == "work"


def test_context_snippet_highlight_range_falls_back_to_word_boundary() -> None:
    snippet = "homework is a work that has to be done for school"
    highlight_start, highlight_end = context_snippet_highlight_range(
        None,
        None,
        None,
        snippet,
        token="work",
    )

    assert highlight_start is not None
    assert highlight_end is not None
    assert snippet[highlight_start:highlight_end] == "work"
    assert highlight_start > snippet.index("homework")
