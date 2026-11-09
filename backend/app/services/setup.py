"""Guided first-run setup (PLAN-v0.4): setup mode, the one-time setup token, step bookkeeping, the license
acceptance record, organization settings, the demo installation and its removal, the review summary, the setup
report and the post-setup checklist.

Setup mode (ADR 0034): a `setup_state` row whose `completed_at` is NULL. Only `erag bootstrap` creates that row,
and only for a database without any user. An upgrade of an existing installation writes a completed row instead
(migration 0012 and `init_on_start`), so it is never put into setup mode.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import logging
import os
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.security import constant_time_equals, new_token, sha256_hex, verify_password
from app.models import (
    AppSetting,
    Chunk,
    Classification,
    Connector,
    Container,
    Document,
    DocumentVersion,
    Grant,
    LlmProvider,
    McpServer,
    ResourceType,
    Role,
    SetupState,
    Tool,
    User,
    VersionStatus,
)
from app.services.audit import audit
from app.services.settings_store import get_setting, put_setting

log = logging.getLogger(__name__)

# --- Steps ------------------------------------------------------------------------------------------------

STEPS: list[str] = [
    "license",  # 0
    "environment",  # 1
    "admin",  # 2
    "install_type",  # 3
    "organization",  # 4
    "models",  # 5
    "access",  # 6
    "people",  # 7
    "knowledge",  # 8
    "check",  # 9
    "tools",  # 10
    "finish",  # 11
]
# Steps that cannot be skipped. Every other step may be skipped; a skipped step leaves the stricter setting.
REQUIRED_STEPS = {"license", "admin"}
# Steps that need the administrator's session (created in step 2) besides the setup session.
ADMIN_STEPS = set(STEPS[3:])

SETUP_COOKIE = "erag_setup"
SETUP_SESSION_HOURS = 4
DEMO_INSTALL = "demo"
EMPTY_INSTALL = "empty"
ORG_SETTING = "organization"


class SetupError(Exception):
    """A request that cannot be honoured (shown to the administrator as is; never contains a secret)."""

    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


# --- Setup mode, cached -----------------------------------------------------------------------------------

_COMPLETED_TTL = 30.0  # seconds a "not in setup mode" answer is cached (a CLI reset is seen within this time)
_cache: dict[str, float] = {}


def invalidate_cache() -> None:
    _cache.clear()


async def get_state(db: AsyncSession) -> SetupState | None:
    return await db.scalar(select(SetupState).where(SetupState.id == 1).execution_options(populate_existing=True))


async def setup_required(db: AsyncSession) -> bool:
    """True while the installation is in setup mode (a setup row that is not completed)."""
    seen = _cache.get("not_required")
    if seen is not None and time.monotonic() - seen < _COMPLETED_TTL:
        return False
    row = await get_state(db)
    required = row is not None and row.completed_at is None
    if not required:
        _cache["not_required"] = time.monotonic()
    return required


# --- The setup token ----------------------------------------------------------------------------------------

_TOKEN_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # noqa: S105 - no I, O, 0, 1: easy to read from a terminal


def generate_token() -> str:
    """Six groups of four characters from a 32-symbol alphabet: 120 random bits, easy to type."""
    chars = "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(24))
    return "-".join(chars[i : i + 4] for i in range(0, 24, 4))


def normalize_token(raw: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", raw.upper())


def token_hash(raw: str) -> str:
    return sha256_hex("erag-setup-token:" + normalize_token(raw))


def write_token_file(path: os.PathLike[str] | str, token: str) -> None:
    """Write the token to a file only its owner can read (replacing an older one)."""
    p = os.fspath(path)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    with contextlib.suppress(FileNotFoundError):
        os.unlink(p)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(token + "\n")


def remove_token_file(settings: Settings) -> None:
    try:
        settings.setup_token_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:  # never fail completion because of a file
        log.warning("could not remove the setup token file: %s", exc.strerror)


@dataclass
class StartResult:
    mode: str  # "setup" | "completed" | "upgraded"
    new_token: str | None = None  # a freshly generated token, to print once
    token_from_init: bool = False  # the token printed by `erag init` is in use


def _now() -> datetime:
    return datetime.now(UTC)


async def _has_users(db: AsyncSession) -> bool:
    return bool(await db.scalar(select(exists().where(User.id.is_not(None)))))


async def _has_admin(db: AsyncSession) -> bool:
    return bool(await db.scalar(select(exists().where(User.is_admin.is_(True)))))


async def complete_existing(db: AsyncSession, via: str, user_id: uuid.UUID | None = None) -> None:
    """Mark setup completed for an installation that got its administrator another way (an upgrade, the
    `erag create-admin` command, or ERAG_SEED_DEMO=true). Destroys any setup token. Commits."""
    s = get_settings()
    row = await get_state(db)
    if row is None:
        row = SetupState(id=1, steps={}, checklist_dismissed=[])
        db.add(row)
    if row.completed_at is None:
        row.completed_at = _now()
        row.completed_by = user_id
        row.completed_via = via
        row.token_hash = row.session_hash = row.session_csrf = None
        row.token_expires_at = row.session_expires_at = None
        audit(db, "setup.complete", user_id=user_id, details={"via": via})
    await db.commit()
    remove_token_file(s)
    invalidate_cache()


async def init_on_start(db: AsyncSession, settings: Settings | None = None) -> StartResult:
    """Called by `erag bootstrap` at every start. Commits.

    - No row and the database has users: an existing installation, recorded as completed (never setup mode).
    - No row and no users: a fresh installation enters setup mode.
    - In setup mode, a setup token exists and has not expired: the one printed by `erag init` (only its hash is
      known, from ERAG_SETUP_TOKEN_SHA256), or a new one generated here, written to the token file and returned
      so the caller prints it to the log."""
    s = settings or get_settings()
    row = await get_state(db)
    if row is None:
        if await _has_users(db):
            await complete_existing(db, "upgrade")
            if not await _has_admin(db):
                log.warning("no administrator exists; create one with `erag create-admin --email ...`")
            return StartResult("upgraded")
        row = SetupState(id=1, steps={}, checklist_dismissed=[])
        db.add(row)
        await db.flush()
    if row.completed_at is not None:
        await db.commit()
        remove_token_file(s)  # a token file left over from before completion is useless; do not keep it
        return StartResult("completed")

    now = _now()
    result = StartResult("setup")
    expired = row.token_expires_at is None or row.token_expires_at <= now
    if row.token_hash is None and s.setup_token_sha256 and re.fullmatch(r"[0-9a-f]{64}", s.setup_token_sha256):
        row.token_hash = s.setup_token_sha256
        row.token_expires_at = now + timedelta(hours=s.setup_token_ttl_hours)
        result.token_from_init = True
        remove_token_file(s)
    elif row.token_hash is None or expired:
        token = generate_token()
        row.token_hash = token_hash(token)
        row.token_expires_at = now + timedelta(hours=s.setup_token_ttl_hours)
        row.session_hash = row.session_csrf = None
        try:
            write_token_file(s.setup_token_path, token)
        except OSError as exc:
            log.warning("could not write the setup token file: %s", exc.strerror)
        result.new_token = token
    else:
        result.token_from_init = row.token_hash == s.setup_token_sha256
    audit(db, "setup.start", details={"new_token": result.new_token is not None, "from_init": result.token_from_init})
    await db.commit()
    invalidate_cache()
    return result


async def new_token_cli(db: AsyncSession, settings: Settings | None = None, *, reopen: bool = False) -> str:
    """`erag setup --new-token` (setup mode only) and `erag setup --reset` (re-opens a completed setup). Server
    side only: needs the database and the data volume, never a web request. Commits."""
    s = settings or get_settings()
    row = await get_state(db)
    if row is None:
        row = SetupState(id=1, steps={}, checklist_dismissed=[])
        db.add(row)
    if row.completed_at is not None and not reopen:
        raise SetupError("setup is already completed; use `erag setup --reset` to re-open it")
    if reopen:
        row.completed_at = None
        row.completed_by = None
        row.completed_via = None
        row.steps = {}
        row.license_accepted_at = row.license_accepted_by = row.license_text_sha256 = None
    token = generate_token()
    row.token_hash = token_hash(token)
    row.token_expires_at = _now() + timedelta(hours=s.setup_token_ttl_hours)
    row.session_hash = row.session_csrf = None
    row.session_expires_at = None
    audit(db, "setup.reset" if reopen else "setup.new_token", details={"via": "cli"})
    await db.commit()
    try:
        write_token_file(s.setup_token_path, token)
    except OSError as exc:
        log.warning("could not write the setup token file: %s", exc.strerror)
    invalidate_cache()
    return token


async def verify_token(db: AsyncSession, raw: str) -> bool:
    row = await get_state(db)
    if row is None or row.completed_at is not None or not row.token_hash or not raw:
        return False
    if row.token_expires_at is None or row.token_expires_at <= _now():
        return False
    return constant_time_equals(token_hash(raw), row.token_hash)


async def open_session(db: AsyncSession) -> tuple[str, str]:
    """After a correct token: a setup session for this browser (the previous one, if any, ends). Commits."""
    row = await get_state(db)
    assert row is not None
    raw, csrf = new_token(), new_token()
    row.session_hash = sha256_hex(raw)
    row.session_csrf = csrf
    row.session_expires_at = _now() + timedelta(hours=SETUP_SESSION_HOURS)
    await db.commit()
    return raw, csrf


async def session_valid(db: AsyncSession, raw: str | None) -> SetupState | None:
    if not raw:
        return None
    row = await get_state(db)
    if row is None or row.completed_at is not None or not row.session_hash:
        return None
    if row.session_expires_at is None or row.session_expires_at <= _now():
        return None
    if not constant_time_equals(sha256_hex(raw), row.session_hash):
        return None
    return row


# --- Step bookkeeping -----------------------------------------------------------------------------------------


def mark_step(row: SetupState, key: str, status: str, user_id: uuid.UUID | None = None, **extra: Any) -> None:
    if key not in STEPS:
        raise SetupError(f"unknown step {key!r}", 404)
    if status not in ("done", "skipped"):
        raise SetupError("status must be done or skipped", 422)
    if status == "skipped" and key in REQUIRED_STEPS:
        raise SetupError(f"step {key!r} cannot be skipped", 422)
    entry = {"status": status, "at": _now().isoformat(), "by": str(user_id) if user_id else None, **extra}
    row.steps = {**(row.steps or {}), key: entry}


def current_step(row: SetupState) -> int:
    steps = row.steps or {}
    for i, key in enumerate(STEPS):
        if key == "finish":
            return i
        if key not in steps:
            return i
    return len(STEPS) - 1


# --- License acceptance (ADR 0035) -------------------------------------------------------------------------------

LICENSE_TEXT = "\n\n".join(
    [
        "EnterpriseRAG license, in plain language",
        "EnterpriseRAG is source-available software licensed under the PolyForm Noncommercial License 1.0.0. "
        "It is not open source.",
        "- Noncommercial use is free: personal use, research, study, hobby projects, and use by educational, "
        "charitable, public-research and government institutions.\n"
        "- Commercial use, including running EnterpriseRAG inside a company for business purposes, requires a "
        "commercial license from the licensor, Alvaro Badia (Badia Partners): https://badiapartners.com\n"
        "- 60-day evaluation permit: any company or organization may install and run EnterpriseRAG for evaluation "
        "only, for up to 60 days from first installation, in a non-production environment, without redistributing "
        "it or offering it to third parties. After 60 days, stop using it or obtain a commercial license. You do not "
        "need to ask or notify us. The permit is provided as-is and may be withdrawn for future versions.",
        "This summary does not replace the license. The full text is in the LICENSE file shipped with the software "
        "and at https://polyformproject.org/licenses/noncommercial/1.0.0/",
        "By ticking the box you confirm that you have read this notice and that your use of EnterpriseRAG falls "
        "within the license, the evaluation permit, or a commercial license you hold. This records your acceptance "
        "on this installation; nothing is sent anywhere.",
    ]
)


def license_text_sha256() -> str:
    return hashlib.sha256(LICENSE_TEXT.encode()).hexdigest()


def accept_license(row: SetupState, name: str, text_sha256: str) -> None:
    if text_sha256 != license_text_sha256():
        raise SetupError("the license text changed; reload the page and read it again", 409)
    name = name.strip()
    if not name:
        raise SetupError("enter the name of the person accepting", 422)
    row.license_accepted_at = _now()
    row.license_accepted_by = name[:320]
    row.license_text_sha256 = text_sha256
    mark_step(row, "license", "done", None, accepted_by=row.license_accepted_by, text_sha256=text_sha256)


# --- Passwords ---------------------------------------------------------------------------------------------------

_COMMON = {
    "password1234",
    "password12345",
    "123456789012",
    "qwertyuiop12",
    "letmein12345",
    "administrator",
    "changeme1234",
    "welcome12345",
    "acme-demo-password",
}


def password_problems(password: str, email: str = "") -> list[str]:
    """Server-side password policy (the wizard also shows a strength meter)."""
    problems: list[str] = []
    if len(password) < 12:
        problems.append("use at least 12 characters")
    if len(password) > 1024:
        problems.append("use at most 1024 characters")
    if len(set(password)) < 5:
        problems.append("use more different characters")
    low = password.lower()
    if low in _COMMON or low == get_settings().demo_password.get_secret_value().lower():
        problems.append("this password is too common")
    local = email.split("@", 1)[0].lower()
    if email and (low == email.lower() or (len(local) >= 4 and local in low)):
        problems.append("do not use your email address in the password")
    return problems


def temporary_password() -> str:
    """A random temporary password (the user must change it at first login)."""
    return base64.b32encode(secrets.token_bytes(10)).decode().lower().rstrip("=")[:16]


async def known_password_admins(db: AsyncSession, settings: Settings | None = None) -> list[str]:
    """Active administrators whose password is known to someone else: the published demo password, the demo's
    admin account, or a temporary password that was never changed."""
    s = settings or get_settings()
    demo_pw = s.demo_password.get_secret_value()
    out: list[str] = []
    for u in (await db.scalars(select(User).where(User.is_admin.is_(True), User.is_active.is_(True)))).all():
        if u.email == "admin@acme.test" or u.must_change_password or verify_password(u.password_hash, demo_pw):
            out.append(u.email)
    return out


# --- Organization (ADR 0034: stored in the database, the environment as initial default) ---------------------


async def get_organization(db: AsyncSession, settings: Settings | None = None) -> dict[str, Any]:
    from app.llm import registry

    s = settings or get_settings()
    stored = await get_setting(db, ORG_SETTING)
    await registry.refresh_cloud_cap(db)
    demo = await demo_present(db)
    name = str(stored.get("company_name") or "") or ("ACME Corp" if demo else "")
    return {
        "company_name": name,
        "cloud_max_classification": registry.cloud_cap(s).value,
        "cloud_max_source": "database" if stored.get("cloud_max_classification") else "environment",
        "environment_cloud_max_classification": s.cloud_max_classification,
    }


async def put_organization(
    db: AsyncSession, user_id: uuid.UUID | None, *, company_name: str | None, cloud_max: str | None
) -> dict[str, Any]:
    from app.llm import registry

    stored = await get_setting(db, ORG_SETTING)
    before = dict(stored)
    if company_name is not None:
        stored["company_name"] = company_name.strip()[:200]
    if cloud_max is not None:
        if cloud_max not in (Classification.public.value, Classification.internal.value):
            raise SetupError("the cloud ceiling is public or internal; confidential data never leaves", 422)
        stored["cloud_max_classification"] = cloud_max
    await put_setting(db, ORG_SETTING, stored, user_id)
    audit(db, "setup.organization", user_id=user_id, details={"before": before, "after": stored})
    await db.commit()
    await registry.refresh_cloud_cap(db)
    from app.llm.router import invalidate_router_cache

    invalidate_router_cache()
    return await get_organization(db)


# --- Demo installation (ADR 0036) ----------------------------------------------------------------------------------


def _demo() -> Any:
    from demo.acme import seed as acme

    return acme


async def demo_present(db: AsyncSession) -> bool:
    acme = _demo()
    slugs = [c.slug for c in acme.CONTAINERS]
    emails = [p.email for p in acme.PERSONAS]
    containers = await db.scalar(select(exists().where(Container.slug.in_(slugs))))
    users = await db.scalar(select(exists().where(User.email.in_(emails))))
    return bool(containers or users)


async def install_demo(db: AsyncSession, user_id: uuid.UUID | None, settings: Settings | None = None) -> dict[str, Any]:
    """ACME Corp without its admin persona: the administrator is the person who ran the wizard. Documents are
    queued for the worker. Commits."""
    from app.mcp.registry import sync_registry

    s = settings or get_settings()
    acme = _demo()
    containers = await acme.seed_structure(db, s.demo_password.get_secret_value(), include_admin=False)
    queued = await acme.seed_documents(db, containers)
    tools = 0
    try:
        await sync_registry(db)
        tools = await acme.seed_tool_grants(db)
    except Exception as exc:  # noqa: BLE001 - the reference MCP servers are optional for the demo
        await db.rollback()
        log.warning("demo: reference MCP servers not registered: %s", type(exc).__name__)
    connector = await acme.seed_connector(db)
    office = await acme.seed_office_mcp(db, attempts=2)
    personas = [p.email for p in acme.PERSONAS if not p.is_admin]
    details = {
        "containers": len(containers),
        "personas": personas,
        "documents_queued": queued,
        "tool_grants": tools,
        "connector": connector,
        "office_mcp": office,
    }
    audit(db, "setup.demo.install", user_id=user_id, details=details)
    await db.commit()
    return details


async def demo_inventory(db: AsyncSession, actor_id: uuid.UUID | None) -> dict[str, Any]:
    """What `Remove demo data` deletes (shown in the confirmation, and used by the removal itself)."""
    acme = _demo()
    users = (
        await db.scalars(select(User).where(User.email.in_([p.email for p in acme.PERSONAS])).order_by(User.email))
    ).all()
    roles = (await db.scalars(select(Role).where(Role.name.in_(list(acme.ROLES))).order_by(Role.name))).all()
    containers = (
        await db.scalars(
            select(Container).where(Container.slug.in_([c.slug for c in acme.CONTAINERS])).order_by(Container.name)
        )
    ).all()
    cids = [c.id for c in containers]
    docs = await db.scalar(select(func.count(Document.id)).where(Document.container_id.in_(cids))) if cids else 0
    connectors = (
        (await db.scalars(select(Connector.name).where(Connector.container_id.in_(cids)))).all() if cids else []
    )
    servers = (
        await db.scalars(
            select(McpServer.name)
            .where(McpServer.name.in_(["acme-office", "files", "tickets"]))
            .order_by(McpServer.name)
        )
    ).all()
    return {
        "users": [u.email for u in users if u.id != actor_id],
        "skipped_users": [u.email for u in users if u.id == actor_id],
        "roles": [r.name for r in roles],
        "containers": [c.name for c in containers],
        "documents": int(docs or 0),
        "connectors": list(connectors),
        "mcp_servers": list(servers),
    }


async def remove_demo(db: AsyncSession, actor_id: uuid.UUID) -> dict[str, Any]:
    """Turn a demo installation into an empty one: delete the ACME personas (never the acting administrator),
    roles, containers (with their documents, versions, chunks, jobs and connectors), the demo MCP servers and
    their tools, and the grants pointing at any of them. The audit log is kept. Commits."""
    from app.ingestion.blobstore import get_blobstore

    acme = _demo()
    inv = await demo_inventory(db, actor_id)
    container_ids = list(
        (await db.scalars(select(Container.id).where(Container.slug.in_([c.slug for c in acme.CONTAINERS])))).all()
    )
    doc_ids = (
        list((await db.scalars(select(Document.id).where(Document.container_id.in_(container_ids)))).all())
        if container_ids
        else []
    )
    server_ids = list((await db.scalars(select(McpServer.id).where(McpServer.name.in_(inv["mcp_servers"])))).all())
    tool_ids = (
        list((await db.scalars(select(Tool.id).where(Tool.server_id.in_(server_ids)))).all()) if server_ids else []
    )
    blob_keys: set[str] = set()
    if doc_ids:
        blob_keys = set(
            (await db.scalars(select(DocumentVersion.blob_key).where(DocumentVersion.document_id.in_(doc_ids)))).all()
        )
    # Grants carry no foreign key to their resource: remove those that point at what goes away.
    for rtype, ids in (
        (ResourceType.container, container_ids),
        (ResourceType.document, doc_ids),
        (ResourceType.tool, tool_ids),
    ):
        if ids:
            await db.execute(delete(Grant).where(Grant.resource_type == rtype, Grant.resource_id.in_(ids)))
    if inv["users"]:
        await db.execute(delete(User).where(User.email.in_(inv["users"]), User.id != actor_id))
    if inv["roles"]:
        await db.execute(delete(Role).where(Role.name.in_(inv["roles"])))
    if container_ids:
        await db.execute(delete(Connector).where(Connector.container_id.in_(container_ids)))
        await db.execute(delete(Container).where(Container.id.in_(container_ids)))
    if server_ids:
        await db.execute(delete(McpServer).where(McpServer.id.in_(server_ids)))
    row = await get_state(db)
    if row is not None:
        row.install_type = EMPTY_INSTALL
    audit(db, "setup.demo.remove", user_id=actor_id, details=inv)
    await db.commit()
    # Blobs are content-addressed: delete only those no remaining version uses.
    blobs = get_blobstore()
    for key in blob_keys:
        still = await db.scalar(select(exists().where(DocumentVersion.blob_key == key)))
        if not still:
            try:
                await blobs.delete(key)
            except (OSError, ValueError):
                log.warning("demo removal: blob %s not deleted", key[:12])
    return inv


# --- Progress, summary, report, checklist --------------------------------------------------------------------------


async def counts(db: AsyncSession) -> dict[str, Any]:
    by_cls = dict(
        (await db.execute(select(Container.classification, func.count()).group_by(Container.classification))).all()
    )
    docs = dict(
        (
            await db.execute(
                select(DocumentVersion.status, func.count())
                .join(Document, Document.current_version_id == DocumentVersion.id)
                .group_by(DocumentVersion.status)
            )
        ).all()
    )
    return {
        "admins": int(
            await db.scalar(select(func.count()).where(User.is_admin.is_(True), User.is_active.is_(True))) or 0
        ),
        "users": int(await db.scalar(select(func.count(User.id))) or 0),
        "roles": int(await db.scalar(select(func.count(Role.id))) or 0),
        "containers": int(sum(by_cls.values())),
        "containers_by_classification": {c.value: int(by_cls.get(c, 0)) for c in Classification},
        "grants": int(await db.scalar(select(func.count(Grant.id))) or 0),
        "documents": int(await db.scalar(select(func.count(Document.id))) or 0),
        "documents_ready": int(docs.get(VersionStatus.ready, 0)),
        "documents_pending": int(docs.get(VersionStatus.pending, 0) + docs.get(VersionStatus.processing, 0)),
        "documents_failed": int(docs.get(VersionStatus.failed, 0)),
        "chunks": int(await db.scalar(select(func.count(Chunk.id))) or 0),
        "connectors": int(await db.scalar(select(func.count(Connector.id))) or 0),
        "tools_enabled": int(await db.scalar(select(func.count(Tool.id)).where(Tool.enabled.is_(True))) or 0),
    }


async def security_checklist(
    db: AsyncSession, actor: User | None, settings: Settings | None = None
) -> list[dict[str, Any]]:
    """The review step's security checklist: ok / warn items, booleans only."""
    from app.llm import registry
    from app.modelrouter.service import load_config

    s = settings or get_settings()
    known = await known_password_admins(db, s)
    c = await counts(db)
    demo = await demo_present(db)
    router_cfg = await load_config(db)
    providers = (await db.scalars(select(LlmProvider))).all()
    cloud = [p for p in providers if p.enabled and p.locality != "local" and not (p.builtin and p.slug == "openrouter")]
    unbudgeted = [p.slug for p in cloud if p.monthly_budget_usd is None]
    cap = registry.cloud_cap(s)

    def item(key: str, ok: bool, label: str, fix: str = "") -> dict[str, Any]:
        return {"key": key, "status": "ok" if ok else "warn", "label": label, "fix": "" if ok else fix}

    return [
        item(
            "mfa",
            bool(actor and actor.mfa_enabled),
            "MFA is enabled for your administrator account",
            "Enable MFA on the Account page (authenticator app).",
        ),
        item(
            "known_password_admin",
            not known,
            "No administrator has a known or temporary password",
            "Change or remove: " + ", ".join(known),
        ),
        item(
            "second_admin",
            c["admins"] >= 2,
            "A second administrator exists (to recover a lost account)",
            "Create a second administrator in Admin > Users.",
        ),
        item(
            "master_key",
            (s.secrets_key is not None and bool(s.secrets_key.get_secret_value()))
            or (s.secrets_key_file is not None and s.secrets_key_file.exists()),
            "The credential store master key is configured (back it up outside this machine)",
            "Run `erag init` or `erag secrets init --file ...` and set ERAG_SECRETS_KEY(_FILE).",
        ),
        item("production", s.is_prod, "Production mode (ERAG_ENV=prod)", "Set ERAG_ENV=prod (erag init does)."),
        item(
            "https",
            s.cookie_secure,
            "HTTPS with secure cookies (ERAG_COOKIE_SECURE=true)",
            "Put the application behind HTTPS and set ERAG_COOKIE_SECURE=true; until then keep it on localhost "
            "or a private network.",
        ),
        item(
            "demo",
            not demo,
            "No demo data (published passwords)",
            "Remove demo data from Admin > Setup assistant when you are done exploring; keep the instance on "
            "localhost or a private network meanwhile.",
        ),
        item(
            "cloud_ceiling",
            cap == Classification.public,
            f"Cloud ceiling: {cap.value} (confidential never leaves)",
            "Internal data may go to approved cloud providers; set the ceiling to public to keep it local.",
        ),
        item(
            "budgets",
            not unbudgeted,
            "Every cloud provider has an explicit monthly budget",
            "Set a monthly ceiling on: " + ", ".join(unbudgeted),
        ),
        item("router", True, f"Model router {'on' if router_cfg.enabled else 'off'}", ""),
    ]


def _host_only(url: str) -> str:
    """A provider address for the report, without any user:password part or query string."""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}{parts.path}"


async def report_markdown(db: AsyncSession, actor: User | None, settings: Settings | None = None) -> str:
    """The setup report for the company's IT records. Names, counts and booleans only: never a password, key,
    token or secret value (the tests assert it)."""
    from app.services.envcheck import run_checks

    s = settings or get_settings()
    row = await get_state(db)
    org = await get_organization(db, s)
    c = await counts(db)
    checks = await run_checks(db, s)
    checklist = await security_checklist(db, actor, s)
    roles = (await db.scalars(select(Role).order_by(Role.name))).all()
    containers = (await db.scalars(select(Container).order_by(Container.name))).all()
    providers = (await db.scalars(select(LlmProvider).order_by(LlmProvider.slug))).all()
    default = next((f"{p.slug}:{m.model}" for p in providers for m in p.models if m.is_default), "automatic")
    admins = (await db.scalars(select(User.email).where(User.is_admin.is_(True)).order_by(User.email))).all()
    lines = [
        f"# EnterpriseRAG setup report{(': ' + org['company_name']) if org['company_name'] else ''}",
        "",
        f"Generated {_now():%Y-%m-%d %H:%M} UTC. This report lists what was configured; it contains no passwords, "
        "keys, tokens or other secret values.",
        "",
        "## Installation",
        "",
        f"- Installation type: {(row.install_type if row else None) or 'empty'}",
        f"- Setup completed: {row.completed_at:%Y-%m-%d %H:%M} UTC ({row.completed_via})"
        if row and row.completed_at
        else "- Setup completed: not yet",
        f"- Mode: {'production' if s.is_prod else 'development'}; HTTPS cookies: {'yes' if s.cookie_secure else 'no'}",
        f"- License notice accepted: {row.license_accepted_at:%Y-%m-%d %H:%M} UTC by {row.license_accepted_by} "
        f"(text SHA-256 {row.license_text_sha256})"
        if row and row.license_accepted_at
        else "- License notice accepted: no",
        "",
        "## Environment checks",
        "",
    ]
    lines += [f"- [{ch['status']}] {ch['label']}" for ch in checks]
    lines += [
        "",
        "## Organization",
        "",
        f"- Company name: {org['company_name'] or '(not set)'}",
        f"- Cloud ceiling: {org['cloud_max_classification']} (confidential data never leaves)",
        "",
    ]
    lines += [
        "## People and access",
        "",
        f"- Administrators: {', '.join(admins)}",
        f"- Users: {c['users']}",
        f"- Roles: {', '.join(r.name for r in roles) or 'none'}",
        f"- Grants: {c['grants']}",
        "",
    ]
    lines += ["### Containers", ""]
    lines += [f"- {ct.name} ({ct.classification.value})" for ct in containers] or ["- none"]
    lines += [
        "",
        "## Knowledge",
        "",
        f"- Documents: {c['documents']} ({c['documents_ready']} ready, "
        f"{c['documents_pending']} pending, {c['documents_failed']} failed); connectors: {c['connectors']}",
        "",
    ]
    lines += ["## Models", "", f"- Default model: {default}"]
    for p in providers:
        lines.append(
            f"- Provider {p.slug} ({p.kind}, {p.locality}, {'enabled' if p.enabled else 'disabled'}): "
            f"{_host_only(p.base_url)}; models: {', '.join(m.model for m in p.models) or 'none'}"
        )
    lines += ["", "## Security checklist", ""]
    lines += [f"- [{'x' if i['status'] == 'ok' else ' '}] {i['label']}" for i in checklist]
    lines += [
        "",
        "Back up the credential store master key and the database separately (see the installation guide).",
        "",
    ]
    return "\n".join(lines)


CHECKLIST_MANUAL = {"master_key_backup", "review_router"}


async def checklist(db: AsyncSession, actor: User, settings: Settings | None = None) -> dict[str, Any]:
    """The post-setup checklist card (PLAN-v0.4 section 6)."""
    s = settings or get_settings()
    row = await get_state(db)
    dismissed = set((row.checklist_dismissed if row else None) or [])
    c = await counts(db)
    providers = (await db.scalars(select(LlmProvider))).all()
    cloud = [p for p in providers if p.enabled and p.locality != "local" and not (p.builtin and p.slug == "openrouter")]
    known = await known_password_admins(db, s)
    items = [
        ("mfa", "Enable MFA for your administrator account", actor.mfa_enabled, "account"),
        ("master_key_backup", "Back up the credential store master key outside this machine", False, ""),
        ("second_admin", "Add a second administrator", c["admins"] >= 2, "Users"),
        (
            "provider_budgets",
            "Set a monthly budget on every cloud provider",
            all(p.monthly_budget_usd is not None for p in cloud),
            "Providers",
        ),
        ("review_router", "Review the model router (off by default)", False, "Model router"),
        ("remove_demo", "Remove the demo data", not await demo_present(db), "Setup assistant"),
        ("known_password_admin", "No administrator with a known or temporary password", not known, "Users"),
    ]
    out = []
    for key, label, done, where in items:
        dismissable = key in CHECKLIST_MANUAL or key in {"provider_budgets", "second_admin", "mfa"}
        out.append(
            {
                "key": key,
                "label": label,
                "done": bool(done),
                "dismissed": key in dismissed and dismissable,
                "dismissable": dismissable,
                "where": where,
            }
        )
    visible = any(not i["done"] and not i["dismissed"] for i in out)
    return {"items": out, "visible": visible}


async def dismiss_checklist_item(db: AsyncSession, key: str, user_id: uuid.UUID) -> None:
    row = await get_state(db)
    if row is None:
        row = SetupState(id=1, steps={}, checklist_dismissed=[], completed_at=_now(), completed_via="upgrade")
        db.add(row)
    current = list(row.checklist_dismissed or [])
    if key not in current:
        row.checklist_dismissed = [*current, key]
    audit(db, "setup.checklist.dismiss", user_id=user_id, details={"item": key})
    await db.commit()


async def worker_heartbeat(db: AsyncSession) -> None:
    """Called by the worker every few seconds (read by the environment check)."""
    row = await db.get(AppSetting, "worker.heartbeat")
    now = _now()
    if row is None:
        db.add(AppSetting(key="worker.heartbeat", value={"at": now.isoformat()}))
    else:
        row.value = {"at": now.isoformat()}
    await db.commit()
