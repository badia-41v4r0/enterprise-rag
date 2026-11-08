"""ACME Corp seeded once per module, with real bge-m3 embeddings."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_sessionmaker
from app.models import Container, Document, User
from demo.acme.seed import CONTAINERS, PERSONAS, Persona, seed
from tests.conftest import truncate_all

DEMO_PASSWORD = "acme-test-password"
CANARY_BY_SLUG = {c.slug: c.canary for c in CONTAINERS}
RESTRICTED_CANARIES = {fn: canary for c in CONTAINERS for fn, canary in c.restricted_docs.items()}


@pytest.fixture(scope="module")
async def acme() -> AsyncIterator[None]:
    await truncate_all()
    await seed(password=DEMO_PASSWORD, ingest=True)
    yield
    await truncate_all()


@pytest.fixture
async def session(acme: None) -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s:
        yield s
        await s.rollback()


async def user(db: AsyncSession, persona: Persona) -> User:
    u = await db.scalar(select(User).where(User.email == persona.email))
    assert u is not None
    return u


async def slug_maps(db: AsyncSession) -> tuple[dict[object, str], dict[object, str]]:
    """container id -> slug, document id -> filename"""
    containers = {c.id: c.slug for c in (await db.scalars(select(Container))).all()}
    docs = {d.id: d.filename for d in (await db.scalars(select(Document))).all()}
    return containers, docs


__all__ = ["CANARY_BY_SLUG", "DEMO_PASSWORD", "PERSONAS", "RESTRICTED_CANARIES", "slug_maps", "user"]
