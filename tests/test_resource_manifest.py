from __future__ import annotations

from pathlib import Path

from app.core.config import BACKEND_ROOT
from app.core.resource_manifest import get_resource_manifest, resolve_resource_local_path


def _write_manifest(path: Path, *, local_path: str) -> None:
    path.write_text(
        "\n".join(
            [
                "resources:",
                "  - key: nayiri_western_corpus",
                '    display_name: "Nayiri Western Armenian Corpus"',
                "    type: corpus",
                f"    local_path: {local_path}",
            ]
        ),
        encoding="utf-8",
    )


def test_resource_path_env_override_wins_over_manifest(monkeypatch, tmp_path):
    manifest_path = tmp_path / "resources.yaml"
    manifest_path_value = tmp_path / "manifest-path"
    override_path = tmp_path / "container-path"
    _write_manifest(manifest_path, local_path=str(manifest_path_value))

    get_resource_manifest.cache_clear()
    monkeypatch.setenv("RESOURCE_PATH_NAYIRI_WESTERN_CORPUS", str(override_path))

    try:
        assert resolve_resource_local_path("nayiri_western_corpus", configured_path=str(manifest_path)) == override_path
    finally:
        get_resource_manifest.cache_clear()


def test_relative_manifest_resource_path_resolves_from_backend_root(monkeypatch, tmp_path):
    manifest_path = tmp_path / "resources.yaml"
    _write_manifest(manifest_path, local_path="../nayiri-corpus")

    get_resource_manifest.cache_clear()
    monkeypatch.delenv("RESOURCE_PATH_NAYIRI_WESTERN_CORPUS", raising=False)

    try:
        assert resolve_resource_local_path("nayiri_western_corpus", configured_path=str(manifest_path)) == (
            BACKEND_ROOT / "../nayiri-corpus"
        )
    finally:
        get_resource_manifest.cache_clear()
