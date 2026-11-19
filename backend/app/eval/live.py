"""Company evaluation sets (R15): stored in the database, run against the live installation as real users.

A company set is classified (default `confidential`), its items come from reviewed feedback or an uploaded
YAML file, and its personas are real accounts. The leakage oracle here is the live policy
(`can_read_document` over each persona's effective permissions): unlike the repository oracle it is not
independent of the permission code, so these runs measure quality and catch `must_not_see` leaks; the
independent check stays with the `acme` set in CI.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.authz.policy import EffectivePermissions, can_read_document, effective_permissions
from app.eval.schema import EvalItem, EvalSet, EvalSetError
from app.models import Container, Document, EvaluationItem, EvaluationSet, User


class LiveOracle:
    def __init__(self, perms: dict[str, EffectivePermissions], docs: dict[str, tuple[uuid.UUID, uuid.UUID, bool]]):
        self.perms = perms
        self.docs = docs

    def can_read(self, email: str, doc_key: str) -> bool:
        p, d = self.perms.get(email), self.docs.get(doc_key)
        if p is None or d is None:
            return False
        doc_id, container_id, restricted = d
        return can_read_document(p, container_id=container_id, document_id=doc_id, restricted=restricted)


async def document_map(db: AsyncSession) -> dict[str, tuple[uuid.UUID, uuid.UUID, bool]]:
    rows = (
        await db.execute(
            select(Document.id, Document.container_id, Document.restricted, Container.slug, Document.filename).join(
                Container, Container.id == Document.container_id
            )
        )
    ).all()
    return {f"{slug}/{fn}": (doc_id, cid, restricted) for doc_id, cid, restricted, slug, fn in rows}


async def live_oracle(db: AsyncSession, emails: set[str]) -> LiveOracle:
    perms = {}
    for email in emails:
        user = await db.scalar(select(User).where(User.email == email, User.is_active.is_(True)))
        if user is not None:
            perms[email] = await effective_permissions(db, user)
    return LiveOracle(perms, await document_map(db))


async def validate_live(db: AsyncSession, items: list[EvalItem]) -> None:
    """Personas must be active users, documents must exist; expected documents must be readable by the persona
    and `must_not_see` documents must not be (otherwise the item itself is wrong)."""
    emails = {u for i in items for u in i.as_users}
    oracle = await live_oracle(db, emails)
    problems = []
    for item in items:
        for user, exp in item.expect.items():
            if user not in oracle.perms:
                problems.append(f"{item.id}: no active user {user}")
                continue
            for d in exp.documents + exp.must_not_see:
                if d not in oracle.docs:
                    problems.append(f"{item.id}: unknown document {d}")
            for d in exp.documents:
                if d in oracle.docs and not oracle.can_read(user, d):
                    problems.append(f"{item.id}: {user} cannot read expected document {d}")
            for d in exp.must_not_see:
                if d in oracle.docs and oracle.can_read(user, d):
                    problems.append(f"{item.id}: {user} can read must_not_see document {d}")
    if problems:
        raise EvalSetError("invalid evaluation set:\n  " + "\n  ".join(problems[:50]))


async def load_db_set(db: AsyncSession, row: EvaluationSet) -> EvalSet:
    rows = (await db.scalars(select(EvaluationItem).where(EvaluationItem.set_id == row.id))).all()
    items = [EvalItem.model_validate({"id": r.key, "question": r.question, **r.payload}) for r in rows]
    return EvalSet(name=row.name, items=items, digest=f"db:{len(items)}")


async def store_items(db: AsyncSession, set_row: EvaluationSet, items: list[EvalItem]) -> int:
    existing = {
        r.key: r for r in (await db.scalars(select(EvaluationItem).where(EvaluationItem.set_id == set_row.id))).all()
    }
    for item in items:
        payload: dict[str, Any] = item.model_dump(include={"as_users", "expect", "tags"})
        rec = existing.get(item.id)
        if rec is None:
            db.add(EvaluationItem(set_id=set_row.id, key=item.id, question=item.question, payload=payload))
        else:
            rec.question, rec.payload = item.question, payload
    await db.flush()
    return len(items)
