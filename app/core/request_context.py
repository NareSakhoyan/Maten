from __future__ import annotations

from contextvars import ContextVar


request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
request_path_var: ContextVar[str | None] = ContextVar("request_path", default=None)
request_method_var: ContextVar[str | None] = ContextVar("request_method", default=None)
current_user_id_var: ContextVar[str | None] = ContextVar("current_user_id", default=None)


def request_context() -> dict[str, str | None]:
    return {
        "request_id": request_id_var.get(),
        "method": request_method_var.get(),
        "path": request_path_var.get(),
        "user_id": current_user_id_var.get(),
    }
