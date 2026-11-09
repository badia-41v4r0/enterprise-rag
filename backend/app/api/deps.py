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


STEP_UP_REQUIRED = "step_up_required"
PASSWORD_CHANGE_REQUIRED = "password_change_required"  # noqa: S105 - an error code


async def _session_and_user(request: Request, db: AsyncSession) -> tuple[Session, User]:
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
    return sess, user


async def get_auth_allow_password_change(request: Request, db: AsyncSession = Depends(get_db)) -> AuthContext:
    """A complete login, also when the user still has to replace a temporary password. Only for /me, /logout and
    /change-password."""
    sess, user = await _session_and_user(request, db)
    if sess.mfa_pending:
        # Password accepted but the second factor is missing: this session grants nothing.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "MFA verification required")
    perms = await effective_permissions(db, user)
    return AuthContext(user=user, session=sess, perms=perms)


async def get_auth(request: Request, db: AsyncSession = Depends(get_db)) -> AuthContext:
    auth = await get_auth_allow_password_change(request, db)
    if auth.user.must_change_password:
        # A temporary password set by an administrator: nothing works until the user chooses their own (ADR 0037).
        raise HTTPException(status.HTTP_403_FORBIDDEN, PASSWORD_CHANGE_REQUIRED)
    return auth


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


async def setup_gate(request: Request, db: AsyncSession = Depends(get_db)) -> None:
    """While the installation is in setup mode (ADR 0034) every API outside /health, /api/info, /api/auth and
    /api/setup answers 503 "setup required", except for an administrator's session (the one created in the
    wizard's step 2, which configures the rest through the normal admin screens)."""
    from app.services.setup import setup_required

    if not await setup_required(db):
        return
    try:
        sess, user = await _session_and_user(request, db)
    except HTTPException:
        sess = None
        user = None
    if sess is None or user is None or sess.mfa_pending or not user.is_admin or user.must_change_password:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "setup required")


async def require_step_up(auth: AuthContext = Depends(require_admin)) -> AuthContext:
    """Admin actions that create code-execution or data-access paths need a recent re-authentication
    (password, or a TOTP code when MFA is on) within ERAG_STEP_UP_SECONDS (ADR 0019)."""
    if not recently_authenticated(auth.session):
        raise HTTPException(status.HTTP_403_FORBIDDEN, STEP_UP_REQUIRED)
    return auth
