from __future__ import annotations

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.core.database import get_db_session
from app.core.security import bearer_scheme, unauthorized
from app.services.auth_service import AuthenticatedUser, AuthService, get_auth_service


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    auth_service: AuthService = Depends(get_auth_service),
) -> AuthenticatedUser:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise unauthorized()

    try:
        return auth_service.verify_access_token(credentials.credentials)
    except ValueError as exc:
        raise unauthorized(str(exc)) from exc


DBSession = Session

