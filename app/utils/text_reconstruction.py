from __future__ import annotations

import regex


ARMENIAN_LINE_END_RE = regex.compile(r"(?<![\p{L}\p{Nd}])(?P<prefix>\p{Script=Armenian}+)-\s*$", flags=regex.VERSION1)
ARMENIAN_LINE_START_RE = regex.compile(
    r"^(?P<leading>\s*)(?P<prefix>\p{Script=Armenian}+)(?P<rest>.*)$",
    flags=regex.VERSION1,
)


def reconstruct_page_text(raw_text: str) -> str:
    if not raw_text:
        return raw_text

    lines = raw_text.split("\n")
    reconstructed_lines: list[str] = []
    index = 0

    while index < len(lines):
        current_line = lines[index]
        while index + 1 < len(lines) and _should_join_lines(current_line, lines[index + 1]):
            current_line = _join_lines(current_line, lines[index + 1])
            index += 1
        reconstructed_lines.append(current_line)
        index += 1

    return "\n".join(reconstructed_lines)


def _should_join_lines(current_line: str, next_line: str) -> bool:
    return ARMENIAN_LINE_END_RE.search(current_line) is not None and ARMENIAN_LINE_START_RE.search(next_line) is not None


def _join_lines(current_line: str, next_line: str) -> str:
    current_match = ARMENIAN_LINE_END_RE.search(current_line)
    next_match = ARMENIAN_LINE_START_RE.search(next_line)
    if current_match is None or next_match is None:  # pragma: no cover - guarded by caller
        return current_line

    current_prefix = current_line[: current_match.start("prefix")]
    joined_word = current_match.group("prefix") + next_match.group("prefix")
    return f"{current_prefix}{joined_word}{next_match.group('rest')}"
