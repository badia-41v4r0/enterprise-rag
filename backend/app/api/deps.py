"""Request dependencies: current user from the session cookie, CSRF, admin checks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.authz.policy import EffectivePermissions, effective_permissions
from app.core.config import get_settings
from app.core.db import get_db
from app.core.security import constant_time_equals, sha256_hex
from app.models import Session, User

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CSRF_HEADER = "X-CSRF-Token"


@dataclass
class AuthContext:
    user: User
    session: Session
    perms: EffectivePermissions


async def get_auth(request: Request, db: AsyncSession = Depends(get_db)) -> AuthContext:
    settings = get_settings()
    raw = request.cookies.get(settings.session_cookie_name)
    if not raw:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    sess = await db.get(Session, sha256_hex(raw))
    if sess is None or sess.expires_at <= datetime.now(UTC):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired")
    user = await db.scalar(select(User).where(User.id == sess.user_id))
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account disabled")
    if request.method not in SAFE_METHODS:
        sent = request.headers.get(CSRF_HEADER, "")
        if not sent or not constant_time_equals(sent, sess.csrf_token):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF token missing or invalid")
    perms = await effective_permissions(db, user)
    return AuthContext(user=user, session=sess, perms=perms)


async def require_admin(auth: AuthContext = Depends(get_auth)) -> AuthContext:
    if not auth.user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin only")
    return auth
