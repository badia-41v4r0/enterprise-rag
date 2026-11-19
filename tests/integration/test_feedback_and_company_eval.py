"""User feedback, the review queue and company evaluation sets (R15).

Admin is not data access (ADR 0009): a reviewer sees a proposed question only if they can read every source the
answer cited, and can only name documents they can read.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Action,
    AnswerFeedback,
    AuditLog,
    Classification,
    Conversation,
    EvaluationItem,
    EvaluationSet,
    Message,
    ResourceType,
)
from tests.factories import grant, login, make_container, make_document, make_role, make_user

pytestmark = pytest.mark.db


@pytest.fixture
async def world(db: AsyncSession) -> dict[str, object]:
    hr = await make_role(db, "hr")
    c_hr = await make_container(db, "HR", Classification.confidential)
    doc = await make_document(db, c_hr, "salary-bands")
    await grant(db, hr, ResourceType.container, c_hr.id, Action.read)
    asker = await make_user(db, "asker@x.test", roles=[hr])
    other = await make_user(db, "other@x.test", roles=[hr])
    blind_admin = await make_user(db, "admin@x.test", admin=True)
    hr_admin = await make_user(db, "hradmin@x.test", admin=True, roles=[hr])
    conv = Conversation(user_id=asker.id, title="t")
    db.add(conv)
    await db.flush()
    db.add(Message(conversation_id=conv.id, role="user", content="What is the senior band?"))
    await db.flush()
    answer = Message(
        conversation_id=conv.id,
        role="assistant",
        content="I could not find it.",
        citations={"items": [{"n": 1, "document_id": str(doc.id), "title": "salary-bands"}]},
        classification=Classification.confidential,
    )
    db.add(answer)
    await db.commit()
    return {
        "answer": answer,
        "doc_key": f"{c_hr.slug}/{doc.filename}",
        "asker": asker,
        "other": other,
        "blind_admin": blind_admin,
        "hr_admin": hr_admin,
    }


async def test_feedback_only_on_own_answers_and_never_audits_the_comment(
    db: AsyncSession, client: httpx.AsyncClient, world: dict[str, object]
) -> None:
    mid = str(world["answer"].id)  # type: ignore[attr-defined]
    await login(client, "other@x.test")
    r = await client.post("/api/chat/feedback", json={"message_id": mid, "rating": -1})
    assert r.status_code == 404  # someone else's answer
    await login(client, "asker@x.test")
    r = await client.post(
        "/api/chat/feedback", json={"message_id": mid, "rating": -1, "comment": "secret detail", "propose": True}
    )
    assert r.status_code == 200 and r.json()["state"] == "proposed"
    fb = await db.scalar(select(AnswerFeedback))
    assert fb is not None and fb.rating == -1 and fb.comment == "secret detail"
    entry = await db.scalar(select(AuditLog).where(AuditLog.action == "chat.feedback"))
    assert entry is not None and "secret detail" not in str(entry.details)
    assert (await client.post("/api/chat/feedback", json={"message_id": mid, "rating": 0})).status_code == 422


async def test_review_needs_read_access_to_every_source(
    db: AsyncSession, client: httpx.AsyncClient, world: dict[str, object]
) -> None:
    await login(client, "asker@x.test")
    mid = str(world["answer"].id)  # type: ignore[attr-defined]
    await client.post("/api/chat/feedback", json={"message_id": mid, "rating": -1, "propose": True})
    fb = await db.scalar(select(AnswerFeedback))
    assert fb is not None

    await login(client, "admin@x.test")  # an administrator without HR access
    queue = (await client.get("/api/admin/eval/feedback")).json()
    assert len(queue) == 1 and queue[0]["reviewable"] is False and "question" not in queue[0]
    r = await client.post(f"/api/admin/eval/feedback/{fb.id}/accept", json={"documents": []})
    assert r.status_code == 403

    await login(client, "hradmin@x.test")
    queue = (await client.get("/api/admin/eval/feedback")).json()
    assert queue[0]["reviewable"] is True and queue[0]["question"] == "What is the senior band?"
    assert queue[0]["cited_documents"] == [world["doc_key"]]
    r = await client.post(
        f"/api/admin/eval/feedback/{fb.id}/accept", json={"documents": [world["doc_key"]], "answerable": True}
    )
    assert r.status_code == 200, r.text
    item = await db.scalar(select(EvaluationItem))
    company = await db.scalar(select(EvaluationSet).where(EvaluationSet.name == "company"))
    assert company is not None and company.source == "db" and company.classification == Classification.confidential
    assert item is not None and item.payload["as_users"] == ["asker@x.test"]
    assert item.payload["expect"]["asker@x.test"]["documents"] == [world["doc_key"]]
    await db.refresh(fb)
    assert fb.review_state == "accepted" and fb.eval_item_id == item.id
    assert (await client.post(f"/api/admin/eval/feedback/{fb.id}/reject")).status_code == 404  # already reviewed


async def test_uploaded_company_items_are_validated_against_live_users_and_permissions(
    db: AsyncSession, client: httpx.AsyncClient, world: dict[str, object]
) -> None:
    await login(client, "hradmin@x.test")
    key = world["doc_key"]
    bad = (
        "items:\n"
        f"- {{id: q1, question: q, as_users: [admin@x.test], expect: {{admin@x.test: {{documents: [{key}]}}}}}}\n"
    )
    r = await client.post("/api/admin/eval/sets/team/items", json={"yaml_text": bad})
    assert r.status_code == 422 and "cannot read expected document" in r.text
    good = (
        "items:\n"
        f"- {{id: q1, question: q, as_users: [asker@x.test, admin@x.test],"
        f" expect: {{asker@x.test: {{documents: [{key}]}},"
        f" admin@x.test: {{answerable: false, must_not_see: [{key}]}}}}}}\n"
    )
    r = await client.post("/api/admin/eval/sets/team/items", json={"yaml_text": good})
    assert r.status_code == 200 and r.json()["items"] == 1
    sets = {s["name"]: s for s in (await client.get("/api/admin/eval/sets")).json()}
    assert sets["team"]["items"] == 1 and sets["team"]["source"] == "db"
    r = await client.post("/api/admin/eval/runs", json={"set": "acme"})
    assert r.status_code == 409  # the acme set runs against the evaluation database only
    r = await client.post("/api/admin/eval/runs", json={"set": "team", "overrides": {"mode": "magic"}})
    assert r.status_code == 422


@pytest.mark.embeddings
async def test_company_run_uses_live_permissions_and_flags_a_must_not_see_leak(
    db: AsyncSession, world: dict[str, object]
) -> None:
    """A real run with live embeddings: the reviewed item is answered for its asker; if the persona is later
    given access to a `must_not_see` document, the run reports the leak."""
    from app.api.evaluation import _execute
    from app.eval.live import store_items
    from app.eval.schema import EvalItem
    from app.ingestion.embedder import get_embedder
    from app.models import Chunk, Document, DocumentVersion, EvaluationRun, VersionStatus

    doc = await db.scalar(select(Document))
    assert doc is not None
    v = DocumentVersion(document_id=doc.id, sha256="0" * 64, blob_key="x", status=VersionStatus.ready)
    db.add(v)
    await db.flush()
    doc.current_version_id = v.id
    text = "Senior Engineer (E3) salary band: 58,000 to 72,000 EUR."
    db.add(
        Chunk(
            version_id=v.id,
            document_id=doc.id,
            container_id=doc.container_id,
            ordinal=0,
            text=text,
            token_count=12,
            embedding=(await get_embedder().embed([text]))[0],
            meta={},
        )
    )
    key = str(world["doc_key"])
    row = EvaluationSet(name="team", source="db")
    db.add(row)
    await db.flush()
    item = {
        "id": "q1",
        "question": "What is the salary band of a senior engineer?",
        "as_users": ["asker@x.test", "admin@x.test"],
        "expect": {
            "asker@x.test": {"documents": [key]},
            "admin@x.test": {"answerable": False, "must_not_see": [key]},
        },
    }
    await store_items(db, row, [EvalItem.model_validate(item)])
    await db.commit()

    await _execute("team", "first", {"reranker": "", "mode": "vector", "vector_min_score": 0.3}, world["asker"].id)  # type: ignore[attr-defined]
    run = await db.scalar(select(EvaluationRun).where(EvaluationRun.label == "first"))
    assert run is not None and run.status == "done", run
    assert run.metrics["recall@8"] == 1.0 and run.metrics["leaks"] == 0

    # The administrator is later given HR access: the item still says they must not see the document.
    from app.models import Role, UserRole

    hr = await db.scalar(select(Role).where(Role.name == "hr"))
    assert hr is not None
    db.add(UserRole(user_id=world["blind_admin"].id, role_id=hr.id))  # type: ignore[attr-defined]
    await db.commit()
    await _execute("team", "second", {"reranker": "", "mode": "vector", "vector_min_score": 0.3}, world["asker"].id)  # type: ignore[attr-defined]
    run2 = await db.scalar(select(EvaluationRun).where(EvaluationRun.label == "second"))
    assert run2 is not None and run2.metrics["leaks"] == 1
