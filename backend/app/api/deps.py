"""Request dependencies: current user from the session cookie, CSRF, admin checks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.authz.policy import EffectivePermissions, effective_permissions
from app.core.config import get_settings
from app.core.db import get_db, get_side_sessionmaker
from app.core.security import constant_time_equals, sha256_hex
from app.models import Session, User

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CSRF_HEADER = "X-CSRF-Token"
# `sessions.last_seen_at` is written at most this often per session: idle detection needs minutes, not requests.
TOUCH_EVERY = timedelta(minutes=1)


async def _touch(session_id: str, now: datetime) -> None:
    """Record activity in its own short transaction, so read-only requests (which never commit) count too and
    the request's own transaction is not affected. Best effort: if another transaction holds the row (a login
    step, a step-up), skip instead of waiting, which could wait on this very request."""
    locked = select(Session.id).where(Session.id == session_id).with_for_update(skip_locked=True).scalar_subquery()
    async with get_side_sessionmaker()() as s:
        await s.execute(update(Session).where(Session.id == locked).values(last_seen_at=now))
        await s.commit()


@dataclass
class AuthContext:
    user: User
    session: Session
    perms: EffectivePermissions


STEP_UP_REQUIRED = "step_up_required"


async def _session_and_user(request: Request, db: AsyncSession) -> tuple[Session, User]:
    settings = get_settings()
    raw = request.cookies.get(settings.session_cookie_name)
    if not raw:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    sess = await db.get(Session, sha256_hex(raw))
    now = datetime.now(UTC)
    if sess is None or sess.expires_at <= now:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired")
    if settings.session_idle_minutes and not sess.mfa_pending:
        seen = sess.last_seen_at or sess.created_at
        if now - seen > timedelta(minutes=settings.session_idle_minutes):
            await db.execute(delete(Session).where(Session.id == sess.id))
            await db.commit()
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired")
    if sess.last_seen_at is None or now - sess.last_seen_at >= TOUCH_EVERY:
        await _touch(sess.id, now)
    user = await db.scalar(select(User).where(User.id == sess.user_id))
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account disabled")
    if request.method not in SAFE_METHODS:
        sent = request.headers.get(CSRF_HEADER, "")
        if not sent or not constant_time_equals(sent, sess.csrf_token):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF token missing or invalid")
    return sess, user


async def get_auth(request: Request, db: AsyncSession = Depends(get_db)) -> AuthContext:
    sess, user = await _session_and_user(request, db)
    if sess.mfa_pending:
        # Password accepted but the second factor is missing: this session grants nothing.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "MFA verification required")
    perms = await effective_permissions(db, user)
    return AuthContext(user=user, session=sess, perms=perms)


async def get_pending_mfa(request: Request, db: AsyncSession = Depends(get_db)) -> tuple[Session, User]:
    """Only for POST /api/auth/mfa/verify: a session that passed the password step."""
    sess, user = await _session_and_user(request, db)
    if not sess.mfa_pending:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No MFA verification pending")
    return sess, user


def recently_authenticated(sess: Session) -> bool:
    if sess.reauth_at is None:
        return False
    return (datetime.now(UTC) - sess.reauth_at).total_seconds() <= get_settings().step_up_seconds


async def require_admin(auth: AuthContext = Depends(get_auth)) -> AuthContext:
    if not auth.user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin only")
    return auth


async def require_step_up(auth: AuthContext = Depends(require_admin)) -> AuthContext:
    """Admin actions that create code-execution or data-access paths need a recent re-authentication
    (password, or a TOTP code when MFA is on) within ERAG_STEP_UP_SECONDS (ADR 0019)."""
    if not recently_authenticated(auth.session):
        raise HTTPException(status.HTTP_403_FORBIDDEN, STEP_UP_REQUIRED)
    return auth
