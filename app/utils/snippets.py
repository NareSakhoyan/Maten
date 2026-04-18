from __future__ import annotations


def build_context_snippet(text: str, char_start: int, char_end: int, radius: int = 25) -> str:
    start = max(0, char_start - radius)
    end = min(len(text), char_end + radius)

    snippet = text[start:end].replace("\n", " ")
    snippet = " ".join(snippet.split())
    if start > 0:
        snippet = f"...{snippet}"
    if end < len(text):
        snippet = f"{snippet}..."
    return snippet

