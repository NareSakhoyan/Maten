from __future__ import annotations

from uuid import UUID

import pytest

from app.core.config import Settings
from app.services.auth_service import AuthService


class _FakeAuthClient:
    def __init__(self, response=None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls = 0

    def get_user(self, access_token: str):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.response


class _FakeClient:
    def __init__(self, auth_client: _FakeAuthClient) -> None:
        self.auth = auth_client


def _settings() -> Settings:
    return Settings(
        DATABASE_URL="sqlite+pysqlite://",
        SUPABASE_URL="https://example.supabase.co",
        SUPABASE_SERVICE_ROLE_KEY="service-role-key",
        SUPABASE_AUTH_TIMEOUT_SECONDS=1,
        SUPABASE_AUTH_CACHE_TTL_SECONDS=60,
    )


def test_verify_access_token_caches_successful_lookup() -> None:
    auth_client = _FakeAuthClient(
        response={"user": {"id": "123e4567-e89b-12d3-a456-426614174000", "email": "user@example.com"}}
    )
    service = AuthService(settings=_settings(), client=_FakeClient(auth_client))

    first = service.verify_access_token("token-1")
    second = service.verify_access_token("token-1")

    assert auth_client.calls == 1
    assert first.user_id == UUID("123e4567-e89b-12d3-a456-426614174000")
    assert second.user_id == first.user_id
    assert second.access_token == "token-1"
    assert second.email == "user@example.com"


def test_verify_access_token_raises_value_error_on_remote_failure() -> None:
    auth_client = _FakeAuthClient(error=RuntimeError("network timeout"))
    service = AuthService(settings=_settings(), client=_FakeClient(auth_client))

    with pytest.raises(ValueError, match="Token verification failed."):
        service.verify_access_token("token-1")

    assert auth_client.calls == 1
