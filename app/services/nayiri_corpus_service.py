from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import re

from app.utils.text_normalization import normalize_token


_TOKEN_PATTERN = re.compile(r"\[\[(.*?)>>>\s*(.*?)\]\]")


@dataclass(frozen=True, slots=True)
class NayiriCorpusMatch:
    normalized_query: str
    canonical_form: str
    token_count: int
    source_count: int


class NayiriCorpusService:
    def __init__(self, corpus_root: Path | None = None) -> None:
        if corpus_root is None:
            corpus_root = Path(__file__).resolve().parents[3] / "nayiri-corpus-of-western-armenian-2026-02-25-v2"
        self.corpus_root = corpus_root

    def lookup(self, query: str, *, limit: int = 8) -> list[NayiriCorpusMatch]:
        normalized_query = normalize_token(query)
        if not normalized_query:
            return []
        index = self._index(self.corpus_root.resolve())
        lemma_counts = index.lemma_counts.get(normalized_query)
        if not lemma_counts:
            return []
        source_map = index.lemma_sources.get(normalized_query, {})
        ranked = sorted(
            lemma_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )
        return [
            NayiriCorpusMatch(
                normalized_query=normalized_query,
                canonical_form=lemma,
                token_count=count,
                source_count=len(source_map.get(lemma, set())),
            )
            for lemma, count in ranked[:limit]
        ]

    @staticmethod
    @lru_cache(maxsize=1)
    def _index(corpus_root: Path):
        lemma_counts: dict[str, Counter[str]] = defaultdict(Counter)
        lemma_sources: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))

        data_store = corpus_root / "data-store"
        if not data_store.exists():
            return _CorpusIndex(lemma_counts=dict(lemma_counts), lemma_sources=dict(lemma_sources))

        for file_path in sorted(data_store.glob("*.txt")):
            text = file_path.read_text(encoding="utf-8", errors="ignore")
            source_id = file_path.stem
            for surface_raw, annotation_raw in _TOKEN_PATTERN.findall(text):
                surface = normalize_token(surface_raw.strip())
                if not surface:
                    continue
                annotation = annotation_raw.strip()
                lemma_candidate = annotation.split("@", maxsplit=1)[0].strip()
                lemma_candidate = lemma_candidate.split()[0] if lemma_candidate else ""
                lemma = normalize_token(lemma_candidate) or normalize_token(surface_raw.strip())
                if not lemma:
                    continue
                lemma_counts[surface][lemma] += 1
                lemma_sources[surface][lemma].add(source_id)

        return _CorpusIndex(lemma_counts=dict(lemma_counts), lemma_sources=dict(lemma_sources))


@dataclass(frozen=True, slots=True)
class _CorpusIndex:
    lemma_counts: dict[str, Counter[str]]
    lemma_sources: dict[str, dict[str, set[str]]]


@lru_cache(maxsize=1)
def get_nayiri_corpus_service() -> NayiriCorpusService:
    return NayiriCorpusService()
