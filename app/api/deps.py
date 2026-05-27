from __future__ import annotations

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.core.database import get_db_session
from app.core.request_context import current_user_id_var
from app.core.security import bearer_scheme, forbidden, unauthorized
from app.services.auth_service import AuthenticatedUser, AuthService, get_auth_service


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    auth_service: AuthService = Depends(get_auth_service),
) -> AuthenticatedUser:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise unauthorized()

    try:
        user = auth_service.verify_access_token(credentials.credentials)
        current_user_id_var.set(str(user.user_id))
        return user
    except ValueError as exc:
        raise unauthorized(str(exc)) from exc


def require_admin_user(
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> AuthenticatedUser:
    if current_user.role != "admin":
        raise forbidden("Admin access is required for this action.")
    return current_user


DBSession = Session

