"""Delegated content management (R1): managers and contributors cannot escalate.

Real Postgres, real HTTP API (ASGI), real files. No model involved: chunks for the content checks
are inserted directly so the read barrier can be tested without embeddings.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import login_limiter
from app.authz.policy import effective_permissions
from app.ingestion.worker import drain
from app.models import (
    Action,
    AuditLog,
    Chunk,
    Classification,
    Container,
    Document,
    DocumentVersion,
    Grant,
    IngestionJob,
    JobState,
    ResourceType,
    Role,
    User,
    UserRole,
    VersionStatus,
)
from app.rag.vector_store import PgVectorStore
from tests.factories import grant, login, make_container, make_role, make_user

pytestmark = pytest.mark.db

MD = b"# Leave policy\n\nEmployees get 25 days of paid leave."


@pytest.fixture(autouse=True)
def _reset_limiter() -> None:
    login_limiter.reset()


class World:
    hr: Container
    eng: Container
    manager_role: Role
    contributor_role: Role
    reader_role: Role
    secret_doc: Document


async def _world(db: AsyncSession) -> World:
    w = World()
    w.hr = await make_container(db, "HR", Classification.confidential)
    w.eng = await make_container(db, "Eng", Classification.internal)
    w.manager_role = await make_role(db, "hr-manager")
    w.contributor_role = await make_role(db, "hr-contributor")
    w.reader_role = await make_role(db, "hr-reader")
    await grant(db, w.manager_role, ResourceType.container, w.hr.id, Action.manage)
    await grant(db, w.contributor_role, ResourceType.container, w.hr.id, Action.contribute)
    await grant(db, w.reader_role, ResourceType.container, w.hr.id, Action.read)
    await make_user(db, "admin@acme.test", admin=True)
    await make_user(db, "mgr@acme.test", roles=[w.manager_role])
    await make_user(db, "contrib@acme.test", roles=[w.contributor_role])
    await make_user(db, "reader@acme.test", roles=[w.reader_role])
    # A restricted document nobody above holds a document grant for, with one chunk of content.
    w.secret_doc = Document(container_id=w.hr.id, title="investigation", filename="investigation.md", restricted=True)
    db.add(w.secret_doc)
    await db.flush()
    v = DocumentVersion(document_id=w.secret_doc.id, sha256="a" * 64, blob_key="a" * 64, status=VersionStatus.ready)
    db.add(v)
    await db.flush()
    w.secret_doc.current_version_id = v.id
    await PgVectorStore(db).upsert_chunks(
        [
            Chunk(
                version_id=v.id,
                document_id=w.secret_doc.id,
                container_id=w.hr.id,
                ordinal=0,
                text="CANARY-DELEGATION-SECRET",
                token_count=3,
                embedding=[0.0] * 1023 + [1.0],
                meta={},
            )
        ]
    )
    await db.commit()
    return w


async def _upload(client: httpx.AsyncClient, cid: uuid.UUID, name: str, data: bytes, **form: str) -> httpx.Response:
    return await client.post(
        f"/api/containers/{cid}/documents", files={"file": (name, data, "text/markdown")}, data=form
    )


async def _audit_actions(db: AsyncSession, prefix: str) -> list[AuditLog]:
    db.expire_all()
    return list((await db.scalars(select(AuditLog).where(AuditLog.action.startswith(prefix)))).all())


async def test_manager_manages_content_of_own_container_only(db: AsyncSession, client: httpx.AsyncClient) -> None:
    w = await _world(db)
    await login(client, "mgr@acme.test")

    containers = (await client.get("/api/containers")).json()
    assert [(c["name"], c["can_manage"], c["can_contribute"]) for c in containers] == [("HR", True, True)]
    assert (await client.get("/api/auth/me")).json()["can_manage_content"] is True

    r = await _upload(client, w.hr.id, "leave.md", MD)
    assert r.status_code == 201, r.text
    doc_id = r.json()["document_id"]
    # Replace: explicit new version keeps the stored filename.
    r = await client.post(f"/api/documents/{doc_id}/versions", files={"file": ("other-name.md", MD + b" v2")})
    assert r.status_code == 201, r.text
    detail = (await client.get(f"/api/documents/{doc_id}")).json()
    assert detail["filename"] == "leave.md" and detail["version_count"] == 2
    # Restricting is allowed. Without a document grant the manager can no longer read it, so lifting
    # the flag again is refused (it would grant access); the admin can lift it.
    assert (await client.patch(f"/api/documents/{doc_id}", json={"restricted": True})).status_code == 200
    assert (await client.patch(f"/api/documents/{doc_id}", json={"restricted": False})).status_code == 403
    assert (await client.delete(f"/api/documents/{doc_id}")).status_code == 204

    # Outside the container: same 404 as a missing container.
    assert (await _upload(client, w.eng.id, "x.md", MD)).status_code == 404
    assert (await client.get(f"/api/containers/{w.eng.id}/documents")).status_code == 404

    actions = [a.action for a in await _audit_actions(db, "document.")]
    assert actions.count("document.upload") == 2
    assert "document.update" in actions and "document.delete" in actions


async def test_manager_cannot_escalate(db: AsyncSession, client: httpx.AsyncClient) -> None:
    w = await _world(db)
    await login(client, "mgr@acme.test")
    # Grants, classification, roles, users: admin only.
    body = {"role_id": str(w.manager_role.id), "resource_type": "container", "resource_id": str(w.eng.id)}
    assert (await client.post("/api/admin/grants", json={**body, "action": "read"})).status_code == 403
    r = await client.patch(f"/api/admin/containers/{w.hr.id}", json={"classification": "public"})
    assert r.status_code == 403
    r = await client.put(f"/api/admin/roles/{w.manager_role.id}/containers/{w.eng.id}", json={"level": "manage"})
    assert r.status_code == 403
    assert (await client.get("/api/admin/users")).status_code == 403

    # Restricted document: metadata is visible to the manager, the content is not.
    listed = {d["id"]: d for d in (await client.get(f"/api/containers/{w.hr.id}/documents")).json()}
    assert listed[str(w.secret_doc.id)]["can_read"] is False
    chunk_id = await db.scalar(select(Chunk.id).where(Chunk.document_id == w.secret_doc.id))
    assert (await client.get(f"/api/chunks/{chunk_id}")).status_code == 404
    mgr = await db.scalar(select(User).where(User.email == "mgr@acme.test"))
    assert mgr is not None
    perms = await effective_permissions(db, mgr)
    assert w.secret_doc.id not in perms.readable_restricted_documents

    # Lifting the restricted flag would hand them (and every reader) the content: refused.
    r = await client.patch(f"/api/documents/{w.secret_doc.id}", json={"restricted": False})
    assert r.status_code == 403
    r = await _upload(client, w.hr.id, "investigation.md", b"overwrite", restricted="false")
    assert r.status_code == 403
    secret_id = w.secret_doc.id
    db.expire_all()
    assert await db.scalar(select(Document.restricted).where(Document.id == secret_id)) is True


async def test_contributor_uploads_but_cannot_delete_restrict_or_touch_restricted(
    db: AsyncSession, client: httpx.AsyncClient
) -> None:
    w = await _world(db)
    await login(client, "contrib@acme.test")
    containers = (await client.get("/api/containers")).json()
    assert [(c["can_contribute"], c["can_manage"]) for c in containers] == [(True, False)]

    r = await _upload(client, w.hr.id, "leave.md", MD)
    assert r.status_code == 201
    doc_id = r.json()["document_id"]
    assert (
        await client.post(f"/api/documents/{doc_id}/versions", files={"file": ("x.md", MD + b"!")})
    ).status_code == 201
    assert (await client.delete(f"/api/documents/{doc_id}")).status_code == 404
    assert (await client.patch(f"/api/documents/{doc_id}", json={"restricted": True})).status_code == 404
    assert (await _upload(client, w.hr.id, "new.md", b"# x", restricted="true")).status_code == 403

    # The restricted document is invisible, cannot be replaced, and dedupe does not confirm its content.
    listed = [d["id"] for d in (await client.get(f"/api/containers/{w.hr.id}/documents")).json()]
    assert str(w.secret_doc.id) not in listed
    r = await client.post(f"/api/documents/{w.secret_doc.id}/versions", files={"file": ("x.md", b"x")})
    assert r.status_code == 404
    assert (await _upload(client, w.hr.id, "investigation.md", b"guess")).status_code == 403


async def test_dedupe_is_not_an_oracle_for_restricted_documents(db: AsyncSession, client: httpx.AsyncClient) -> None:
    w = await _world(db)
    secret = b"# Case\n\nRestricted content."
    await login(client, "mgr@acme.test")
    r = await _upload(client, w.hr.id, "case.md", secret, restricted="true")
    assert r.status_code == 201
    restricted_id = r.json()["document_id"]

    await login(client, "contrib@acme.test")
    r = await _upload(client, w.hr.id, "guess.md", secret)
    assert r.status_code == 201
    assert r.json()["status"] == "queued" and r.json()["document_id"] != restricted_id


async def test_revoking_manage_is_immediate(db: AsyncSession, client: httpx.AsyncClient) -> None:
    w = await _world(db)
    admin = httpx.AsyncClient(transport=client._transport, base_url="http://testserver")
    await login(admin, "admin@acme.test")
    await login(client, "mgr@acme.test")
    assert (await _upload(client, w.hr.id, "a.md", MD)).status_code == 201

    g = await db.scalar(select(Grant).where(Grant.role_id == w.manager_role.id, Grant.action == Action.manage))
    assert g is not None
    assert (await admin.delete(f"/api/admin/grants/{g.id}")).status_code == 204
    assert (await _upload(client, w.hr.id, "b.md", MD + b"b")).status_code == 404
    assert (await client.get("/api/containers")).json() == []
    await admin.aclose()


async def test_role_lifecycle_templates_matrix_and_preview(db: AsyncSession, client: httpx.AsyncClient) -> None:
    w = await _world(db)
    await login(client, "admin@acme.test")
    # Create from a template, rename, clone.
    r = await client.post(
        "/api/admin/roles", json={"name": "eng-readers", "template": "reader", "container_ids": [str(w.eng.id)]}
    )
    assert r.status_code == 201 and r.json()["grant_count"] == 1
    rid = r.json()["id"]
    assert (await client.patch(f"/api/admin/roles/{rid}", json={"name": "engineering-readers"})).json()["name"] == (
        "engineering-readers"
    )
    clone = (await client.post(f"/api/admin/roles/{rid}/clone", json={"name": "eng-copy"})).json()
    assert clone["grant_count"] == 1 and clone["user_count"] == 0
    # Matrix cell: upgrade to contribute replaces (not adds) the grant; none removes it.
    r = await client.put(f"/api/admin/roles/{rid}/containers/{w.eng.id}", json={"level": "contribute"})
    assert r.json()["grant_count"] == 1
    actions = (await db.scalars(select(Grant.action).where(Grant.role_id == uuid.UUID(rid)))).all()
    assert list(actions) == [Action.contribute]
    r = await client.post(
        f"/api/admin/roles/{rid}/apply-template", json={"template": "manager", "container_ids": [str(w.hr.id)]}
    )
    assert r.json()["grant_count"] == 2

    # Bulk membership.
    users = {u["email"]: u["id"] for u in (await client.get("/api/admin/users")).json()}
    r = await client.post(f"/api/admin/roles/{rid}/members", json={"add": [users["reader@acme.test"]]})
    assert r.json()["user_count"] == 1

    # Effective-permissions preview uses the request-path function.
    eff = (await client.get(f"/api/admin/users/{users['reader@acme.test']}/effective-permissions")).json()
    levels = {c["name"]: c["level"] for c in eff["containers"]}
    assert levels == {"HR": "manage", "Eng": "contribute"}
    assert eff["restricted_documents"] == []

    # A role in use cannot be deleted without reassigning its users.
    assert (await client.delete(f"/api/admin/roles/{rid}")).status_code == 409
    r = await client.delete(f"/api/admin/roles/{rid}", params={"reassign_to": clone["id"]})
    assert r.status_code == 204
    reader_id = uuid.UUID(users["reader@acme.test"])
    db.expire_all()
    held = set((await db.scalars(select(UserRole.role_id).where(UserRole.user_id == reader_id))).all())
    assert uuid.UUID(clone["id"]) in held and uuid.UUID(rid) not in held

    logged = {a.action for a in await _audit_actions(db, "admin.")}
    assert {
        "admin.role.create",
        "admin.role.update",
        "admin.role.clone",
        "admin.grant.set_level",
        "admin.role.template",
        "admin.role.members",
        "admin.role.delete",
    } <= logged


async def test_failed_ingestion_can_be_retried(db: AsyncSession, client: httpx.AsyncClient) -> None:
    w = await _world(db)
    await login(client, "contrib@acme.test")
    r = await client.post(
        f"/api/containers/{w.hr.id}/documents", files={"file": ("broken.pdf", b"not a pdf", "application/pdf")}
    )
    assert r.status_code == 201
    doc_id = r.json()["document_id"]
    await drain()
    view = next(d for d in (await client.get(f"/api/containers/{w.hr.id}/documents")).json() if d["id"] == doc_id)
    assert view["latest_status"] == "failed" and view["latest_error"]

    r = await client.post(f"/api/documents/{doc_id}/retry")
    assert r.status_code == 200 and r.json()["latest_status"] == "pending"
    db.expire_all()
    queued = await db.scalar(
        select(func.count()).select_from(IngestionJob).where(IngestionJob.state == JobState.queued)
    )
    assert queued == 1
    assert (await client.post(f"/api/documents/{doc_id}/retry")).status_code == 409
    assert [a.action for a in await _audit_actions(db, "document.retry")] == ["document.retry"]
