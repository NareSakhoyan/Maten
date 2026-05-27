from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import logging
from pathlib import Path

from app.core.config import Settings, get_settings
from app.core.resource_manifest import (
    ResourceManifest,
    ResourceManifestEntry,
    get_resource_manifest,
    resolve_resource_local_path,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ResourceDescriptor:
    key: str
    display_name: str
    type: str
    language_variant: str
    language_profile: str
    provider_key: str | None
    provider_type: str | None
    evidence_role: str | None
    enabled: bool
    priority: int
    can_validate_word: bool | str
    can_attest_usage: bool
    can_suggest_lemma: bool
    can_suggest_named_entity: bool
    requires_exact_match: bool
    requires_structured_headword: bool
    default_runtime: str
    independent_source_group: str | None
    source_kind: str | None
    local_path: Path | None
    available: bool
    version: str | None = None


class ResourceRegistry:
    """Process-local view of configured resources and model paths."""

    def __init__(self, *, settings: Settings | None = None, manifest: ResourceManifest | None = None) -> None:
        self.settings = settings or get_settings()
        self.manifest = manifest or get_resource_manifest(self.settings.resource_manifest_path)

    def enabled_resources(self) -> list[ResourceDescriptor]:
        return [
            self._descriptor(entry)
            for entry in self.manifest.enabled_entries()
        ]

    def resource(self, key: str) -> ResourceDescriptor | None:
        entry = self.manifest.by_key(key)
        if entry is None:
            return None
        return self._descriptor(entry)

    def resource_enabled(self, key: str, *, default: bool = True) -> bool:
        entry = self.manifest.by_key(key)
        if entry is None:
            return default
        return entry.enabled

    def local_path(self, key: str) -> Path | None:
        descriptor = self.resource(key)
        if descriptor is None or not descriptor.enabled:
            return None
        if descriptor.local_path is not None and not descriptor.available:
            logger.warning("Configured resource path is missing key=%s path=%s", key, descriptor.local_path)
            return None
        return descriptor.local_path

    def pie_model_path(self, profile: str | None) -> Path | None:
        normalized = (profile or self.settings.pie_default_profile or "").strip().lower()
        resource_key = "pie_classical_morphology" if normalized == "classical" else "pie_eastern_morphology"
        configured = (
            self.settings.pie_classical_model_path
            if normalized == "classical"
            else self.settings.pie_eastern_model_path
        )
        if configured:
            path = Path(configured)
            if not path.is_absolute():
                from app.core.config import BACKEND_ROOT

                path = BACKEND_ROOT / path
            if path.exists():
                return path
            logger.warning("Configured PIE model path is missing profile=%s path=%s", normalized, path)
            if profile:
                return None

        resource_path = self.local_path(resource_key)
        if resource_path is not None:
            return resource_path
        if profile:
            return None
        return self._fallback_pie_root()

    def _fallback_pie_root(self) -> Path | None:
        if not self.settings.pie_model_root:
            return None
        path = Path(self.settings.pie_model_root)
        if not path.is_absolute():
            from app.core.config import BACKEND_ROOT

            path = BACKEND_ROOT / path
        return path if path.exists() else None

    def _descriptor(self, entry: ResourceManifestEntry) -> ResourceDescriptor:
        path = resolve_resource_local_path(entry.key, configured_path=self.settings.resource_manifest_path)
        return ResourceDescriptor(
            key=entry.key,
            display_name=entry.display_name,
            type=entry.type,
            language_variant=entry.language_variant,
            language_profile=entry.language_profile or entry.language_variant,
            provider_key=entry.provider_key,
            provider_type=entry.provider_type,
            evidence_role=entry.evidence_role,
            enabled=entry.enabled,
            priority=entry.priority,
            can_validate_word=entry.can_validate_word,
            can_attest_usage=entry.can_attest_usage,
            can_suggest_lemma=entry.can_suggest_lemma,
            can_suggest_named_entity=entry.can_suggest_named_entity,
            requires_exact_match=entry.requires_exact_match,
            requires_structured_headword=entry.requires_structured_headword,
            default_runtime=entry.default_runtime,
            independent_source_group=entry.independent_source_group,
            source_kind=entry.source_kind,
            local_path=path,
            available=path.exists() if path is not None else True,
            version=entry.version,
        )


@lru_cache(maxsize=1)
def get_resource_registry() -> ResourceRegistry:
    return ResourceRegistry()
