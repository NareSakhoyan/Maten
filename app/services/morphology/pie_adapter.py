from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings, get_settings
from app.services.morphology.pie_runner import PieRawPrediction
from app.utils.text_normalization import normalize_token


@dataclass(frozen=True, slots=True)
class AdaptedMorphologyPrediction:
    lemma: str | None
    lemma_normalized: str | None
    pos: str | None
    morph_features: dict[str, object] | None

    @property
    def is_usable(self) -> bool:
        return bool(self.lemma or self.pos or self.morph_features)


class PieAdapter:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def adapt_prediction(self, prediction: PieRawPrediction) -> AdaptedMorphologyPrediction:
        lemma = self._normalize_optional_text(prediction.lemma)
        pos = self._normalize_optional_text(prediction.pos)
        morph_features = self._parse_morph_features(prediction.morph_features)

        if morph_features is None:
            fallback_features = {
                key: value
                for key, value in prediction.columns.items()
                if key not in {"token", "form", "word", "surface", "lemma", "pos", "upos", "xpos", "feats", "morph"}
                and value not in {None, "", "_"}
            }
            if fallback_features:
                morph_features = self._parse_morph_features(fallback_features)

        return AdaptedMorphologyPrediction(
            lemma=lemma,
            lemma_normalized=normalize_token(lemma) if lemma else None,
            pos=pos,
            morph_features=morph_features,
        )

    @staticmethod
    def _normalize_optional_text(value: object) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text or text == "_":
            return None
        return text

    def _parse_morph_features(self, value: object) -> dict[str, object] | None:
        if value is None:
            return None
        if isinstance(value, dict):
            normalized: dict[str, object] = {}
            for key, raw_feature_value in value.items():
                feature_key = str(key).strip()
                if not feature_key:
                    continue
                normalized_value = self._normalize_feature_value(raw_feature_value)
                if normalized_value is None:
                    continue
                normalized[feature_key] = normalized_value
            return normalized or None

        text = str(value).strip()
        if not text or text == "_":
            return None

        features: dict[str, object] = {}
        for item in text.split("|"):
            feature = item.strip()
            if not feature:
                continue
            if "=" not in feature:
                features[feature] = True
                continue
            key, raw_feature_value = feature.split("=", maxsplit=1)
            normalized_value = self._normalize_feature_value(raw_feature_value)
            if normalized_value is not None:
                features[key.strip()] = normalized_value
        return features or None

    @staticmethod
    def _normalize_feature_value(value: object) -> str | list[str] | bool | None:
        if isinstance(value, bool):
            return value
        if value is None:
            return None
        if isinstance(value, (list, tuple, set)):
            normalized_values = [
                str(item).strip()
                for item in value
                if str(item).strip() and str(item).strip() != "_"
            ]
            if not normalized_values:
                return None
            if len(normalized_values) == 1:
                return normalized_values[0]
            return sorted(dict.fromkeys(normalized_values))

        text = str(value).strip()
        if not text or text == "_":
            return None
        if "," in text:
            values = [item.strip() for item in text.split(",") if item.strip()]
            if len(values) == 1:
                return values[0]
            return sorted(dict.fromkeys(values))
        return text


def get_pie_adapter() -> PieAdapter:
    return PieAdapter()
