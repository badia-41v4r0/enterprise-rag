"""Guided first-run setup API (PLAN-v0.4).

- `GET /api/info`: public; whether the installation is in setup mode, the company name and whether demo data is
  present (for the header, the login page and the demo banner). Never anything secret.
- `/api/setup/*`: the token-gated installation mode (ADR 0034). Every endpoint answers 404 once setup is completed.
  Steps 0 to 2 need the setup session obtained with the one-time setup token; from step 3 on, also the session of
  the administrator created in step 2.
- `/api/admin/setup/*`: the Setup assistant and the post-setup checklist, with the normal admin session.

The steps that change something sensitive (the cloud ceiling, the default chat model, removing demo data, asking
as another user after setup) need a recent re-authentication, as everywhere else (ADR 0019).
"""

from __future__ import annotations

import csv
import io
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import MeOut, _me, _new_session, _set_cookie
from app.api.deps import STEP_UP_REQUIRED, _session_and_user, recently_authenticated, require_admin
from app.authz.policy import effective_permissions
from app.core.config import get_settings
from app.core.db import get_db
from app.core.ratelimit import RateLimiter
from app.core.security import hash_password
from app.models import Chunk, Conversation, LlmModel, LlmProvider, Role, Session, SetupState, User, UserRole
from app.services import envcheck, ollama_models
from app.services import setup as svc
from app.services.audit import audit
from app.services.settings_store import get_setting

public_router = APIRouter(tags=["setup"])
wizard_router = APIRouter(prefix="/api/setup", tags=["setup"])
assistant_router = APIRouter(prefix="/api/admin/setup", tags=["setup"])

SETUP_CSRF_HEADER = "X-Setup-CSRF"
token_limiter = RateLimiter(10)  # per client address
token_global_limiter = RateLimiter(30)  # every client together: guessing 120 bits stays hopeless
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# --- Who is acting ----------------------------------------------------------------------------------------


@dataclass
class Actor:
    user: User | None
    session: Session | None
    wizard: bool  # True: installation mode (token); False: the Setup assistant (normal admin session)
    state: SetupState | None = None


async def _optional_admin(request: Request, db: AsyncSession) -> tuple[Session | None, User | None]:
    try:
        sess, user = await _session_and_user(request, db)
    except HTTPException:
        return None, None
    if sess.mfa_pending or not user.is_admin or user.must_change_password:
        return None, None
    return sess, user


async def wizard_actor(request: Request, db: AsyncSession = Depends(get_db)) -> Actor:
    if not await svc.setup_required(db):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
    row = await svc.session_valid(db, request.cookies.get(svc.SETUP_COOKIE))
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "setup token required")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        sent = request.headers.get(SETUP_CSRF_HEADER, "")
        if not sent or not row.session_csrf or sent != row.session_csrf:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "setup CSRF token missing or invalid")
    sess, user = await _optional_admin(request, db)
    return Actor(user=user, session=sess, wizard=True, state=row)


async def wizard_admin(actor: Actor = Depends(wizard_actor)) -> Actor:
    if actor.user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "sign in as the administrator created in step 2")
    return actor


async def assistant_admin(auth: Any = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> Actor:
    return Actor(user=auth.user, session=auth.session, wizard=False, state=await svc.get_state(db))


def _step_up(actor: Actor) -> None:
    if actor.session is None or not recently_authenticated(actor.session):
        raise HTTPException(status.HTTP_403_FORBIDDEN, STEP_UP_REQUIRED)


def _err(exc: svc.SetupError) -> HTTPException:
    return HTTPException(exc.status, str(exc))


def _uid(actor: Actor) -> uuid.UUID | None:
    return actor.user.id if actor.user else None


# --- Public -----------------------------------------------------------------------------------------------


class InfoOut(BaseModel):
    setup_required: bool
    company_name: str
    demo: bool
    product: str = "EnterpriseRAG"


@public_router.get("/api/info")
async def info(db: AsyncSession = Depends(get_db)) -> InfoOut:
    required = await svc.setup_required(db)
    org = await svc.get_organization(db)
    return InfoOut(setup_required=required, company_name=org["company_name"], demo=await svc.demo_present(db))


# --- Wizard only ---------------------------------------------------------------------------------------------


class StepOut(BaseModel):
    key: str
    index: int
    status: str  # pending | done | skipped
    required: bool


class StatusOut(BaseModel):
    mode: str
    steps: list[StepOut]
    current_step: int
    session: bool
    setup_csrf: str | None = None
    admin_signed_in: bool = False
    token_source: str  # "init" (printed by erag init) | "file" (log and token file)
    token_file: str
    install_type: str | None = None
    license_accepted: bool = False


@wizard_router.get("/status")
async def setup_status(request: Request, db: AsyncSession = Depends(get_db)) -> StatusOut:
    """Public while in setup mode: progress only, no secrets. 404 after completion."""
    if not await svc.setup_required(db):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
    s = get_settings()
    row = await svc.get_state(db)
    assert row is not None
    sess_row = await svc.session_valid(db, request.cookies.get(svc.SETUP_COOKIE))
    _, admin = await _optional_admin(request, db)
    steps = row.steps or {}
    return StatusOut(
        mode="setup",
        steps=[
            StepOut(
                key=k, index=i, status=str(steps.get(k, {}).get("status", "pending")), required=k in svc.REQUIRED_STEPS
            )
            for i, k in enumerate(svc.STEPS)
        ],
        current_step=svc.current_step(row),
        session=sess_row is not None,
        setup_csrf=sess_row.session_csrf if sess_row is not None else None,
        admin_signed_in=admin is not None and sess_row is not None,
        token_source="init" if s.setup_token_sha256 and row.token_hash == s.setup_token_sha256 else "file",
        token_file=str(s.setup_token_path),
        install_type=row.install_type,
        license_accepted=row.license_accepted_at is not None,
    )


class TokenIn(BaseModel):
    token: str = Field(max_length=200)


@wizard_router.post("/token")
async def enter_token(
    body: TokenIn, request: Request, response: Response, db: AsyncSession = Depends(get_db)
) -> dict[str, str]:
    """Exchange the one-time setup token for a setup session in this browser (an HttpOnly cookie limited to
    /api/setup). Rate limited; failures are audited without the value."""
    if not await svc.setup_required(db):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
    ip = request.client.host if request.client else "?"
    if not token_limiter.allow(ip) or not token_global_limiter.allow("all"):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts; wait a minute")
    if not await svc.verify_token(db, body.token):
        audit(db, "setup.token", decision="deny", details={"client": ip})
        await db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired setup token")
    raw, csrf = await svc.open_session(db)
    audit(db, "setup.token", details={"client": ip})
    await db.commit()
    response.set_cookie(
        svc.SETUP_COOKIE,
        raw,
        max_age=svc.SETUP_SESSION_HOURS * 3600,
        httponly=True,
        secure=get_settings().cookie_secure,
        samesite="strict",
        path="/api/setup",
    )
    return {"setup_csrf": csrf}


class LicenseOut(BaseModel):
    text: str
    sha256: str
    accepted_at: str | None = None
    accepted_by: str | None = None


@wizard_router.get("/license")
async def get_license(actor: Actor = Depends(wizard_actor)) -> LicenseOut:
    row = actor.state
    return LicenseOut(
        text=svc.LICENSE_TEXT,
        sha256=svc.license_text_sha256(),
        accepted_at=row.license_accepted_at.isoformat() if row and row.license_accepted_at else None,
        accepted_by=row.license_accepted_by if row else None,
    )


class LicenseIn(BaseModel):
    accepted: bool
    name: str = Field(max_length=320)
    text_sha256: str = Field(max_length=64)


@wizard_router.post("/license")
async def accept_license(
    body: LicenseIn, actor: Actor = Depends(wizard_actor), db: AsyncSession = Depends(get_db)
) -> LicenseOut:
    """Step 0. A record and a deliberate pause, not enforcement: no key, no license server, no network (ADR 0035)."""
    if not body.accepted:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Tick the box to accept the license notice")
    row = actor.state
    assert row is not None
    try:
        svc.accept_license(row, body.name, body.text_sha256)
    except svc.SetupError as exc:
        raise _err(exc) from exc
    audit(db, "setup.license.accept", details={"name": row.license_accepted_by, "text_sha256": body.text_sha256})
    await db.commit()
    return await get_license(actor)


class AdminIn(BaseModel):
    email: str = Field(max_length=320)
    display_name: str = Field(default="", max_length=200)
    password: str = Field(max_length=1024)


@wizard_router.post("/admin", status_code=201)
async def create_admin(
    body: AdminIn, response: Response, actor: Actor = Depends(wizard_actor), db: AsyncSession = Depends(get_db)
) -> MeOut:
    """Step 2: the administrator, who is then signed in. Their password is their own (not temporary)."""
    row = actor.state
    assert row is not None
    if row.license_accepted_at is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Accept the license notice first (step 0)")
    if (row.steps or {}).get("admin", {}).get("status") == "done":
        raise HTTPException(status.HTTP_409_CONFLICT, "The administrator was already created: sign in instead")
    email = body.email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Enter a valid email address")
    problems = svc.password_problems(body.password, email)
    if problems:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Password: " + "; ".join(problems))
    user = User(
        email=email,
        display_name=body.display_name.strip() or "Administrator",
        password_hash=hash_password(body.password),
        is_admin=True,
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "A user with this email already exists") from exc
    svc.mark_step(row, "admin", "done", user.id, email=email)
    lic = (row.steps or {}).get("license")
    if lic:
        row.steps = {**row.steps, "license": {**lic, "admin_email": email}}
    audit(db, "setup.admin.create", user_id=user.id, resource=f"user:{user.id}", details={"email": email})
    token, sess = _new_session(db, user, pending=False)
    await db.commit()
    await db.refresh(user, ["roles"])
    _set_cookie(response, token)
    return _me(user, sess, await effective_permissions(db, user))


class StepIn(BaseModel):
    status: str = Field(pattern="^(done|skipped)$")


@wizard_router.post("/steps/{key}")
async def mark_step(
    key: str, body: StepIn, actor: Actor = Depends(wizard_actor), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    row = actor.state
    assert row is not None
    if key in svc.ADMIN_STEPS and actor.user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "sign in as the administrator created in step 2")
    if key == "license":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "accept the license with POST /api/setup/license")
    if key == "finish":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "finish with POST /api/setup/finish")
    if key == "admin" and actor.user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "create the administrator or sign in as one")
    if key == "install_type" and body.status == "skipped":
        row.install_type = row.install_type or svc.EMPTY_INSTALL  # skipping means an empty installation
    if key == "organization" and body.status == "skipped":
        # A skipped step leaves the stricter setting: no cloud egress of internal data.
        org = await get_setting(db, svc.ORG_SETTING)
        if not org.get("cloud_max_classification"):
            await svc.put_organization(db, _uid(actor), company_name=None, cloud_max="public")
            row = await svc.get_state(db)
            assert row is not None
    try:
        svc.mark_step(row, key, body.status, _uid(actor))
    except svc.SetupError as exc:
        raise _err(exc) from exc
    audit(db, f"setup.step.{key}", user_id=_uid(actor), details={"status": body.status})
    await db.commit()
    return {"key": key, "status": body.status, "current_step": svc.current_step(row)}


class InstallTypeIn(BaseModel):
    type: str = Field(pattern="^(empty|demo)$")


@wizard_router.post("/install-type")
async def install_type(
    body: InstallTypeIn, actor: Actor = Depends(wizard_admin), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """Step 3: an empty installation (default: nothing is created) or the ACME Corp demo with mock data."""
    row = actor.state
    assert row is not None
    details: dict[str, Any] = {}
    if body.type == svc.DEMO_INSTALL:
        details = await svc.install_demo(db, _uid(actor))
    elif await svc.demo_present(db):
        raise HTTPException(status.HTTP_409_CONFLICT, "Demo data is installed: remove it first (Remove demo data)")
    row = await svc.get_state(db)
    assert row is not None
    row.install_type = body.type
    svc.mark_step(row, "install_type", "done", _uid(actor), type=body.type)
    audit(db, "setup.install_type", user_id=_uid(actor), details={"type": body.type})
    await db.commit()
    return {"type": body.type, **details}


@wizard_router.post("/finish")
async def finish(response: Response, actor: Actor = Depends(wizard_admin), db: AsyncSession = Depends(get_db)) -> MeOut:
    """Step 11: records completion, destroys the setup token and the setup session, audits, and leaves the
    administrator signed in. Refused in production while an administrator with a known password exists."""
    s = get_settings()
    row = actor.state
    assert row is not None and actor.user is not None and actor.session is not None
    steps = row.steps or {}
    missing = [k for k in svc.REQUIRED_STEPS if steps.get(k, {}).get("status") != "done"]
    if missing:
        raise HTTPException(status.HTTP_409_CONFLICT, "Steps not done: " + ", ".join(sorted(missing)))
    known = await svc.known_password_admins(db, s)
    if known and s.is_prod:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "An administrator has a known or temporary password: " + ", ".join(known) + ". Change it first.",
        )
    if "organization" not in steps:
        # Never visited: the stricter cloud ceiling.
        org = await get_setting(db, svc.ORG_SETTING)
        if not org.get("cloud_max_classification"):
            await svc.put_organization(db, actor.user.id, company_name=None, cloud_max="public")
        row = await svc.get_state(db)
        assert row is not None
    for key in svc.STEPS:
        if key not in (row.steps or {}) and key not in svc.REQUIRED_STEPS and key != "finish":
            svc.mark_step(row, key, "skipped", actor.user.id)
    if row.install_type is None:
        row.install_type = svc.EMPTY_INSTALL
    svc.mark_step(row, "finish", "done", actor.user.id)
    row.completed_at = svc._now()
    row.completed_by = actor.user.id
    row.completed_via = "wizard"
    row.token_hash = row.session_hash = row.session_csrf = None
    row.token_expires_at = row.session_expires_at = None
    audit(
        db,
        "setup.complete",
        user_id=actor.user.id,
        details={"via": "wizard", "install_type": row.install_type, "known_password_admins": known},
    )
    # A fresh session for the administrator (the wizard's session may be hours old).
    await db.execute(delete(Session).where(Session.id == actor.session.id))
    token, sess = _new_session(db, actor.user, pending=False)
    await db.commit()
    svc.remove_token_file(s)
    svc.invalidate_cache()
    _set_cookie(response, token)
    response.delete_cookie(svc.SETUP_COOKIE, path="/api/setup")
    return _me(actor.user, sess, await effective_permissions(db, actor.user))


# --- Shared by the wizard (token + admin session) and the Setup assistant (admin session) ---------------------


class OrgIn(BaseModel):
    company_name: str | None = Field(default=None, max_length=200)
    cloud_max_classification: str | None = Field(default=None, pattern="^(public|internal)$")


class ChatModelIn(BaseModel):
    model: str = Field(max_length=200)


class DownloadPreviewIn(BaseModel):
    model: str = Field(max_length=200)


class DownloadIn(BaseModel):
    model: str = Field(max_length=200)
    size_bytes: int = Field(gt=0)
    confirm: bool


class ModelTestIn(BaseModel):
    kind: str = Field(pattern="^(chat|embedding)$")
    model: str | None = Field(default=None, max_length=200)


class UserRowIn(BaseModel):
    email: str = Field(max_length=320)
    display_name: str = Field(default="", max_length=200)
    role_ids: list[uuid.UUID] = Field(default_factory=list)
    password: str | None = Field(default=None, max_length=1024)


class ImportIn(BaseModel):
    csv: str = Field(max_length=200_000)


class CheckIn(BaseModel):
    user_id: uuid.UUID
    question: str = Field(min_length=1, max_length=2000)


async def _create_temp_user(
    db: AsyncSession, actor: Actor, email: str, name: str, role_ids: list[uuid.UUID], password: str | None
) -> tuple[User, str]:
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        raise ValueError("invalid email")
    if await db.scalar(select(User.id).where(User.email == email)) is not None:
        raise ValueError("a user with this email already exists")
    temp = password or svc.temporary_password()
    if len(temp) < 12:
        raise ValueError("a temporary password needs at least 12 characters")
    user = User(email=email, display_name=name.strip(), password_hash=hash_password(temp), must_change_password=True)
    db.add(user)
    await db.flush()
    for rid in set(role_ids):
        db.add(UserRole(user_id=user.id, role_id=rid))
    audit(
        db,
        "admin.user.create",
        user_id=_uid(actor),
        resource=f"user:{user.id}",
        details={"email": email, "via": "setup", "must_change_password": True},
    )
    return user, temp


def shared_routes(
    router: APIRouter,
    admin_dep: Callable[..., Awaitable[Actor]],
    session_dep: Callable[..., Awaitable[Actor]],
) -> None:
    # Step 1 comes before the administrator exists: the setup session is enough to read the checks.
    @router.get("/environment")
    async def environment(
        request: Request, actor: Actor = Depends(session_dep), db: AsyncSession = Depends(get_db)
    ) -> dict[str, Any]:
        checks = await envcheck.run_checks(
            db,
            scheme=request.headers.get("x-forwarded-proto") or request.url.scheme,
            client_host=request.client.host if request.client else None,
        )
        return {"checks": checks, "summary": envcheck.summary(checks)}

    @router.get("/organization")
    async def get_org(actor: Actor = Depends(admin_dep), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
        return await svc.get_organization(db)

    @router.put("/organization")
    async def put_org(
        body: OrgIn, actor: Actor = Depends(admin_dep), db: AsyncSession = Depends(get_db)
    ) -> dict[str, Any]:
        current = await svc.get_organization(db)
        if body.cloud_max_classification and body.cloud_max_classification != current["cloud_max_classification"]:
            _step_up(actor)  # what may leave the building is a data-access decision
        try:
            out = await svc.put_organization(
                db, _uid(actor), company_name=body.company_name, cloud_max=body.cloud_max_classification
            )
        except svc.SetupError as exc:
            raise _err(exc) from exc
        if actor.wizard:
            row = await svc.get_state(db)
            assert row is not None
            svc.mark_step(row, "organization", "done", _uid(actor))
            await db.commit()
        return out

    @router.get("/models")
    async def models(actor: Actor = Depends(admin_dep), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
        """What the models step shows. Reads only: nothing is downloaded here."""
        s = get_settings()
        reachable, error, installed = True, "", []
        try:
            installed = await ollama_models_list(s)
        except Exception as exc:  # noqa: BLE001 - shown as "unreachable" with the fix
            reachable, error = False, type(exc).__name__
        names = {m["name"] for m in installed}
        default = await envcheck.default_ollama_model(db)
        locked = (await db.scalar(select(Chunk.id).limit(1))) is not None
        row = await svc.get_state(db)
        return {
            "ollama_url": s.ollama_base_url,
            "reachable": reachable,
            "error": error,
            "installed": installed,
            "embedding": {
                "model": s.embed_model,
                "dimensions": s.embed_dim,
                "installed": envcheck.has_model(names, s.embed_model),
                "locked": locked,
                "confirmed": bool(row and (row.steps or {}).get("models", {}).get("embedding_confirmed")),
            },
            "chat": {
                "environment_model": s.local_chat_model,
                "default_model": default,
                "installed": envcheck.has_model(names, default or s.local_chat_model),
            },
            "suggested": ollama_models.SUGGESTED,
            "downloads": ollama_models.pulls(),
            "registry": s.ollama_registry_url,
        }

    @router.post("/models/chat")
    async def choose_chat_model(
        body: ChatModelIn, actor: Actor = Depends(admin_dep), db: AsyncSession = Depends(get_db)
    ) -> dict[str, Any]:
        """Make an installed Ollama model the default chat model (the built-in `ollama` provider)."""
        from app.llm import registry
        from app.llm.router import invalidate_router_cache

        _step_up(actor)  # changing the default model needs re-authentication (ADR 0023)
        s = get_settings()
        name = body.model.strip()
        try:
            installed = {m["name"] for m in await ollama_models_list(s)}
        except Exception as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, f"Ollama is unreachable ({type(exc).__name__})") from exc
        if not envcheck.has_model(installed, name):
            raise HTTPException(status.HTTP_409_CONFLICT, f"{name} is not installed in Ollama; download it first")
        if name == s.embed_model or name.startswith(s.embed_model + ":"):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "That is the embedding model, not a chat model")
        await registry.ensure_builtin_providers(db, s)
        prov = await db.scalar(select(LlmProvider).where(LlmProvider.builtin, LlmProvider.slug == registry.OLLAMA))
        assert prov is not None
        model = next((m for m in prov.models if m.model == name), None)
        if model is None:
            model = LlmModel(provider_id=prov.id, model=registry.validate_model_name(name))
            db.add(model)
        model.enabled = True
        prov.enabled = True
        previous = await envcheck.default_ollama_model(db)
        await db.execute(update(LlmModel).where(LlmModel.is_default).values(is_default=False))
        await db.flush()
        model.is_default = True
        audit(
            db,
            "admin.model.default",
            user_id=_uid(actor),
            resource=f"model:ollama:{name}",
            details={"previous": previous, "new": f"ollama:{name}", "via": "setup"},
        )
        if actor.wizard:
            row = await svc.get_state(db)
            assert row is not None
            entry = (row.steps or {}).get("models", {})
            row.steps = {**(row.steps or {}), "models": {**entry, "chat_model": name}}
        await db.commit()
        invalidate_router_cache()
        return {"default_model_id": f"ollama:{name}"}

    @router.post("/models/test")
    async def test_model(body: ModelTestIn, actor: Actor = Depends(admin_dep)) -> dict[str, Any]:
        """A Test button: one tiny real request to the local Ollama (an embedding, or a chat reply of a few
        tokens). A model that is not installed is reported as such; Ollama never downloads it on this request."""
        import time

        s = get_settings()
        start = time.perf_counter()
        try:
            if body.kind == "embedding":
                from app.ingestion.embedder import OllamaEmbedder

                vec = (await OllamaEmbedder(model=body.model or s.embed_model).embed(["EnterpriseRAG setup test"]))[0]
                result: dict[str, Any] = {"dimensions": len(vec)}
            else:
                name = body.model or await envcheck.default_ollama_model_or_env(s)
                async with envcheck.ollama_client(timeout=120) as client:
                    r = await client.post(
                        f"{s.ollama_base_url.rstrip('/')}/api/chat",
                        json={
                            "model": name,
                            "messages": [{"role": "user", "content": "Reply with the single word OK."}],
                            "stream": False,
                            "options": {"num_predict": 8},
                        },
                    )
                if r.status_code >= 300:
                    detail = (
                        r.json().get("error", "")
                        if r.headers.get("content-type", "").startswith("application/json")
                        else ""
                    )
                    return {"ok": False, "error": f"HTTP {r.status_code} {detail}".strip()[:300]}
                result = {"model": name, "reply": str(r.json().get("message", {}).get("content", ""))[:80]}
        except Exception as exc:  # noqa: BLE001 - a test reports, it never raises
            return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
        return {"ok": True, "latency_ms": int((time.perf_counter() - start) * 1000), **result}

    @router.post("/models/embedding/confirm")
    async def confirm_embedding(
        actor: Actor = Depends(admin_dep), db: AsyncSession = Depends(get_db)
    ) -> dict[str, Any]:
        s = get_settings()
        row = await svc.get_state(db)
        if row is not None:
            entry = (row.steps or {}).get("models", {})
            row.steps = {**(row.steps or {}), "models": {**entry, "embedding_confirmed": s.embed_model}}
        audit(db, "setup.embedding.confirm", user_id=_uid(actor), details={"model": s.embed_model})
        await db.commit()
        return {"model": s.embed_model}

    @router.post("/models/download/preview")
    async def download_preview(
        body: DownloadPreviewIn, actor: Actor = Depends(admin_dep), db: AsyncSession = Depends(get_db)
    ) -> dict[str, Any]:
        """The confirmation's facts: name, size (from the registry manifest; the model itself is not fetched) and
        source. Nothing is downloaded."""
        s = get_settings()
        try:
            name = ollama_models.validate_name(body.model)
            size = await ollama_models.registry_size(name, s)
        except ollama_models.ModelDownloadError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        try:
            installed = {m["name"] for m in await ollama_models_list(s)}
        except Exception:  # noqa: BLE001
            installed = set()
        return {
            "model": name,
            "size_bytes": size,
            "source": f"the Ollama registry ({s.ollama_registry_url})",
            "already_installed": envcheck.has_model(installed, name),
        }

    @router.post("/models/download", status_code=202)
    async def download(
        body: DownloadIn, actor: Actor = Depends(admin_dep), db: AsyncSession = Depends(get_db)
    ) -> dict[str, Any]:
        """Start a download the administrator confirmed (the size they saw must still match the registry)."""
        if not body.confirm:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Confirm the download")
        s = get_settings()
        try:
            name = ollama_models.validate_name(body.model)
            size = await ollama_models.registry_size(name, s)
        except ollama_models.ModelDownloadError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        if size != body.size_bytes:
            raise HTTPException(status.HTTP_409_CONFLICT, "The model changed in the registry; review the size again")
        pull = ollama_models.start_pull(name, size, _uid(actor), s)
        audit(
            db,
            "setup.model.download.start",
            user_id=_uid(actor),
            resource=f"ollama-model:{name}",
            details={"size_bytes": size, "source": s.ollama_registry_url},
        )
        await db.commit()
        return pull.view()

    @router.get("/models/download/{pull_id}")
    async def download_status(pull_id: str, actor: Actor = Depends(admin_dep)) -> dict[str, Any]:
        pull = ollama_models.get_pull(pull_id)
        if pull is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such download")
        return pull.view()

    @router.delete("/models/download/{pull_id}")
    async def download_cancel(pull_id: str, actor: Actor = Depends(admin_dep)) -> dict[str, Any]:
        pull = await ollama_models.cancel_pull(pull_id)
        if pull is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such download")
        return pull.view()

    @router.get("/progress")
    async def progress(actor: Actor = Depends(admin_dep), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
        c = await svc.counts(db)
        return {**c, "demo": await svc.demo_present(db)}

    @router.post("/users", status_code=201)
    async def create_user(
        body: UserRowIn, actor: Actor = Depends(admin_dep), db: AsyncSession = Depends(get_db)
    ) -> dict[str, Any]:
        """One user with a temporary password (generated when none is given, shown once); they must change it at
        their first login."""
        if body.role_ids:
            found = set((await db.scalars(select(Role.id).where(Role.id.in_(body.role_ids)))).all())
            if found != set(body.role_ids):
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown role id")
        try:
            user, temp = await _create_temp_user(db, actor, body.email, body.display_name, body.role_ids, body.password)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        await db.commit()
        return {"id": str(user.id), "email": user.email, "temporary_password": temp if not body.password else None}

    @router.post("/users/import")
    async def import_users(
        body: ImportIn, actor: Actor = Depends(admin_dep), db: AsyncSession = Depends(get_db)
    ) -> dict[str, Any]:
        """Paste a CSV with the columns email, name, role (a header row is optional; several roles separated by
        `;`). Every valid row becomes a user with a generated temporary password, shown once; every invalid row is
        reported with its line number and reason, and nothing of it is created."""
        roles = {r.name.lower(): r.id for r in (await db.scalars(select(Role))).all()}
        reader = csv.reader(io.StringIO(body.csv.strip()))
        created: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        seen: set[str] = set()
        for lineno, raw in enumerate(reader, start=1):
            cells = [c.strip() for c in raw]
            if not any(cells):
                continue
            if lineno == 1 and cells and cells[0].lower() == "email":
                continue
            if len(cells) < 1 or len(cells) > 3:
                errors.append({"line": lineno, "email": cells[0] if cells else "", "error": "expected email,name,role"})
                continue
            email = cells[0].lower()
            name = cells[1] if len(cells) > 1 else ""
            role_names = [x.strip() for x in (cells[2] if len(cells) > 2 else "").split(";") if x.strip()]
            unknown = [r for r in role_names if r.lower() not in roles]
            if email in seen:
                errors.append({"line": lineno, "email": email, "error": "duplicate email in the list"})
                continue
            if unknown:
                errors.append({"line": lineno, "email": email, "error": "unknown role: " + ", ".join(unknown)})
                continue
            try:
                async with db.begin_nested():
                    user, temp = await _create_temp_user(
                        db, actor, email, name, [roles[r.lower()] for r in role_names], None
                    )
            except ValueError as exc:
                errors.append({"line": lineno, "email": email, "error": str(exc)})
                continue
            seen.add(email)
            created.append({"line": lineno, "email": user.email, "roles": role_names, "temporary_password": temp})
            if len(created) > 500:
                errors.append({"line": lineno, "email": "", "error": "at most 500 users per import"})
                break
        audit(db, "setup.users.import", user_id=_uid(actor), details={"created": len(created), "errors": len(errors)})
        await db.commit()
        return {"created": created, "errors": errors}

    @router.post("/check")
    async def check_as_user(
        body: CheckIn, actor: Actor = Depends(admin_dep), db: AsyncSession = Depends(get_db)
    ) -> dict[str, Any]:
        """Step 9, "Does it work?": run a question through the real pipeline (permissions, retrieval, routing, the
        chosen model) as a chosen user. The answer text is shown only while setup is running; afterwards the
        assistant shows sources, model and classification without the answer, since an administrator is not a
        data reader (ADR 0009, ADR 0041). Audited without the question; the test conversation is deleted."""
        from app.api.admin import user_effective_permissions
        from app.ingestion.embedder import get_embedder
        from app.llm.router import load_router
        from app.orchestrator.chat import ChatOrchestrator

        if not actor.wizard:
            _step_up(actor)
        user = await db.get(User, body.user_id)
        if user is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
        await db.refresh(user, ["roles"])
        eff = await user_effective_permissions(user.id, None, db)  # type: ignore[arg-type]
        perms = await effective_permissions(db, user)
        orch = ChatOrchestrator(db, get_embedder(), await load_router(db), None)
        events: list[dict[str, Any]] = []
        conv_id: str | None = None
        try:
            async for ev in orch.run(user, perms, body.question):
                if ev["type"] == "meta":
                    conv_id = ev["conversation_id"]
                if ev["type"] != "delta":
                    events.append(ev)
        finally:
            if conv_id:
                await db.execute(delete(Conversation).where(Conversation.id == uuid.UUID(conv_id)))
                await db.commit()
        done = next((e for e in events if e["type"] == "done"), {})
        cites: list[dict[str, Any]] = next((e["items"] for e in events if e["type"] == "citations"), [])
        refusal = next((e["reason"] for e in events if e["type"] == "refusal"), None)
        error = next((e.get("message") for e in events if e["type"] == "error"), None)
        sources = sorted({(c["title"], c["container"], c["classification"]) for c in cites}, key=lambda x: (x[1], x[0]))
        audit(
            db,
            "setup.check_as_user",
            user_id=_uid(actor),
            resource=f"user:{user.id}",
            details={
                "sources": len(sources),
                "model": done.get("model"),
                "classification": done.get("classification"),
                "answer_shown": actor.wizard,
            },
        )
        await db.commit()
        return {
            "user": {"id": str(user.id), "email": user.email, "roles": eff.roles},
            "effective": eff.model_dump(mode="json"),
            "sources": [{"title": t, "container": c, "classification": k} for t, c, k in sources],
            "model": done.get("model"),
            "classification": done.get("classification"),
            "refusal": refusal,
            "error": error,
            "answer": done.get("answer") if actor.wizard else None,
            "answer_hidden": not actor.wizard,
        }

    @router.get("/summary")
    async def summary(
        request: Request, actor: Actor = Depends(admin_dep), db: AsyncSession = Depends(get_db)
    ) -> dict[str, Any]:
        checks = await envcheck.run_checks(
            db,
            scheme=request.headers.get("x-forwarded-proto") or request.url.scheme,
            client_host=request.client.host if request.client else None,
        )
        row = await svc.get_state(db)
        return {
            "counts": await svc.counts(db),
            "organization": await svc.get_organization(db),
            "checklist": await svc.security_checklist(db, actor.user),
            "environment": envcheck.summary(checks),
            "install_type": row.install_type if row else None,
            "known_password_admins": await svc.known_password_admins(db),
            "production": get_settings().is_prod,
            "steps": (row.steps if row else {}) or {},
        }

    @router.get("/report", response_class=PlainTextResponse)
    async def report(actor: Actor = Depends(admin_dep), db: AsyncSession = Depends(get_db)) -> PlainTextResponse:
        text = await svc.report_markdown(db, actor.user)
        audit(db, "setup.report", user_id=_uid(actor))
        await db.commit()
        return PlainTextResponse(
            text,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="enterprise-rag-setup-report.md"'},
        )

    @router.get("/demo")
    async def demo_inventory(actor: Actor = Depends(admin_dep), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
        return {"present": await svc.demo_present(db), **await svc.demo_inventory(db, _uid(actor))}

    @router.delete("/demo")
    async def remove_demo(actor: Actor = Depends(admin_dep), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
        _step_up(actor)
        assert actor.user is not None
        if not await svc.demo_present(db):
            raise HTTPException(status.HTTP_409_CONFLICT, "No demo data is installed")
        return await svc.remove_demo(db, actor.user.id)


async def ollama_models_list(s: Any) -> list[dict[str, Any]]:
    return await envcheck.ollama_models(s)


shared_routes(wizard_router, wizard_admin, wizard_actor)
shared_routes(assistant_router, assistant_admin, assistant_admin)


# --- Setup assistant only ---------------------------------------------------------------------------------------


@assistant_router.get("/state")
async def state(actor: Actor = Depends(assistant_admin)) -> dict[str, Any]:
    row = actor.state
    if row is None:
        return {"completed": True, "steps": {}, "completed_via": None}
    return {
        "completed": row.completed_at is not None,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "completed_via": row.completed_via,
        "install_type": row.install_type,
        "license_accepted_at": row.license_accepted_at.isoformat() if row.license_accepted_at else None,
        "license_accepted_by": row.license_accepted_by,
        "steps": row.steps or {},
    }


@assistant_router.get("/checklist")
async def get_checklist(actor: Actor = Depends(assistant_admin), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    assert actor.user is not None
    return await svc.checklist(db, actor.user)


@assistant_router.post("/checklist/{key}/dismiss", status_code=204)
async def dismiss(key: str, actor: Actor = Depends(assistant_admin), db: AsyncSession = Depends(get_db)) -> None:
    assert actor.user is not None
    items = {i["key"]: i for i in (await svc.checklist(db, actor.user))["items"]}
    if key not in items or not items[key]["dismissable"]:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "This item cannot be dismissed")
    await svc.dismiss_checklist_item(db, key, actor.user.id)
