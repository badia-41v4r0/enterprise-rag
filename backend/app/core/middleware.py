"""Security headers and structured request logging."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.context import new_request_id, request_id_var
from app.core.metrics import HTTP_LATENCY, HTTP_REQUESTS

log = logging.getLogger("app.request")

SECURITY_HEADERS = {
    # The SPA is self-contained: no third-party scripts, styles, fonts or frames.
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        # Correlation id (R19): in every log line of this request, on its audit entries and its trace.
        request_id = new_request_id(request.headers.get("X-Request-ID"))
        token = request_id_var.set(request_id)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        response.headers["X-Request-ID"] = request_id
        elapsed = time.perf_counter() - start
        # The route template, never the concrete path (ids would explode the label set).
        route = getattr(request.scope.get("route"), "path", None) or "unmatched"
        HTTP_REQUESTS.inc(route=route, status=str(response.status_code))
        HTTP_LATENCY.observe(elapsed, route=route)
        log.info(
            "request",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "ms": round(elapsed * 1000, 1),
            },
        )
        return response
