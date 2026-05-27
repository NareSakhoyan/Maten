from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings, get_settings
from app.services.morphology.pie_adapter import PieAdapter, get_pie_adapter
from app.services.morphology.pie_runner import PieRunner, get_pie_runner
from app.utils.text_normalization import normalize_token


@dataclass(frozen=True, slots=True)
class PieMorphologyResult:
    input_form: str
    normalized_form: str
    lemma: str | None = None
    lemma_normalized: str | None = None
    pos: str | None = None
    features: dict[str, object] = field(default_factory=dict)
    model_key: str = "pie_eastern_morphology"
    language_variant: str = "eastern"
    confidence: float | None = None
    status: str = "unavailable"
    raw_payload: dict[str, Any] = field(default_factory=dict)


class PieService:
    def __init__(
        self,
        *,
        runner: PieRunner | None = None,
        adapter: PieAdapter | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.runner = runner or get_pie_runner()
        self.adapter = adapter or get_pie_adapter()

    def analyze_forms(self, forms: list[str], *, profile: str | None = None) -> list[PieMorphologyResult]:
        if not forms:
            return []
        language_variant = self._language_variant(profile)
        model_key = self._model_key(language_variant)
        if not self.settings.pie_enabled:
            return [
                self._status_result(form, status="unavailable", model_key=model_key, language_variant=language_variant)
                for form in forms
            ]
        try:
            sequences = [[form] for form in forms]
            raw_sequences = self.runner.analyze_sequences(sequences, profile=language_variant)
        except Exception as exc:
            return [
                self._status_result(
                    form,
                    status="failed",
                    model_key=model_key,
                    language_variant=language_variant,
                    raw_payload={"error": str(exc)},
                )
                for form in forms
            ]

        results: list[PieMorphologyResult] = []
        for form, raw_sequence in zip(forms, raw_sequences, strict=True):
            if not raw_sequence:
                results.append(
                    self._status_result(form, status="failed", model_key=model_key, language_variant=language_variant)
                )
                continue
            prediction = raw_sequence[0]
            adapted = self.adapter.adapt_prediction(prediction)
            status = "analyzed" if adapted.is_usable else "failed"
            results.append(
                PieMorphologyResult(
                    input_form=form,
                    normalized_form=normalize_token(form),
                    lemma=adapted.lemma,
                    lemma_normalized=adapted.lemma_normalized,
                    pos=adapted.pos,
                    features=adapted.morph_features or {},
                    model_key=model_key,
                    language_variant=language_variant,
                    confidence=None,
                    status=status,
                    raw_payload={"columns": prediction.columns},
                )
            )
        return results

    def analyze_token_sequences(
        self,
        sequences: list[list[str]],
        *,
        profile: str | None = None,
    ) -> list[list[PieMorphologyResult]]:
        language_variant = self._language_variant(profile)
        flat_forms = [form for sequence in sequences for form in sequence]
        flat_results = self.analyze_forms(flat_forms, profile=language_variant)
        grouped: list[list[PieMorphologyResult]] = []
        cursor = 0
        for sequence in sequences:
            next_cursor = cursor + len(sequence)
            grouped.append(flat_results[cursor:next_cursor])
            cursor = next_cursor
        return grouped

    def _status_result(
        self,
        form: str,
        *,
        status: str,
        model_key: str,
        language_variant: str,
        raw_payload: dict[str, Any] | None = None,
    ) -> PieMorphologyResult:
        return PieMorphologyResult(
            input_form=form,
            normalized_form=normalize_token(form),
            model_key=model_key,
            language_variant=language_variant,
            status=status,
            raw_payload=raw_payload or {},
        )

    def _language_variant(self, profile: str | None) -> str:
        normalized = (profile or self.settings.pie_default_profile or "eastern").strip().lower()
        return "classical" if normalized.startswith("class") or normalized == "xcl" else "eastern"

    @staticmethod
    def _model_key(language_variant: str) -> str:
        return f"pie_{language_variant}_morphology"


def get_pie_service() -> PieService:
    return PieService()
