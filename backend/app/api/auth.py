from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, get_auth
from app.authz.policy import EffectivePermissions, content_containers, effective_permissions
from app.core.config import get_settings
from app.core.db import get_db
from app.core.ratelimit import RateLimiter
from app.core.security import new_token, sha256_hex, verify_password
from app.models import Session, User
from app.services.audit import audit

router = APIRouter(prefix="/api/auth", tags=["auth"])
login_limiter = RateLimiter(get_settings().login_rate_limit_per_minute)


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


def _me(auth_user: User, csrf: str, perms: EffectivePermissions | None = None) -> MeOut:
    return MeOut(
        id=str(auth_user.id),
        email=auth_user.email,
        display_name=auth_user.display_name,
        is_admin=auth_user.is_admin,
        roles=sorted(r.name for r in auth_user.roles),
        csrf_token=csrf,
        can_manage_content=perms is not None and bool(content_containers(perms)),
    )


@router.post("/login")
async def login(body: LoginIn, request: Request, response: Response, db: AsyncSession = Depends(get_db)) -> MeOut:
    settings = get_settings()
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

    token, csrf = new_token(), new_token()
    now = datetime.now(UTC)
    await db.execute(delete(Session).where(Session.user_id == user.id, Session.expires_at <= now))
    db.add(
        Session(
            id=sha256_hex(token),
            user_id=user.id,
            csrf_token=csrf,
            expires_at=now + timedelta(hours=settings.session_ttl_hours),
        )
    )
    audit(db, "auth.login", user_id=user.id, resource=f"user:{user.id}")
    await db.commit()
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
    )
    return _me(user, csrf, await effective_permissions(db, user))


@router.post("/logout", status_code=204)
async def logout(response: Response, auth: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db)) -> None:
    await db.execute(delete(Session).where(Session.id == auth.session.id))
    audit(db, "auth.logout", user_id=auth.user.id)
    await db.commit()
    response.delete_cookie(get_settings().session_cookie_name, path="/")


@router.get("/me")
async def me(auth: AuthContext = Depends(get_auth)) -> MeOut:
    return _me(auth.user, auth.session.csrf_token, auth.perms)
