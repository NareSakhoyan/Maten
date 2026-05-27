from __future__ import annotations

import argparse
from pathlib import Path

from app.core.config import get_settings
from app.core.database import session_scope
from app.core.resource_manifest import resolve_resource_local_path
from app.services.ner.pioner_import_service import (
    PIONER_DATASET_NAME,
    PIONER_GITHUB_DATA_URL,
    PIONER_RESOURCE_KEY,
    PionerImportService,
)


def _default_input_dir() -> Path | None:
    resolved = resolve_resource_local_path(
        PIONER_RESOURCE_KEY,
        configured_path=get_settings().resource_manifest_path,
    )
    return resolved


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Import pioNER CoNLL data as non-validating named-entity evidence. "
            f"Download files from {PIONER_GITHUB_DATA_URL} first."
        ),
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help="Directory containing pioner-gold/ and pioner-silver/ CoNLL files (default: manifest local_path).",
    )
    parser.add_argument(
        "--dataset",
        default=None,
        help="Optional Hugging Face dataset name. The public HF repo has no data files; prefer --input.",
    )
    parser.add_argument(
        "--append",
        action="store_true",
        help="Append to existing entries for this source/split instead of replacing them.",
    )
    args = parser.parse_args()

    service = PionerImportService()
    with session_scope() as session:
        if args.dataset:
            summaries = service.import_huggingface_dataset(
                session,
                dataset_name=args.dataset,
                replace=not args.append,
            )
        else:
            input_dir = args.input or _default_input_dir()
            if input_dir is None:
                raise SystemExit(
                    "No --input path and no manifest local_path for pioner_ner. "
                    f"Clone {PIONER_GITHUB_DATA_URL} into data/pioner and retry."
                )
            summaries = service.import_local_directory(
                session,
                input_dir=input_dir,
                replace=not args.append,
            )

    for summary in summaries:
        print(
            "Imported pioNER "
            f"source_id={summary.source_id} "
            f"kind={summary.source_kind} "
            f"split={summary.dataset_split} "
            f"sentences={summary.sentence_count} "
            f"entities={summary.entity_count} "
            f"entries={summary.entry_count} "
            f"skipped={summary.skipped_entity_count}"
        )


if __name__ == "__main__":
    main()
