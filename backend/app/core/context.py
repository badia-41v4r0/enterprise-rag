"""The correlation id of the current request (R19): in every log line, on the audit entry and on the trace."""

from __future__ import annotations

import re
import uuid
from contextvars import ContextVar

request_id_var: ContextVar[str | None] = ContextVar("erag_request_id", default=None)
_VALID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def new_request_id(incoming: str | None = None) -> str:
    """Keep a caller's X-Request-ID when it is a plain token (a proxy's id), else make one. Never trusted for
    anything but correlation, and never longer than 64 characters (it ends up in logs)."""
    if incoming and _VALID.match(incoming):
        return incoming
    return uuid.uuid4().hex[:16]


def current_request_id() -> str | None:
    return request_id_var.get()
