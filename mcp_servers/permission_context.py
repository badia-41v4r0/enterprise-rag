"""Signed, short-lived permission context passed from the gateway to MCP servers (ADR 0010).

Shared by the gateway (mint) and the reference servers (verify). It depends only on PyJWT so a
server does not need the app installed. The signing key is derived per server:
HMAC-SHA256(master_secret, server_name), so a token minted for one server is useless at another.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
import uuid
from typing import Any

import jwt

META_KEY = "erag/permission_context"
ISSUER = "enterprise-rag-gateway"
ALGORITHM = "HS256"


class PermissionDenied(Exception):
    """Raised by servers when the permission context is missing, invalid or out of scope."""


def server_key(master_secret: str, server_name: str) -> bytes:
    return hmac.new(master_secret.encode(), f"mcp-server:{server_name}".encode(), hashlib.sha256).digest()


def mint(
    master_secret: str,
    *,
    server: str,
    tool: str,
    user_id: str,
    scopes: dict[str, Any],
    ttl_seconds: int = 60,
    now: float | None = None,
) -> str:
    issued = int(now if now is not None else time.time())
    claims = {
        "iss": ISSUER,
        "aud": server,
        "sub": user_id,
        "tool": tool,
        "scopes": scopes,
        "iat": issued,
        "exp": issued + ttl_seconds,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(claims, server_key(master_secret, server), algorithm=ALGORITHM)


def token_from_meta(meta: Any) -> str | None:
    value = (meta or {}).get(META_KEY) if hasattr(meta or {}, "get") else None
    return value if isinstance(value, str) else None


def verify(token: str | None, *, server: str, tool: str, master_secret: str | None = None) -> dict[str, Any]:
    if not token:
        raise PermissionDenied("missing permission context")
    secret = master_secret or os.environ.get("ERAG_MCP_CONTEXT_SECRET", "")
    if not secret:
        raise PermissionDenied("server has no context secret configured")
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            server_key(secret, server),
            algorithms=[ALGORITHM],
            audience=server,
            issuer=ISSUER,
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise PermissionDenied("permission context expired") from exc
    except jwt.InvalidTokenError as exc:
        raise PermissionDenied(f"invalid permission context: {type(exc).__name__}") from exc
    if claims.get("tool") != tool:
        raise PermissionDenied("permission context was issued for another tool")
    return claims
