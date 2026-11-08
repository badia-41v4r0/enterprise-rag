"""Login (with optional TOTP second step), logout, profile MFA management and step-up re-authentication."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, get_auth, get_pending_mfa, recently_authenticated
from app.authz.policy import EffectivePermissions, content_containers, effective_permissions
from app.core.config import get_settings
from app.core.db import get_db
from app.core.ratelimit import RateLimiter
from app.core.security import new_token, sha256_hex, verify_password
from app.models import Session, User
from app.services import mfa
from app.services.audit import audit

router = APIRouter(prefix="/api/auth", tags=["auth"])
login_limiter = RateLimiter(get_settings().login_rate_limit_per_minute)
# Second-factor attempts (verify, step-up) per user, on top of the per-session attempt counter.
mfa_limiter = RateLimiter(get_settings().login_rate_limit_per_minute)


class LoginIn(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=1024)


class MeOut(BaseModel):
    id: str
    email: str
    display_name: str
    is_admin: bool
    roles: list[str]
    csrf_token: str
    # True when the user may upload to at least one container (contribute or manage grant).
    can_manage_content: bool = False
    mfa_enabled: bool = False
    # Login: password accepted, a TOTP or recovery code is still required (POST /mfa/verify).
    mfa_required: bool = False
    step_up_valid: bool = False


class CodeIn(BaseModel):
    code: str | None = Field(default=None, max_length=32)
    recovery_code: str | None = Field(default=None, max_length=32)


class StepUpIn(BaseModel):
    password: str | None = Field(default=None, max_length=1024)
    code: str | None = Field(default=None, max_length=32)
    recovery_code: str | None = Field(default=None, max_length=32)


class MfaSetupOut(BaseModel):
    secret: str
    otpauth_uri: str
    qr_svg: str


class MfaStatus(BaseModel):
    enabled: bool
    recovery_codes_left: int


class RecoveryCodesOut(BaseModel):
    recovery_codes: list[str]


def _me(user: User, sess: Session, perms: EffectivePermissions | None = None) -> MeOut:
    return MeOut(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        is_admin=user.is_admin,
        roles=sorted(r.name for r in user.roles),
        csrf_token=sess.csrf_token,
        can_manage_content=perms is not None and bool(content_containers(perms)),
        mfa_enabled=user.mfa_enabled,
        mfa_required=sess.mfa_pending,
        step_up_valid=recently_authenticated(sess) and not sess.mfa_pending,
    )


def _set_cookie(response: Response, token: str) -> None:
    s = get_settings()
    response.set_cookie(
        s.session_cookie_name,
        token,
        max_age=s.session_ttl_hours * 3600,
        httponly=True,
        secure=s.cookie_secure,
        samesite="strict",
        path="/",
    )


def _new_session(db: AsyncSession, user: User, *, pending: bool) -> tuple[str, Session]:
    s = get_settings()
    token = new_token()
    now = datetime.now(UTC)
    ttl = timedelta(minutes=5) if pending else timedelta(hours=s.session_ttl_hours)
    sess = Session(
        id=sha256_hex(token),
        user_id=user.id,
        csrf_token=new_token(),
        expires_at=now + ttl,
        mfa_pending=pending,
        mfa_attempts=0,
        reauth_at=None if pending else now,
    )
    db.add(sess)
    return token, sess


@router.post("/login")
async def login(body: LoginIn, request: Request, response: Response, db: AsyncSession = Depends(get_db)) -> MeOut:
    email = body.email.strip().lower()
    client_ip = request.client.host if request.client else "?"
    if not login_limiter.allow(f"{client_ip}:{email}"):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many login attempts")
    user = await db.scalar(select(User).where(User.email == email))
    ok = verify_password(user.password_hash if user else None, body.password)
    if user is None or not ok or not user.is_active:
        audit(db, "auth.login", user_id=user.id if user else None, resource=f"email:{email}", decision="deny")
        await db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")

    now = datetime.now(UTC)
    await db.execute(delete(Session).where(Session.user_id == user.id, Session.expires_at <= now))
    token, sess = _new_session(db, user, pending=user.mfa_enabled)
    audit(
        db,
        "auth.login",
        user_id=user.id,
        resource=f"user:{user.id}",
        decision="mfa_required" if user.mfa_enabled else "allow",
    )
    await db.commit()
    _set_cookie(response, token)
    if user.mfa_enabled:
        return _me(user, sess)
    return _me(user, sess, await effective_permissions(db, user))


async def _check_second_factor(db: AsyncSession, user: User, body: CodeIn | StepUpIn) -> bool:
    if body.code and mfa.verify_totp(user, body.code):
        return True
    return bool(body.recovery_code) and await mfa.use_recovery_code(db, user.id, str(body.recovery_code))


@router.post("/mfa/verify")
async def mfa_verify(
    body: CodeIn,
    response: Response,
    pending: tuple[Session, User] = Depends(get_pending_mfa),
    db: AsyncSession = Depends(get_db),
) -> MeOut:
    """Second login step. On success the pending session is replaced by a fresh one (no fixation)."""
    sess, user = pending
    if not mfa_limiter.allow(str(user.id)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts")
    sess.mfa_attempts += 1
    if sess.mfa_attempts > get_settings().mfa_max_attempts:
        await db.delete(sess)
        audit(db, "auth.mfa", user_id=user.id, decision="deny", details={"reason": "too many attempts"})
        await db.commit()
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts; log in again")
    used_recovery = bool(body.recovery_code) and not body.code
    if not await _check_second_factor(db, user, body):
        audit(db, "auth.mfa", user_id=user.id, decision="deny")
        await db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid code")
    await db.delete(sess)
    token, new = _new_session(db, user, pending=False)
    audit(db, "auth.mfa", user_id=user.id, details={"recovery_code": used_recovery})
    await db.commit()
    _set_cookie(response, token)
    return _me(user, new, await effective_permissions(db, user))


@router.post("/logout", status_code=204)
async def logout(response: Response, auth: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db)) -> None:
    await db.execute(delete(Session).where(Session.id == auth.session.id))
    audit(db, "auth.logout", user_id=auth.user.id)
    await db.commit()
    response.delete_cookie(get_settings().session_cookie_name, path="/")


@router.get("/me")
async def me(auth: AuthContext = Depends(get_auth)) -> MeOut:
    return _me(auth.user, auth.session, auth.perms)


@router.post("/step-up")
async def step_up(body: StepUpIn, auth: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db)) -> MeOut:
    """Re-authenticate for sensitive actions: a TOTP (or recovery) code when MFA is on, else the password."""
    user = auth.user
    if not mfa_limiter.allow(str(user.id)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts")
    if user.mfa_enabled:
        ok = await _check_second_factor(db, user, body)
    else:
        ok = bool(body.password) and verify_password(user.password_hash, str(body.password))
    if not ok:
        audit(db, "auth.step_up", user_id=user.id, decision="deny")
        await db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Re-authentication failed")
    auth.session.reauth_at = datetime.now(UTC)
    audit(db, "auth.step_up", user_id=user.id)
    await db.commit()
    return _me(user, auth.session, auth.perms)


# --- MFA enrolment (profile) ---------------------------------------------------------------------


@router.get("/mfa")
async def mfa_status(auth: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db)) -> MfaStatus:
    return MfaStatus(
        enabled=auth.user.mfa_enabled, recovery_codes_left=await mfa.remaining_recovery_codes(db, auth.user.id)
    )


@router.post("/mfa/setup")
async def mfa_setup(auth: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db)) -> MfaSetupOut:
    """Start enrolment: a new secret, stored encrypted but inactive until a code confirms it."""
    user = auth.user
    if user.mfa_enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "MFA is already enabled; disable it first")
    secret = mfa.new_secret()
    mfa.store_secret(user, secret)
    user.mfa_last_step = 0
    audit(db, "auth.mfa.setup", user_id=user.id)
    await db.commit()
    uri = mfa.provisioning_uri(user, secret)
    return MfaSetupOut(secret=secret, otpauth_uri=uri, qr_svg=mfa.qr_svg(uri))


@router.post("/mfa/enable")
async def mfa_enable(
    body: CodeIn, auth: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db)
) -> RecoveryCodesOut:
    user = auth.user
    if user.mfa_enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "MFA is already enabled")
    if not mfa_limiter.allow(str(user.id)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts")
    if not body.code or not mfa.verify_totp(user, body.code):
        audit(db, "auth.mfa.enable", user_id=user.id, decision="deny")
        await db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid code")
    user.mfa_enabled = True
    codes = await mfa.new_recovery_codes(db, user.id)
    audit(db, "auth.mfa.enable", user_id=user.id)
    await db.commit()
    return RecoveryCodesOut(recovery_codes=codes)


@router.post("/mfa/disable", status_code=204)
async def mfa_disable(body: CodeIn, auth: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db)) -> None:
    """Turning MFA off needs a current code (or a recovery code)."""
    user = auth.user
    if not user.mfa_enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "MFA is not enabled")
    if not mfa_limiter.allow(str(user.id)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts")
    if not await _check_second_factor(db, user, body):
        audit(db, "auth.mfa.disable", user_id=user.id, decision="deny")
        await db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid code")
    await mfa.reset(db, user)
    audit(db, "auth.mfa.disable", user_id=user.id)
    await db.commit()


@router.post("/mfa/recovery-codes")
async def regenerate_recovery_codes(
    body: CodeIn, auth: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db)
) -> RecoveryCodesOut:
    user = auth.user
    if not user.mfa_enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "MFA is not enabled")
    if not body.code or not mfa.verify_totp(user, body.code):
        audit(db, "auth.mfa.recovery_codes", user_id=user.id, decision="deny")
        await db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid code")
    codes = await mfa.new_recovery_codes(db, user.id)
    audit(db, "auth.mfa.recovery_codes", user_id=user.id)
    await db.commit()
    return RecoveryCodesOut(recovery_codes=codes)
