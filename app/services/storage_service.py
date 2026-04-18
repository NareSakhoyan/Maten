from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable
import unicodedata

import regex

from app.core.config import Settings, get_settings
from app.services.auth_service import get_supabase_admin_client


def sanitize_storage_filename(filename: str) -> str:
    basename = Path(filename).name.strip() or "upload.bin"
    path = Path(basename)

    stem = unicodedata.normalize("NFKD", path.stem).encode("ascii", "ignore").decode("ascii")
    stem = regex.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")

    suffix = unicodedata.normalize("NFKD", path.suffix).encode("ascii", "ignore").decode("ascii")
    suffix = regex.sub(r"[^A-Za-z0-9.]+", "", suffix)
    if suffix and not suffix.startswith("."):
        suffix = f".{suffix}"

    if not stem:
        stem = "upload"
    if not suffix:
        suffix = ".bin"

    return f"{stem}{suffix}"


class StorageService:
    def __init__(self, settings: Settings | None = None, client: Any | None = None) -> None:
        self.settings = settings or get_settings()
        self.client = client or get_supabase_admin_client()

    def upload_bytes(
        self,
        bucket: str,
        path: str,
        data: bytes,
        content_type: str,
        *,
        upsert: bool = False,
    ) -> None:
        bucket_client = self.client.storage.from_(bucket)
        options = {
            "content-type": content_type,
            "upsert": "true" if upsert else "false",
        }
        self._try_calls(
            [
                lambda: bucket_client.upload(path, data, options),
                lambda: bucket_client.upload(path=path, file=data, file_options=options),
                lambda: bucket_client.upload(path=path, file=data, options=options),
            ],
            f"upload {bucket}/{path}",
        )

    def upload_json(self, bucket: str, path: str, payload: dict[str, Any], *, upsert: bool = True) -> None:
        self.upload_bytes(
            bucket=bucket,
            path=path,
            data=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
            content_type="application/json",
            upsert=upsert,
        )

    def download_bytes(self, bucket: str, path: str) -> bytes:
        bucket_client = self.client.storage.from_(bucket)
        result = self._try_calls(
            [
                lambda: bucket_client.download(path),
                lambda: bucket_client.download(path=path),
            ],
            f"download {bucket}/{path}",
        )

        data = self._extract_data(result)
        if isinstance(data, bytes):
            return data
        if isinstance(result, bytes):
            return result
        if isinstance(data, str):
            return data.encode("utf-8")
        raise RuntimeError(f"Unexpected download response for {bucket}/{path}.")

    def list_buckets(self) -> list[Any]:
        result = self.client.storage.list_buckets()
        if isinstance(result, list):
            return result
        data = self._extract_data(result)
        if isinstance(data, list):
            return data
        return []

    def create_bucket_if_missing(self, bucket_name: str, *, public: bool = False) -> bool:
        bucket_names = {self._bucket_name(bucket) for bucket in self.list_buckets()}
        if bucket_name in bucket_names:
            self.ensure_bucket_private(bucket_name)
            return False

        options = {"public": public}
        self._try_calls(
            [
                lambda: self.client.storage.create_bucket(
                    id=bucket_name,
                    name=bucket_name,
                    options=options,
                ),
                lambda: self.client.storage.create_bucket(
                    bucket_name,
                    name=bucket_name,
                    options=options,
                ),
                lambda: self.client.storage.create_bucket(bucket_name, options=options),
                lambda: self.client.storage.create_bucket(bucket_name),
            ],
            f"create bucket {bucket_name}",
        )
        self.ensure_bucket_private(bucket_name)
        return True

    def ensure_bucket_private(self, bucket_name: str) -> None:
        options = {"public": False}
        try:
            self._try_calls(
                [
                    lambda: self.client.storage.update_bucket(bucket_name, options),
                    lambda: self.client.storage.update_bucket(id=bucket_name, options=options),
                ],
                f"update bucket {bucket_name}",
            )
        except Exception:  # pragma: no cover - depends on remote API behavior
            return

    def _try_calls(self, calls: list[Callable[[], Any]], action: str) -> Any:
        last_type_error: TypeError | None = None
        for call in calls:
            try:
                return call()
            except TypeError as exc:
                last_type_error = exc
                continue

        if last_type_error is not None:
            raise RuntimeError(f"Supabase client signature mismatch during {action}.") from last_type_error
        raise RuntimeError(f"No callable variants available for {action}.")

    @staticmethod
    def _extract_data(value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, dict):
            return value.get("data", value)
        return getattr(value, "data", None)

    @staticmethod
    def _bucket_name(bucket: Any) -> str:
        if isinstance(bucket, dict):
            return str(bucket.get("name") or bucket.get("id") or "")
        return str(getattr(bucket, "name", None) or getattr(bucket, "id", ""))


@lru_cache(maxsize=1)
def get_storage_service() -> StorageService:
    return StorageService()
