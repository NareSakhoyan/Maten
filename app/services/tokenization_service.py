from __future__ import annotations

from dataclasses import dataclass

import regex

from app.utils.snippets import build_context_snippet
from app.utils.text_normalization import normalize_token


TOKEN_RE = regex.compile(
    r"[\p{L}\p{M}\p{Nd}]+(?:['’֊-][\p{L}\p{M}\p{Nd}]+)*",
    flags=regex.VERSION1,
)


@dataclass(frozen=True, slots=True)
class TokenMatch:
    token: str
    normalized_token: str
    char_start: int
    char_end: int
    context_snippet: str


class TokenizationService:
    def tokenize(self, text: str) -> list[TokenMatch]:
        matches: list[TokenMatch] = []
        for match in TOKEN_RE.finditer(text):
            token = match.group(0)
            if not any(char.isalpha() for char in token):
                continue

            normalized = normalize_token(token)
            if not normalized:
                continue

            matches.append(
                TokenMatch(
                    token=token,
                    normalized_token=normalized,
                    char_start=match.start(),
                    char_end=match.end(),
                    context_snippet=build_context_snippet(text, match.start(), match.end()),
                )
            )
        return matches


def get_tokenization_service() -> TokenizationService:
    return TokenizationService()

