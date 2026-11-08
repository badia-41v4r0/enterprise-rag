"""Helpers to build real rows in the test database (no mocks)."""

from __future__ import annotations

import uuid

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models import Action, Classification, Container, Document, Grant, ResourceType, Role, User, UserRole

PASSWORD = "correct-horse-battery"
_HASH = hash_password(PASSWORD)


async def make_user(db: AsyncSession, email: str, *, admin: bool = False, roles: list[Role] | None = None) -> User:
    u = User(email=email, display_name=email.split("@")[0], password_hash=_HASH, is_admin=admin)
    db.add(u)
    await db.flush()
    for r in roles or []:
        db.add(UserRole(user_id=u.id, role_id=r.id))
    await db.flush()
    return u


async def make_role(db: AsyncSession, name: str) -> Role:
    r = Role(name=name)
    db.add(r)
    await db.flush()
    return r


async def make_container(db: AsyncSession, name: str, classification: Classification) -> Container:
    c = Container(name=name, slug=name.lower().replace(" ", "-"), classification=classification)
    db.add(c)
    await db.flush()
    return c


async def make_document(db: AsyncSession, container: Container, title: str, *, restricted: bool = False) -> Document:
    d = Document(container_id=container.id, title=title, filename=f"{title}.md", restricted=restricted)
    db.add(d)
    await db.flush()
    return d


async def grant(
    db: AsyncSession, role: Role, resource_type: ResourceType, resource_id: uuid.UUID, action: Action = Action.read
) -> Grant:
    g = Grant(role_id=role.id, resource_type=resource_type, resource_id=resource_id, action=action)
    db.add(g)
    await db.flush()
    return g


async def login(client: httpx.AsyncClient, email: str, password: str = PASSWORD) -> str:
    """Log in and return the CSRF token (the session cookie is kept by the client)."""
    r = await client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    csrf: str = r.json()["csrf_token"]
    client.headers["X-CSRF-Token"] = csrf
    return csrf
