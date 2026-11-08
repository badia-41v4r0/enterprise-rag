"""Seed the fictional ACME Corp demo: containers, roles, personas, grants, documents (and tools).

Idempotent: re-running updates grants/flags and skips documents that are already ingested.
Run with `erag seed-acme` (or `python -m app.cli seed-acme`).
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_sessionmaker
from app.core.security import hash_password
from app.ingestion.blobstore import get_blobstore
from app.models import Action, Classification, Container, Document, Grant, ResourceType, Role, User, UserRole
from app.services.documents import upload_document

log = logging.getLogger(__name__)

DOCS_DIR = Path(__file__).resolve().parent / "docs"


@dataclass(frozen=True)
class ContainerSpec:
    slug: str
    name: str
    classification: Classification
    description: str
    canary: str
    restricted_docs: dict[str, str] = field(default_factory=dict)  # filename -> canary


CONTAINERS = [
    ContainerSpec(
        "general-handbook", "General Handbook", Classification.internal, "Policies for all employees", "CANARY-GEN-7Q2F"
    ),
    ContainerSpec(
        "hr-confidential",
        "HR Confidential",
        Classification.confidential,
        "Salaries, reviews and HR cases",
        "CANARY-HR-9XK3",
        restricted_docs={"investigation-2026-03.md": "CANARY-HRR-5MZ8"},
    ),
    ContainerSpec("finance", "Finance", Classification.confidential, "Budgets and payment policy", "CANARY-FIN-3TB6"),
    ContainerSpec(
        "engineering", "Engineering", Classification.internal, "Engineering practices and runbooks", "CANARY-ENG-8WD1"
    ),
    ContainerSpec(
        "product-docs", "Product Docs", Classification.public, "Public product documentation", "CANARY-PUB-2LC4"
    ),
]

# role -> container slug -> action
ROLES: dict[str, dict[str, Action]] = {
    "Employee": {"general-handbook": Action.read, "product-docs": Action.read},
    "HR": {"hr-confidential": Action.manage},
    "Finance": {"finance": Action.manage},
    "Engineering": {"engineering": Action.manage},
    "Executive": {c.slug: Action.read for c in CONTAINERS},
}
# role -> restricted document filenames it may read
ROLE_DOCUMENTS: dict[str, list[str]] = {"HR": ["investigation-2026-03.md"]}
# role -> tool names it may execute (registered in Phase 5; ignored if the tool does not exist)
ROLE_TOOLS: dict[str, list[str]] = {
    "Employee": ["files.list_files", "files.read_file"],
    "HR": ["tickets.search_tickets"],
    "Finance": ["tickets.search_tickets", "tickets.list_invoices"],
    "Engineering": ["tickets.search_tickets"],
    "Executive": ["tickets.search_tickets", "tickets.list_invoices"],
}


@dataclass(frozen=True)
class Persona:
    email: str
    name: str
    roles: list[str]
    is_admin: bool = False


PERSONAS = [
    Persona("admin@acme.test", "Ada Admin", ["Employee"], is_admin=True),
    Persona("hr@acme.test", "Hana HR Manager", ["Employee", "HR"]),
    Persona("finance@acme.test", "Felix Finance Analyst", ["Employee", "Finance"]),
    Persona("engineer@acme.test", "Elena Engineer", ["Employee", "Engineering"]),
    Persona("newhire@acme.test", "Nico New Hire", ["Employee"]),
    Persona("exec@acme.test", "Eva Executive", ["Employee", "Executive"]),
]


def readable_slugs(persona: Persona) -> set[str]:
    return {slug for r in persona.roles for slug in ROLES[r]}


def readable_restricted(persona: Persona) -> set[str]:
    return {f for r in persona.roles for f in ROLE_DOCUMENTS.get(r, [])}


async def _get_or_create_role(db: AsyncSession, name: str) -> Role:
    role = await db.scalar(select(Role).where(Role.name == name))
    if role is None:
        role = Role(name=name, description=f"ACME {name}")
        db.add(role)
        await db.flush()
    return role


async def seed_structure(db: AsyncSession, password: str) -> dict[str, Container]:
    containers: dict[str, Container] = {}
    for spec in CONTAINERS:
        c = await db.scalar(select(Container).where(Container.slug == spec.slug))
        if c is None:
            c = Container(
                slug=spec.slug, name=spec.name, classification=spec.classification, description=spec.description
            )
            db.add(c)
            await db.flush()
        c.classification = spec.classification
        containers[spec.slug] = c

    roles = {name: await _get_or_create_role(db, name) for name in ROLES}
    for name, role in roles.items():
        await db.execute(
            delete(Grant).where(
                Grant.role_id == role.id, Grant.resource_type.in_([ResourceType.container, ResourceType.document])
            )
        )
        for slug, action in ROLES[name].items():
            db.add(
                Grant(
                    role_id=role.id,
                    resource_type=ResourceType.container,
                    resource_id=containers[slug].id,
                    action=action,
                )
            )
    await db.flush()

    pw_hash = hash_password(password)
    for p in PERSONAS:
        u = await db.scalar(select(User).where(User.email == p.email))
        if u is None:
            u = User(email=p.email, display_name=p.name, password_hash=pw_hash, is_admin=p.is_admin)
            db.add(u)
            await db.flush()
        u.is_admin, u.is_active = p.is_admin, True
        await db.execute(delete(UserRole).where(UserRole.user_id == u.id))
        for r in p.roles:
            db.add(UserRole(user_id=u.id, role_id=roles[r].id))
    await db.commit()
    return containers


async def seed_documents(db: AsyncSession, containers: dict[str, Container]) -> int:
    """Upload every synthetic document. Returns the number of new versions queued."""
    blobs = get_blobstore()
    queued = 0
    for spec in CONTAINERS:
        for path in sorted((DOCS_DIR / spec.slug).glob("*.md")):
            res = await upload_document(
                db,
                blobs,
                container=containers[spec.slug],
                filename=path.name,
                data=path.read_bytes(),
                user_id=None,
                restricted=path.name in spec.restricted_docs,
            )
            queued += res.status == "queued"
    # Restricted-document grants (documents exist now).
    roles = {r.name: r for r in (await db.scalars(select(Role))).all()}
    for role_name, filenames in ROLE_DOCUMENTS.items():
        for fn in filenames:
            doc = await db.scalar(select(Document).where(Document.filename == fn))
            if doc is not None:
                db.add(
                    Grant(
                        role_id=roles[role_name].id,
                        resource_type=ResourceType.document,
                        resource_id=doc.id,
                        action=Action.read,
                    )
                )
    await db.commit()
    return queued


async def seed_tool_grants(db: AsyncSession) -> int:
    """Grant `execute` on registered tools per ROLE_TOOLS (tools are registered by the MCP registry)."""
    from app.models import McpServer, Tool

    rows = (
        await db.execute(select(Tool.id, McpServer.name, Tool.name).join(McpServer, McpServer.id == Tool.server_id))
    ).all()
    by_name = {f"{server}.{tool}": tid for tid, server, tool in rows}
    roles = {r.name: r for r in (await db.scalars(select(Role))).all()}
    n = 0
    for role_name, tools in ROLE_TOOLS.items():
        role = roles[role_name]
        await db.execute(delete(Grant).where(Grant.role_id == role.id, Grant.resource_type == ResourceType.tool))
        for t in tools:
            if t in by_name:
                db.add(
                    Grant(
                        role_id=role.id, resource_type=ResourceType.tool, resource_id=by_name[t], action=Action.execute
                    )
                )
                n += 1
    await db.commit()
    return n


async def seed(password: str, ingest: bool = True) -> None:
    from app.ingestion.worker import drain

    async with get_sessionmaker()() as db:
        containers = await seed_structure(db, password)
        queued = await seed_documents(db, containers)
        tools = 0
        try:
            registry = importlib.import_module("app.mcp.registry")  # MCP layer (Phase 5)
        except ImportError:
            registry = None
        if registry is not None:
            await registry.sync_registry(db)
            tools = await seed_tool_grants(db)
    processed = await drain() if ingest else 0
    print(
        f"ACME seeded: {len(containers)} containers, {len(PERSONAS)} personas, "
        f"{queued} documents queued, {processed} processed, {tools} tool grants."
    )
