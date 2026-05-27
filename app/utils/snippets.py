from __future__ import annotations

def build_context_snippet(text: str, char_start: int, char_end: int, radius: int = 25) -> str:
    snippet, _, _ = build_context_snippet_with_highlight(text, char_start, char_end, radius=radius)
    return snippet


def _is_word_char(character: str) -> bool:
    return bool(character) and (character.isalnum() or character == "_")


def _map_indices_through_whitespace_collapse(segment: str, start: int, end: int) -> tuple[str, int | None, int | None]:
    if start < 0 or end <= start or end > len(segment):
        return " ".join(segment.split()), None, None

    output: list[str] = []
    highlight_start: int | None = None
    highlight_end: int | None = None
    output_index = 0
    index = 0

    while index < len(segment):
        if segment[index].isspace():
            while index < len(segment) and segment[index].isspace():
                index += 1
            if output and output[-1] != " ":
                output.append(" ")
                output_index += 1
            continue

        if index == start:
            highlight_start = output_index
        if index < end:
            highlight_end = output_index + 1

        output.append(segment[index])
        output_index += 1
        index += 1

    return "".join(output), highlight_start, highlight_end


def build_context_snippet_with_highlight(
    text: str,
    char_start: int,
    char_end: int,
    radius: int = 25,
) -> tuple[str, int | None, int | None]:
    page_start = max(0, char_start - radius)
    page_end = min(len(text), char_end + radius)

    segment = text[page_start:page_end].replace("\n", " ")
    token_start = char_start - page_start
    token_end = char_end - page_start
    collapsed, highlight_start, highlight_end = _map_indices_through_whitespace_collapse(
        segment,
        token_start,
        token_end,
    )

    snippet = collapsed
    if page_start > 0:
        snippet = f"...{snippet}"
        if highlight_start is not None:
            highlight_start += 3
        if highlight_end is not None:
            highlight_end += 3
    if page_end < len(text):
        snippet = f"{snippet}..."

    return snippet, highlight_start, highlight_end


def _word_boundary_matches(snippet: str, token: str) -> list[tuple[int, int]]:
    if not token:
        return []

    matches: list[tuple[int, int]] = []
    search_from = 0
    while search_from < len(snippet):
        index = snippet.find(token, search_from)
        if index < 0:
            break

        before_ok = index == 0 or not _is_word_char(snippet[index - 1])
        after_index = index + len(token)
        after_ok = after_index >= len(snippet) or not _is_word_char(snippet[after_index])
        if before_ok and after_ok:
            matches.append((index, after_index))

        search_from = index + 1

    return matches


def _closest_word_boundary_match(
    snippet: str,
    token: str,
    expected_start: int | None,
) -> tuple[int | None, int | None]:
    matches = _word_boundary_matches(snippet, token)
    if not matches:
        return None, None
    if expected_start is None:
        return matches[0]

    start, end = min(matches, key=lambda match: abs(match[0] - expected_start))
    return start, end


def context_snippet_highlight_range(
    text: str | None,
    char_start: int | None,
    char_end: int | None,
    context_snippet: str,
    *,
    token: str | None = None,
    radius: int = 25,
) -> tuple[int | None, int | None]:
    if not context_snippet:
        return None, None

    resolved_token = token
    if text and char_start is not None and char_end is not None and char_end > char_start:
        resolved_token = text[char_start:char_end]

    if char_start is None or char_end is None or char_end <= char_start or not text:
        return _closest_word_boundary_match(context_snippet, resolved_token or "", None)

    rebuilt, highlight_start, highlight_end = build_context_snippet_with_highlight(
        text,
        char_start,
        char_end,
        radius=radius,
    )
    if rebuilt == context_snippet and highlight_start is not None and highlight_end is not None:
        return highlight_start, highlight_end

    resolved_token = text[char_start:char_end]
    expected_start = highlight_start
    if rebuilt != context_snippet and expected_start is not None and rebuilt.startswith("..."):
        # Stored snippets may have been built with a different radius; keep relative position when possible.
        expected_start = max(0, expected_start)

    return _closest_word_boundary_match(context_snippet, resolved_token, expected_start)

