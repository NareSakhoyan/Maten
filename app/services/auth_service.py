from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from uuid import UUID

from supabase import Client, create_client

from app.core.config import Settings, get_settings


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    user_id: UUID
    access_token: str
    email: str | None = None


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


class AuthService:
    def __init__(self, settings: Settings | None = None, client: Client | None = None) -> None:
        self.settings = settings or get_settings()
        self.client = client or get_supabase_admin_client()

    def verify_access_token(self, access_token: str) -> AuthenticatedUser:
        try:
            response = self.client.auth.get_user(access_token)
        except Exception as exc:  # pragma: no cover - depends on remote auth
            raise ValueError("Token verification failed.") from exc

        user_payload = _value_from_object(response, "user") or _value_from_object(
            _value_from_object(response, "data"),
            "user",
        )
        user_id = _value_from_object(user_payload, "id")
        if not user_id:
            raise ValueError("Token verification failed.")

        email = _value_from_object(user_payload, "email")
        return AuthenticatedUser(
            user_id=UUID(str(user_id)),
            access_token=access_token,
            email=email,
        )


@lru_cache(maxsize=1)
def get_auth_service() -> AuthService:
    return AuthService()

