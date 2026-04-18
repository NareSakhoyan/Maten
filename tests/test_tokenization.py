from __future__ import annotations

from app.services.tokenization_service import TokenizationService


def test_tokenization_extracts_armenian_words_and_normalizes_case() -> None:
    service = TokenizationService()

    tokens = service.tokenize("Բարև, աշխարհ։ ՀԱՅ-գիրք 123, Երևան։")

    assert [token.token for token in tokens] == ["Բարև", "աշխարհ", "ՀԱՅ-գիրք", "Երևան"]
    assert [token.normalized_token for token in tokens] == ["բարև", "աշխարհ", "հայ-գիրք", "երևան"]


def test_tokenization_builds_context_snippets() -> None:
    service = TokenizationService()

    tokens = service.tokenize("Սա հին հայկական գրքի նմուշ է։")

    assert tokens[2].token == "հայկական"
    assert "հին հայկական գրքի" in tokens[2].context_snippet

