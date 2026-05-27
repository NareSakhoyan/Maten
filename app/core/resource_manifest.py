from __future__ import annotations

from functools import lru_cache
import logging
import os
from pathlib import Path
import re

import yaml
from pydantic import BaseModel, Field

from app.core.config import BACKEND_ROOT


logger = logging.getLogger(__name__)
_DEFAULT_MANIFEST_PATH = BACKEND_ROOT / "resources" / "resource_manifest.example.yaml"
_NON_ENV_CHARS = re.compile(r"[^A-Z0-9]+")


class ResourceManifestEntry(BaseModel):
    key: str
    display_name: str
    type: str
    language_variant: str = "unknown"
    language_profile: str | None = None
    license: str | None = None
    source_url: str | None = None
    local_path: str | None = None
    version: str | None = None
    enabled: bool = True
    priority: int = 100
    provider_key: str | None = None
    provider_type: str | None = None
    evidence_role: str | None = None
    can_validate_word: bool | str = False
    can_attest_usage: bool = False
    can_suggest_lemma: bool = False
    can_suggest_named_entity: bool = False
    requires_exact_match: bool = False
    requires_structured_headword: bool = False
    default_runtime: str = "disabled"
    independent_source_group: str | None = None
    source_kind: str | None = None
    notes: str | None = None


class ResourceManifest(BaseModel):
    resources: list[ResourceManifestEntry] = Field(default_factory=list)

    def enabled_entries(self) -> list[ResourceManifestEntry]:
        return [entry for entry in self.resources if entry.enabled]

    def by_key(self, key: str) -> ResourceManifestEntry | None:
        for entry in self.resources:
            if entry.key == key:
                return entry
        return None

    def by_provider_key(self, provider_key: str) -> list[ResourceManifestEntry]:
        return [entry for entry in self.resources if entry.provider_key == provider_key]


def _resolve_manifest_path(configured_path: str | None = None) -> Path:
    candidate = (configured_path or os.getenv("RESOURCE_MANIFEST_PATH") or "").strip()
    if not candidate:
        return _DEFAULT_MANIFEST_PATH
    path = Path(os.path.expandvars(candidate)).expanduser()
    if not path.is_absolute():
        path = BACKEND_ROOT / path
    return path


def _resource_path_override_env_key(resource_key: str) -> str:
    normalized = _NON_ENV_CHARS.sub("_", resource_key.upper()).strip("_")
    return f"RESOURCE_PATH_{normalized}"


def _resource_path_override(resource_key: str) -> str | None:
    return os.getenv(_resource_path_override_env_key(resource_key))


def _read_manifest(path: Path) -> ResourceManifest:
    if not path.exists():
        logger.info("Resource manifest not found at %s; using empty manifest.", path)
        return ResourceManifest()
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        logger.exception("Failed to read resource manifest at %s; using empty manifest.", path)
        return ResourceManifest()
    if not isinstance(payload, dict):
        logger.warning("Resource manifest at %s is not an object; using empty manifest.", path)
        return ResourceManifest()
    try:
        return ResourceManifest.model_validate(payload)
    except Exception:
        logger.exception("Resource manifest at %s is invalid; using empty manifest.", path)
        return ResourceManifest()


@lru_cache(maxsize=1)
def get_resource_manifest(configured_path: str | None = None) -> ResourceManifest:
    return _read_manifest(_resolve_manifest_path(configured_path))


def is_resource_enabled(resource_key: str, *, configured_path: str | None = None, default: bool = True) -> bool:
    manifest = get_resource_manifest(configured_path)
    entry = manifest.by_key(resource_key)
    if entry is None:
        return default
    return entry.enabled


def resolve_resource_local_path(resource_key: str, *, configured_path: str | None = None) -> Path | None:
    manifest = get_resource_manifest(configured_path)
    entry = manifest.by_key(resource_key)
    configured_local_path = _resource_path_override(resource_key) or (entry.local_path if entry is not None else None)
    if not configured_local_path:
        return None
    path = Path(os.path.expandvars(configured_local_path)).expanduser()
    if path.is_absolute():
        return path
    return BACKEND_ROOT / path
