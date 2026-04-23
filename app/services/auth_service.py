from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import hashlib
import logging
from threading import Lock
from time import monotonic
from typing import Any
from uuid import UUID

from httpx import Client as HTTPXClient
from httpx import Timeout
from supabase import Client, create_client
from supabase.lib.client_options import SyncClientOptions

from app.core.config import Settings, get_settings


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    user_id: UUID
    access_token: str
    email: str | None = None


logger = logging.getLogger(__name__)


def _value_from_object(value: Any, key: str) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


@lru_cache(maxsize=1)
def get_supabase_admin_client() -> Client:
    settings = get_settings()
    return create_client(settings.supabase_url, settings.supabase_service_role_key)


def _build_supabase_auth_client(settings: Settings) -> Client:
    timeout = Timeout(settings.supabase_auth_timeout_seconds)
    options = SyncClientOptions(
        auto_refresh_token=False,
        persist_session=False,
        httpx_client=HTTPXClient(timeout=timeout),
    )
    return create_client(settings.supabase_url, settings.supabase_service_role_key, options=options)


class AuthService:
    def __init__(self, settings: Settings | None = None, client: Client | None = None) -> None:
        self.settings = settings or get_settings()
        self.client = client or _build_supabase_auth_client(self.settings)
        self._cache_ttl_seconds = self.settings.supabase_auth_cache_ttl_seconds
        self._verification_cache: dict[str, tuple[float, UUID, str | None]] = {}
        self._cache_lock = Lock()

    def _cache_key(self, access_token: str) -> str:
        return hashlib.sha256(access_token.encode("utf-8")).hexdigest()

    def _get_cached_user(self, access_token: str) -> AuthenticatedUser | None:
        if self._cache_ttl_seconds <= 0:
            return None
        now = monotonic()
        cache_key = self._cache_key(access_token)
        with self._cache_lock:
            cached = self._verification_cache.get(cache_key)
            if cached is None:
                return None
            expires_at, user_id, email = cached
            if expires_at <= now:
                self._verification_cache.pop(cache_key, None)
                return None
        return AuthenticatedUser(user_id=user_id, access_token=access_token, email=email)

    def _store_cached_user(self, access_token: str, authenticated_user: AuthenticatedUser) -> None:
        if self._cache_ttl_seconds <= 0:
            return
        cache_key = self._cache_key(access_token)
        expires_at = monotonic() + self._cache_ttl_seconds
        with self._cache_lock:
            self._verification_cache[cache_key] = (
                expires_at,
                authenticated_user.user_id,
                authenticated_user.email,
            )

    def verify_access_token(self, access_token: str) -> AuthenticatedUser:
        cached_user = self._get_cached_user(access_token)
        if cached_user is not None:
            logger.debug("Supabase access token verification cache hit.")
            return cached_user

        started_at = monotonic()
        try:
            response = self.client.auth.get_user(access_token)
        except Exception as exc:  # pragma: no cover - depends on remote auth
            elapsed_ms = int((monotonic() - started_at) * 1000)
            logger.warning(
                "Supabase access token verification failed after %sms: error_type=%s error=%s",
                elapsed_ms,
                type(exc).__name__,
                str(exc),
            )
            raise ValueError("Token verification failed.") from exc

        user_payload = _value_from_object(response, "user") or _value_from_object(
            _value_from_object(response, "data"),
            "user",
        )
        user_id = _value_from_object(user_payload, "id")
        if not user_id:
            logger.warning("Supabase access token verification returned no user payload.")
            raise ValueError("Token verification failed.")

        email = _value_from_object(user_payload, "email")
        authenticated_user = AuthenticatedUser(
            user_id=UUID(str(user_id)),
            access_token=access_token,
            email=email,
        )
        self._store_cached_user(access_token, authenticated_user)
        elapsed_ms = int((monotonic() - started_at) * 1000)
        logger.info(
            "Supabase access token verified user_id=%s after %sms",
            authenticated_user.user_id,
            elapsed_ms,
        )
        return authenticated_user


@lru_cache(maxsize=1)
def get_auth_service() -> AuthService:
    return AuthService()
