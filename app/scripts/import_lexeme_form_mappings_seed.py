from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from app.core.database import session_scope
from app.db.models import LexemeFormMapping
from app.utils.text_normalization import normalize_token


SEED_PATH = Path(__file__).resolve().parents[2] / "resources" / "lexeme_form_mappings.seed.csv"


def _value(row: dict[str, str], key: str) -> str:
    return (row.get(key) or "").strip()


def _optional_value(row: dict[str, str], key: str) -> str | None:
    value = _value(row, key)
    return value or None


def _confidence(row: dict[str, str]) -> Decimal | None:
    value = _value(row, "confidence")
    return Decimal(value) if value else None


def import_seed(seed_path: Path = SEED_PATH) -> int:
    imported_count = 0
    with seed_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        with session_scope() as session:
            for row in reader:
                surface_form = _value(row, "surface_form")
                dictionary_lemma = _value(row, "dictionary_lemma")
                normalized_surface_form = _value(row, "normalized_surface_form") or normalize_token(surface_form)
                normalized_dictionary_lemma = _value(row, "normalized_dictionary_lemma") or normalize_token(dictionary_lemma)
                language_profile = _value(row, "language_profile") or "unknown"
                source_type = _value(row, "source_type")
                if not surface_form or not normalized_surface_form or not dictionary_lemma or not normalized_dictionary_lemma:
                    raise ValueError(f"Invalid seed row: {row}")
                existing = session.scalar(
                    select(LexemeFormMapping).where(
                        LexemeFormMapping.normalized_surface_form == normalized_surface_form,
                        LexemeFormMapping.normalized_dictionary_lemma == normalized_dictionary_lemma,
                        LexemeFormMapping.language_profile == language_profile,
                        LexemeFormMapping.source_type == source_type,
                    )
                )
                if existing is None:
                    existing = LexemeFormMapping(
                        user_id=None,
                        surface_form=surface_form,
                        normalized_surface_form=normalized_surface_form,
                        dictionary_lemma=dictionary_lemma,
                        normalized_dictionary_lemma=normalized_dictionary_lemma,
                        language_profile=language_profile,
                        mapping_type=_value(row, "mapping_type"),
                        source_type=source_type,
                    )
                    session.add(existing)
                    imported_count += 1
                existing.surface_form = surface_form
                existing.dictionary_lemma = dictionary_lemma
                existing.pos = _optional_value(row, "pos")
                existing.source_key = _optional_value(row, "source_key")
                existing.confidence = _confidence(row)
                existing.review_status = _value(row, "review_status") or "approved"
                existing.notes = _optional_value(row, "notes")
            session.commit()
    return imported_count


def main() -> None:
    imported_count = import_seed()
    print(f"Imported {imported_count} lexeme form mapping seed rows.")


if __name__ == "__main__":
    main()
