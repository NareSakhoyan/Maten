from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import NerEntityEntry, NerSource
from app.utils.text_normalization import normalize_token


PIONER_PROVIDER_KEY = "pioner_ner"
PIONER_DATASET_NAME = "Karavet/pioNER-Armenian-Named-Entity"
PIONER_SOURCE_URL = "https://github.com/ispras-texterra/pioner"
PIONER_GITHUB_DATA_URL = "https://github.com/ispras-texterra/pioner"
PIONER_LICENSE = "Apache-2.0"
PIONER_RESOURCE_KEY = "pioner_ner"
CONLL_SUFFIXES = (".conll", ".conll03", ".txt")
SUPPORTED_ENTITY_TYPES = {"PER", "ORG", "LOC"}
SAMPLE_CONTEXT_LIMIT = 5


@dataclass(slots=True)
class EntityAccumulator:
    entity_surface: str
    normalized_surface: str
    entity_type: str
    occurrence_count: int = 0
    sample_contexts: list[str] = field(default_factory=list)

    def add(self, *, context: str) -> None:
        self.occurrence_count += 1
        if context and context not in self.sample_contexts and len(self.sample_contexts) < SAMPLE_CONTEXT_LIMIT:
            self.sample_contexts.append(context)


@dataclass(frozen=True, slots=True)
class PionerImportSummary:
    source_id: UUID
    source_kind: str
    dataset_split: str
    sentence_count: int
    entity_count: int
    entry_count: int
    skipped_entity_count: int


class PionerImportService:
    def import_local_directory(
        self,
        session: Session,
        *,
        input_dir: Path,
        replace: bool = True,
    ) -> list[PionerImportSummary]:
        if not input_dir.is_dir():
            raise FileNotFoundError(
                f"pioNER data directory not found: {input_dir}. "
                f"Clone {PIONER_GITHUB_DATA_URL} and place pioner-gold/ and pioner-silver/ under that path."
            )

        conll_files = sorted(
            path
            for path in input_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in CONLL_SUFFIXES
        )
        if not conll_files:
            raise FileNotFoundError(
                f"No CoNLL files found under {input_dir}. "
                f"Expected files like pioner-gold/test.conll03 from {PIONER_GITHUB_DATA_URL}."
            )

        summaries: list[PionerImportSummary] = []
        for conll_path in conll_files:
            source_kind = self._source_kind_from_path(conll_path, root=input_dir)
            dataset_split = self._dataset_split_from_path(conll_path)
            examples = self._read_conll_file(conll_path)
            summaries.append(
                self._import_sequences(
                    session,
                    examples=examples,
                    source_kind=source_kind,
                    dataset_split=dataset_split,
                    replace=replace,
                    dataset_name=str(conll_path.relative_to(input_dir)),
                )
            )
        return summaries

    def import_huggingface_dataset(
        self,
        session: Session,
        *,
        dataset_name: str = PIONER_DATASET_NAME,
        replace: bool = True,
    ) -> list[PionerImportSummary]:
        try:
            from datasets import DatasetDict, load_dataset
        except ImportError as exc:
            raise RuntimeError(
                "The pioNER importer requires the optional Hugging Face dependency. "
                "Install it with: pip install -e '.[pioner]'"
            ) from exc

        try:
            dataset = load_dataset(dataset_name)
        except Exception as exc:
            exc_name = type(exc).__name__
            if exc_name == "DataFilesNotFoundError" or "No (supported) data files found" in str(exc):
                raise RuntimeError(
                    f"Hugging Face dataset {dataset_name!r} has no loadable data files (only metadata). "
                    f"Download CoNLL files from {PIONER_GITHUB_DATA_URL} and run the importer with --input."
                ) from exc
            raise
        summaries: list[PionerImportSummary] = []
        if isinstance(dataset, DatasetDict):
            split_items = dataset.items()
        else:
            split_items = [("unknown", dataset)]

        for split_name, split_dataset in split_items:
            source_kind = self._source_kind_from_split(str(split_name))
            label_names = self._label_names(split_dataset)
            examples = [
                self._tokens_and_labels_from_example(example, label_names=label_names)
                for example in split_dataset
            ]
            summaries.append(
                self._import_sequences(
                    session,
                    examples=examples,
                    source_kind=source_kind,
                    dataset_split=str(split_name),
                    replace=replace,
                    dataset_name=dataset_name,
                )
            )
        return summaries

    def _import_sequences(
        self,
        session: Session,
        *,
        examples: list[tuple[list[str], list[str]]],
        source_kind: str,
        dataset_split: str,
        replace: bool,
        dataset_name: str | None,
    ) -> PionerImportSummary:
        source_kind_normalized = self._normalize_source_kind(source_kind)
        dataset_split_normalized = dataset_split.strip().lower() or "unknown"
        source = self._get_or_create_source(
            session,
            source_kind=source_kind_normalized,
            dataset_split=dataset_split_normalized,
        )
        if replace:
            session.execute(delete(NerEntityEntry).where(NerEntityEntry.source_id == source.id))

        accumulators: dict[tuple[str, str], EntityAccumulator] = {}
        sentence_count = 0
        entity_count = 0
        skipped_entity_count = 0

        for tokens, labels in examples:
            sentence_count += 1
            context = " ".join(tokens)
            for surface, entity_type in self._extract_entities(tokens, labels):
                normalized = normalize_token(surface)
                if not normalized or entity_type not in SUPPORTED_ENTITY_TYPES:
                    skipped_entity_count += 1
                    continue
                entity_count += 1
                key = (normalized, entity_type)
                accumulator = accumulators.get(key)
                if accumulator is None:
                    accumulator = EntityAccumulator(
                        entity_surface=surface,
                        normalized_surface=normalized,
                        entity_type=entity_type,
                    )
                    accumulators[key] = accumulator
                accumulator.add(context=context)

        confidence = self._confidence_for_source_kind(source_kind_normalized)
        existing_by_key: dict[tuple[str, str], NerEntityEntry] = {}
        if not replace:
            existing_by_key = {
                (entry.normalized_surface, entry.entity_type): entry
                for entry in session.scalars(
                    select(NerEntityEntry).where(NerEntityEntry.source_id == source.id)
                )
            }
        for accumulator in accumulators.values():
            existing_entry = existing_by_key.get((accumulator.normalized_surface, accumulator.entity_type))
            if existing_entry is not None:
                existing_entry.occurrence_count += accumulator.occurrence_count
                existing_entry.sample_contexts = list(
                    dict.fromkeys([*existing_entry.sample_contexts, *accumulator.sample_contexts])
                )[:SAMPLE_CONTEXT_LIMIT]
                existing_entry.confidence = confidence
                existing_entry.metadata_json = {
                    **(existing_entry.metadata_json or {}),
                    "source_kind": source_kind_normalized,
                    "dataset_split": dataset_split_normalized,
                    "provider_key": PIONER_PROVIDER_KEY,
                }
            else:
                session.add(
                    NerEntityEntry(
                        source_id=source.id,
                        entity_surface=accumulator.entity_surface,
                        normalized_surface=accumulator.normalized_surface,
                        entity_type=accumulator.entity_type,
                        occurrence_count=accumulator.occurrence_count,
                        confidence=confidence,
                        sample_contexts=accumulator.sample_contexts,
                        metadata_json={
                            "source_kind": source_kind_normalized,
                            "dataset_split": dataset_split_normalized,
                            "provider_key": PIONER_PROVIDER_KEY,
                        },
                    )
                )

        source.metadata_json = {
            **(source.metadata_json or {}),
            "dataset_name": dataset_name or PIONER_DATASET_NAME,
            "sentence_count": sentence_count,
            "entity_count": entity_count,
            "entry_count": len(accumulators),
            "skipped_entity_count": skipped_entity_count,
        }
        session.commit()
        return PionerImportSummary(
            source_id=source.id,
            source_kind=source_kind_normalized,
            dataset_split=dataset_split_normalized,
            sentence_count=sentence_count,
            entity_count=entity_count,
            entry_count=len(accumulators),
            skipped_entity_count=skipped_entity_count,
        )

    @staticmethod
    def _label_names(dataset) -> list[str] | None:  # noqa: ANN001
        features = getattr(dataset, "features", {}) or {}
        for key in ("ner_tags", "tags", "labels"):
            feature = features.get(key) if hasattr(features, "get") else None
            if feature is None:
                continue
            inner_feature = getattr(feature, "feature", feature)
            names = getattr(inner_feature, "names", None)
            if isinstance(names, list):
                return [str(name) for name in names]
        return None

    @staticmethod
    def _tokens_and_labels_from_example(
        example: dict[str, object],
        *,
        label_names: list[str] | None,
    ) -> tuple[list[str], list[str]]:
        tokens_raw = (
            example.get("tokens")
            or example.get("words")
            or example.get("token")
            or example.get("sentence")
            or []
        )
        labels_raw = (
            example.get("ner_tags")
            or example.get("tags")
            or example.get("labels")
            or example.get("ner")
            or []
        )
        tokens = [str(token) for token in tokens_raw] if isinstance(tokens_raw, list) else str(tokens_raw).split()
        if isinstance(labels_raw, list):
            labels = [
                label_names[int(label)] if isinstance(label, int) and label_names and int(label) < len(label_names) else str(label)
                for label in labels_raw
            ]
        else:
            labels = str(labels_raw).split()
        return tokens, labels

    @staticmethod
    def _read_conll_file(path: Path) -> list[tuple[list[str], list[str]]]:
        sentences: list[tuple[list[str], list[str]]] = []
        tokens: list[str] = []
        labels: list[str] = []
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                if tokens:
                    sentences.append((tokens, labels))
                tokens = []
                labels = []
                continue
            if line.startswith("-DOCSTART-"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            token = parts[0]
            tag = parts[-1]
            tokens.append(token)
            labels.append(tag)
        if tokens:
            sentences.append((tokens, labels))
        return sentences

    @staticmethod
    def _dataset_split_from_path(path: Path) -> str:
        stem = path.stem.lower()
        for candidate in ("train", "dev", "development", "test", "val", "validation"):
            if candidate in stem:
                if candidate in {"dev", "development"}:
                    return "dev"
                if candidate == "validation":
                    return "validation"
                return candidate
        return stem or "unknown"

    @staticmethod
    def _source_kind_from_path(path: Path, *, root: Path) -> str:
        relative = str(path.relative_to(root)).lower()
        if "gold" in relative or "manual" in relative or "test" in path.stem.lower():
            return "gold"
        if "silver" in relative or "wiki" in relative:
            return "silver"
        return PionerImportService._source_kind_from_split(path.stem)

    @staticmethod
    def _source_kind_from_split(split_name: str) -> str:
        normalized = split_name.strip().lower()
        if any(marker in normalized for marker in ("gold", "manual", "test")):
            return "gold"
        return "silver"

    def _get_or_create_source(self, session: Session, *, source_kind: str, dataset_split: str) -> NerSource:
        source = session.scalar(
            select(NerSource).where(
                NerSource.provider_key == PIONER_PROVIDER_KEY,
                NerSource.source_kind == source_kind,
                NerSource.dataset_split == dataset_split,
            )
        )
        if source is not None:
            source.is_active = True
            return source
        source = NerSource(
            provider_key=PIONER_PROVIDER_KEY,
            display_name=f"pioNER {source_kind} {dataset_split}",
            source_kind=source_kind,
            dataset_split=dataset_split,
            source_url=PIONER_SOURCE_URL,
            license=PIONER_LICENSE,
            version="unknown",
            is_active=True,
            metadata_json={},
        )
        session.add(source)
        session.flush()
        return source

    @staticmethod
    def _extract_entities(tokens: list[str], labels: list[str]) -> list[tuple[str, str]]:
        entities: list[tuple[str, str]] = []
        current_tokens: list[str] = []
        current_type: str | None = None

        def flush() -> None:
            nonlocal current_tokens, current_type
            if current_tokens and current_type:
                entities.append((" ".join(current_tokens), current_type))
            current_tokens = []
            current_type = None

        for token, raw_label in zip(tokens, labels, strict=False):
            label = raw_label.strip()
            if label == "O" or not label:
                flush()
                continue
            if "-" in label:
                prefix, entity_type = label.split("-", maxsplit=1)
            else:
                prefix, entity_type = "B", label
            entity_type = entity_type.upper()
            if prefix == "B" or current_type != entity_type:
                flush()
                current_tokens = [token]
                current_type = entity_type
            elif prefix == "I" and current_type == entity_type:
                current_tokens.append(token)
            else:
                flush()
        flush()
        return entities

    @staticmethod
    def _normalize_source_kind(source_kind: str) -> str:
        normalized = source_kind.strip().lower()
        if normalized in {"manual", "gold", "gold_manual"}:
            return "gold"
        if normalized in {"wiki", "wikipedia", "silver", "silver_wikipedia"}:
            return "silver"
        raise ValueError("source_kind must be one of: gold, manual, silver, wikipedia")

    @staticmethod
    def _confidence_for_source_kind(source_kind: str) -> float:
        return 0.85 if source_kind == "gold" else 0.55


def summarize_entries(entries: list[NerEntityEntry]) -> dict[str, int]:
    return dict(Counter(entry.entity_type for entry in entries))
