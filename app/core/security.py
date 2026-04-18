from __future__ import annotations

from fastapi import HTTPException, status
from fastapi.security import HTTPBearer


bearer_scheme = HTTPBearer(auto_error=False)


def unauthorized(detail: str = "Invalid or missing bearer token.") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )

