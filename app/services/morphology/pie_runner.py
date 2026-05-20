from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import shlex
import shutil
import subprocess

from app.core.config import Settings, get_settings


class PieRuntimeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PieRawPrediction:
    token_surface: str
    lemma: str | None = None
    pos: str | None = None
    morph_features: str | dict[str, object] | None = None
    columns: dict[str, str | None] = field(default_factory=dict)


class PieRunner:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def analyze_sequences(self, sequences: list[list[str]]) -> list[list[PieRawPrediction]]:
        if not self.settings.pie_enabled:
            raise PieRuntimeError("PIE morphology analysis is disabled.")
        if not sequences:
            return []

        pie_binary = self._resolve_pie_binary()
        if pie_binary is None:
            configured = self.settings.pie_executable
            raise PieRuntimeError(
                "The PIE executable could not be found. "
                f"Configured PIE_EXECUTABLE={configured!r}. "
                "Install the PIE CLI in the worker environment or point PIE_EXECUTABLE to the full binary path."
            )

        model_spec = self._resolve_model_spec()
        payload = "\n".join(" ".join(sequence) for sequence in sequences) + "\n"
        completed = subprocess.run(
            [pie_binary, "tag-pipe", *shlex.split(model_spec)],
            input=payload,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.strip() or completed.stdout.strip() or "Unknown PIE failure."
            raise PieRuntimeError(stderr)
        return self._parse_output(completed.stdout, expected_sequences=sequences)

    def resolve_analyzer_version(self) -> str | None:
        model_root = self.settings.pie_model_root
        if not model_root:
            return None
        root = Path(model_root)
        version_file = root / "version.txt" if root.is_dir() else root.parent / "version.txt"
        if version_file.exists():
            version = version_file.read_text(encoding="utf-8").strip()
            return version or None
        return None

    def _resolve_pie_binary(self) -> str | None:
        configured = self.settings.pie_executable.strip()
        if not configured:
            return None

        configured_path = Path(configured).expanduser()
        if configured_path.is_file():
            return str(configured_path)

        return shutil.which(configured)

    def _resolve_model_spec(self) -> str:
        model_root = self.settings.pie_model_root
        if not model_root:
            raise PieRuntimeError("PIE_MODEL_ROOT is not configured.")

        root = Path(model_root)
        if root.is_file():
            return str(root)

        if not root.exists():
            raise PieRuntimeError(f"Configured PIE model root does not exist: {root}")

        model_spec_file = root / "model_spec.txt"
        if model_spec_file.exists():
            model_spec = model_spec_file.read_text(encoding="utf-8").strip()
            if model_spec:
                return model_spec

        model_key = self.settings.pie_model_key.strip()
        prioritized_patterns = [
            f"{model_key}.tar",
            f"{model_key}.tar.gz",
            f"{model_key}.tgz",
            f"{model_key}.pt",
            f"{model_key}.model",
            f"{model_key}*.tar",
            f"{model_key}*.tar.gz",
            f"{model_key}*.tgz",
            f"{model_key}*.pt",
            f"{model_key}*.model",
        ]
        candidates: list[Path] = []
        for pattern in prioritized_patterns:
            candidates.extend(sorted(root.glob(pattern)))
        if candidates:
            return str(candidates[0])

        generic_candidates: list[Path] = []
        for pattern in ("*.tar", "*.tar.gz", "*.tgz", "*.pt", "*.model"):
            generic_candidates.extend(sorted(root.glob(pattern)))
        if len(generic_candidates) == 1:
            return str(generic_candidates[0])

        # Fall back to the directory path so deployments can point PIE directly at an unpacked model directory.
        return str(root)

    def _parse_output(
        self,
        output: str,
        *,
        expected_sequences: list[list[str]],
    ) -> list[list[PieRawPrediction]]:
        sequences: list[list[PieRawPrediction]] = []
        current_sequence: list[PieRawPrediction] = []
        header: list[str] | None = None

        for raw_line in output.splitlines():
            if not raw_line.strip():
                if current_sequence:
                    sequences.append(current_sequence)
                    current_sequence = []
                continue
            if raw_line.lstrip().startswith("#"):
                continue

            parts = [part.strip() or None for part in raw_line.split("\t")]
            if header is None and self._looks_like_header(parts):
                header = [part.lower() if part is not None else "" for part in parts]
                continue
            current_sequence.append(self._build_prediction(parts, header=header))

        if current_sequence:
            sequences.append(current_sequence)

        if len(sequences) == 1 and len(expected_sequences) > 1:
            flattened = sequences[0]
            expected_total = sum(len(sequence) for sequence in expected_sequences)
            if len(flattened) == expected_total:
                split_sequences: list[list[PieRawPrediction]] = []
                cursor = 0
                for expected_sequence in expected_sequences:
                    next_cursor = cursor + len(expected_sequence)
                    split_sequences.append(flattened[cursor:next_cursor])
                    cursor = next_cursor
                sequences = split_sequences

        if len(sequences) != len(expected_sequences):
            raise PieRuntimeError(
                f"PIE output sequence count mismatch: expected {len(expected_sequences)}, received {len(sequences)}."
            )

        for index, (predictions, expected_sequence) in enumerate(zip(sequences, expected_sequences, strict=True)):
            if len(predictions) != len(expected_sequence):
                raise PieRuntimeError(
                    "PIE output token count mismatch for sequence "
                    f"{index}: expected {len(expected_sequence)}, received {len(predictions)}."
                )
        return sequences

    @staticmethod
    def _looks_like_header(parts: list[str | None]) -> bool:
        lowered = {part.lower() for part in parts if part}
        if not lowered:
            return False
        if {"lemma", "pos", "upos", "feats", "morph", "morph_features"} & lowered:
            return True
        return any(part in {"token", "form", "word", "surface"} for part in lowered)

    @staticmethod
    def _build_prediction(parts: list[str | None], *, header: list[str] | None) -> PieRawPrediction:
        if header is None:
            token_surface = parts[0] or ""
            columns = {
                "token": parts[0],
                "lemma": parts[1] if len(parts) > 1 else None,
                "pos": parts[2] if len(parts) > 2 else None,
                "morph_features": parts[3] if len(parts) > 3 else None,
            }
            for index, value in enumerate(parts[4:], start=4):
                columns[f"field_{index}"] = value
            return PieRawPrediction(
                token_surface=token_surface,
                lemma=columns.get("lemma"),
                pos=columns.get("pos"),
                morph_features=columns.get("morph_features"),
                columns=columns,
            )

        columns = {
            (header[index] or f"field_{index}"): value
            for index, value in enumerate(parts)
        }
        token_surface = (
            columns.get("token")
            or columns.get("form")
            or columns.get("word")
            or columns.get("surface")
            or ""
        )
        return PieRawPrediction(
            token_surface=token_surface,
            lemma=columns.get("lemma"),
            pos=columns.get("pos") or columns.get("upos") or columns.get("xpos"),
            morph_features=columns.get("feats") or columns.get("morph") or columns.get("morph_features"),
            columns=columns,
        )


def get_pie_runner() -> PieRunner:
    return PieRunner()
