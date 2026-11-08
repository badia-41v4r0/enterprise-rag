"""Connectors (R3) against real folders, a real local HTTP server and real Postgres.

Path traversal and symlink escapes, update/delete/idempotent re-sync, limits, dry run, SSRF
(loopback, private, link-local, redirects), secret assignment, and the permission boundary:
imported documents are visible only to roles that can read the target container.
"""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Iterator
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import login_limiter
from app.authz.policy import effective_permissions
from app.connectors.sync import process_next, run_sync
from app.core.config import Settings, get_settings
from app.core.db import get_sessionmaker
from app.core.netguard import BlockedDestination, check_url
from app.ingestion.embedder import OllamaEmbedder
from app.ingestion.worker import drain
from app.models import (
    Action,
    AuditLog,
    Classification,
    Connector,
    ConnectorItem,
    Container,
    Document,
    DocumentVersion,
    ResourceType,
    SyncRun,
    User,
)
from app.rag.retrieval import Retriever
from app.services import vault
from tests.factories import grant, login, make_container, make_role, make_user

pytestmark = pytest.mark.db
ROOT = Path(os.environ["ERAG_CONNECTOR_ROOTS"])


@pytest.fixture(autouse=True)
def _reset_limiter() -> None:
    login_limiter.reset()


@pytest.fixture
def folder(tmp_path: Path) -> Iterator[Path]:
    d = ROOT / f"src-{uuid.uuid4().hex[:8]}"
    d.mkdir()
    yield d
    for p in sorted(d.rglob("*"), reverse=True):
        p.unlink() if (p.is_file() or p.is_symlink()) else p.rmdir()
    d.rmdir()


class Site:
    """A real HTTP server on 127.0.0.1 serving a directory, recording requests and headers."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.hits: list[tuple[str, dict[str, str]]] = []
        site = self

        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *a: Any, **kw: Any) -> None:
                super().__init__(*a, directory=str(site.root), **kw)

            def log_message(self, *args: Any) -> None:
                return

            def do_GET(self) -> None:
                site.hits.append((self.path, dict(self.headers)))
                if self.path.startswith("/go-to/"):
                    self.send_response(302)
                    self.send_header("Location", self.path[len("/go-to/") :])
                    self.end_headers()
                    return
                super().do_GET()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def site(tmp_path: Path) -> Iterator[Site]:
    (tmp_path / "web").mkdir()
    s = Site(tmp_path / "web")
    yield s
    s.close()


async def _container(db: AsyncSession) -> uuid.UUID:
    c = await make_container(db, f"Wiki {uuid.uuid4().hex[:6]}", Classification.internal)
    cid = c.id
    await db.commit()
    return cid


async def _connector(
    db: AsyncSession, kind: str, container_id: uuid.UUID, config: dict[str, Any], **kw: Any
) -> Connector:
    c = Connector(name=f"{kind}-{uuid.uuid4().hex[:6]}", kind=kind, container_id=container_id, config=config, **kw)
    db.add(c)
    await db.commit()
    return c


async def _docs(db: AsyncSession, container_id: uuid.UUID) -> dict[str, int]:
    """filename -> number of versions"""
    rows = (
        await db.execute(
            select(Document.filename, func.count(DocumentVersion.id))
            .outerjoin(DocumentVersion, DocumentVersion.document_id == Document.id)
            .where(Document.container_id == container_id)
            .group_by(Document.filename)
        )
    ).all()
    return {f.split("/", 1)[1]: n for f, n in rows}


# --- folder ----------------------------------------------------------------------------------------


async def test_folder_must_resolve_inside_an_allowed_root(
    db: AsyncSession, client: httpx.AsyncClient, folder: Path, tmp_path: Path
) -> None:
    await make_user(db, "admin@acme.test", admin=True)
    cid = await _container(db)
    await login(client, "admin@acme.test")
    outside = tmp_path / "outside"
    outside.mkdir()
    (folder / "escape").symlink_to(outside, target_is_directory=True)
    body = {"kind": "folder", "container_id": str(cid)}
    for path in ("/etc", str(outside), f"{folder.name}/../../", f"{folder}/escape", "does-not-exist"):
        r = await client.post(
            "/api/admin/connectors", json={**body, "name": f"x{uuid.uuid4().hex[:4]}", "config": {"path": path}}
        )
        assert r.status_code == 422, (path, r.text)
    r = await client.post("/api/admin/connectors", json={**body, "name": "wiki", "config": {"path": folder.name}})
    assert r.status_code == 201, r.text
    assert r.json()["config"]["path"] == folder.name


async def test_folder_sync_skips_symlinks_and_handles_update_delete_and_resync(
    db: AsyncSession, folder: Path, tmp_path: Path
) -> None:
    cid = await _container(db)
    secret = tmp_path / "secret.md"
    secret.write_text("# Outside\n\nCANARY-OUTSIDE-ROOT")
    (tmp_path / "outdir").mkdir()
    (tmp_path / "outdir" / "x.md").write_text("# x\n\nCANARY-OUTSIDE-DIR")
    (folder / "a.md").write_text("# A\n\nAlpha.")
    (folder / "sub").mkdir()
    (folder / "sub" / "b.txt").write_text("Bravo.")
    (folder / "binary.exe").write_bytes(b"MZ")
    (folder / "big.md").write_bytes(b"#" * (1024 * 1024 + 10))
    (folder / "link.md").symlink_to(secret)
    (folder / "linkdir").symlink_to(tmp_path / "outdir", target_is_directory=True)
    settings = get_settings().model_copy(update={"connector_max_item_mb": 1})
    conn = await _connector(db, "folder", cid, {"path": str(folder)})

    report = await run_sync(db, conn, settings=settings)
    assert report.fatal is None
    assert report.counts["created"] == 2 and report.counts["skipped"] >= 4, report.counts
    reasons = {e["item"]: e["error"] for e in report.errors}
    assert "symlink" in reasons["link.md"] and "symlink" in reasons["linkdir"]
    assert "unsupported" in reasons["binary.exe"] and "MB" in reasons["big.md"]
    assert await _docs(db, cid) == {"a.md": 1, "sub/b.txt": 1}
    stored = " ".join(await db.scalars(select(Document.filename)))
    assert "secret" not in stored and "x.md" not in stored

    # Idempotent: nothing changes, nothing is fetched again.
    report = await run_sync(db, conn, settings=settings)
    assert report.counts["unchanged"] == 2 and report.counts["created"] == 0 and report.counts["updated"] == 0
    assert await _docs(db, cid) == {"a.md": 1, "sub/b.txt": 1}

    # Update one file, touch another without changing content, delete a third.
    (folder / "a.md").write_text("# A\n\nAlpha, second edition.")
    (folder / "sub" / "b.txt").unlink()
    (folder / "c.md").write_text("# C")
    report = await run_sync(db, conn, settings=settings)
    assert (report.counts["updated"], report.counts["created"], report.counts["deleted"]) == (1, 1, 1)
    assert await _docs(db, cid) == {"a.md": 2, "c.md": 1}
    items = set(await db.scalars(select(ConnectorItem.external_id).where(ConnectorItem.connector_id == conn.id)))
    assert items == {"a.md", "c.md"}


async def test_item_limit_and_dry_run_write_nothing(db: AsyncSession, folder: Path) -> None:
    cid = await _container(db)
    for i in range(5):
        (folder / f"doc{i}.md").write_text(f"# Doc {i}")
    conn = await _connector(db, "folder", cid, {"path": str(folder), "max_items": 3})
    report = await run_sync(db, conn, dry_run=True)
    assert report.counts["create"] == 3 and not report.complete
    assert [p["action"] for p in report.preview] == ["create"] * 3
    assert await _docs(db, cid) == {}
    report = await run_sync(db, conn)
    assert report.counts["created"] == 3 and len(await _docs(db, cid)) == 3
    # The listing was cut by the limit, so nothing is deleted on the basis of it.
    assert report.counts["deleted"] == 0


# --- web and SSRF ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://localhost:8088/",
        "http://10.1.2.3/",
        "http://192.168.1.1/",
        "http://172.16.0.5/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://0.0.0.0/",
        "http://100.64.0.1/",
        "file:///etc/passwd",
        "gopher://example.com/",
        "http://user:pw@93.184.215.14/",
    ],
)
async def test_ssrf_destinations_are_refused(url: str) -> None:
    with pytest.raises(BlockedDestination):
        await check_url(url, [])


async def test_ssrf_allowlist_is_exact() -> None:
    assert (await check_url("http://93.184.215.14/", [])).ip == "93.184.215.14"  # public literal: allowed
    assert (await check_url("http://127.0.0.1:9999/", ["127.0.0.1:9999"])).ip == "127.0.0.1"
    with pytest.raises(BlockedDestination):
        await check_url("http://127.0.0.1:9998/", ["127.0.0.1:9999"])
    assert (await check_url("http://10.9.8.7/", ["10.0.0.0/8"])).ip == "10.9.8.7"


async def test_web_connector_crawls_allowlisted_server_and_blocks_redirect_escape(db: AsyncSession, site: Site) -> None:
    other = Site(site.root)  # a second loopback service that is NOT allowlisted
    try:
        (site.root / "index.html").write_text(
            '<html><body><h1>Wiki</h1><p>Start page.</p><a href="page2.html">next</a>'
            f'<a href="https://example.com/x">external</a><a href="/go-to/{other.base}/secret.html">redir</a>'
            '<a href="notes.txt">notes</a></body></html>'
        )
        (site.root / "page2.html").write_text("<html><body><p>Second page.</p></body></html>")
        (site.root / "notes.txt").write_text("Plain notes.")
        (site.root / "secret.html").write_text("<p>CANARY-INTERNAL-SERVICE</p>")
        cid = await _container(db)
        conn = await _connector(db, "web", cid, {"urls": [f"{site.base}/index.html"], "max_depth": 1})

        # Without an allowlist, loopback is refused: nothing is imported.
        report = await run_sync(db, conn)
        assert report.counts["created"] == 0 and site.hits == []
        assert any("non-public" in e["error"] for e in report.errors)

        allow = get_settings().model_copy(update={"ssrf_allowlist": [f"127.0.0.1:{site.port}"]})
        report = await run_sync(db, conn, settings=allow)
        assert report.fatal is None
        names = set((await _docs(db, cid)).keys())
        assert len(names) == 3 and all("127-0-0-1" in n for n in names), names  # index, page2, notes
        # The redirect to the non-allowlisted service was re-checked and refused; it never got a request.
        assert other.hits == []
        assert any("go-to" in e["item"] and "non-public" in e["error"] for e in report.errors)

        report = await run_sync(db, conn, settings=allow)
        assert report.counts["unchanged"] == 3 and report.counts["created"] == 0
    finally:
        other.close()


async def test_connector_gets_only_assigned_secrets(db: AsyncSession, client: httpx.AsyncClient, site: Site) -> None:
    (site.root / "index.html").write_text("<p>Protected page.</p>")
    await make_user(db, "admin@acme.test", admin=True)
    cid = await _container(db)
    await vault.set_secret(db, "wiki_token", "token-123-abc", user_id=None)
    await db.commit()
    await login(client, "admin@acme.test")
    body = {
        "name": "wiki-web",
        "kind": "web",
        "container_id": str(cid),
        "config": {"urls": [f"{site.base}/index.html"], "max_depth": 0, "bearer_secret": "wiki_token"},
    }
    r = await client.post("/api/admin/connectors", json=body)
    assert r.status_code == 422 and "assign" in r.text
    r = await client.post("/api/admin/connectors", json={**body, "secret_names": ["wiki_token"]})
    assert r.status_code == 201, r.text
    conn = await db.get(Connector, uuid.UUID(r.json()["id"]))
    assert conn is not None
    allow = get_settings().model_copy(update={"ssrf_allowlist": [f"127.0.0.1:{site.port}"]})
    report = await run_sync(db, conn, settings=allow)
    assert report.counts["created"] == 1, report.errors
    assert site.hits[0][1].get("Authorization") == "Bearer token-123-abc"
    uses = (await db.scalars(select(AuditLog).where(AuditLog.action == "secret.use"))).all()
    assert [u.details["consumer"] for u in uses] == [f"connector:{conn.id}"]
    secrets = (await client.get("/api/admin/secrets")).json()
    assert secrets[0]["used_by"] == ["connector: wiki-web"]

    # A config that names a secret which is not assigned is refused at sync time too.
    conn.config = {**conn.config, "bearer_secret": "wiki_token"}
    conn.secret_refs = {"names": []}
    await db.commit()
    report = await run_sync(db, conn, settings=allow)
    assert report.fatal and "not assigned" in report.fatal


# --- API flow and the permission boundary ------------------------------------------------------------


@pytest.mark.embeddings
async def test_imported_documents_answer_only_for_readers_of_the_container(
    db: AsyncSession, client: httpx.AsyncClient, folder: Path
) -> None:
    (folder / "vpn.md").write_text(
        "# VPN setup\n\nConnect to vpn.acme.test with the WireGuard profile CANARY-WIKI-7RQ2."
    )
    readers, others = await make_role(db, "eng"), await make_role(db, "sales")
    c = await make_container(db, "Eng Wiki", Classification.internal)
    other = await make_container(db, "Sales", Classification.internal)
    await grant(db, readers, ResourceType.container, c.id, Action.read)
    await grant(db, others, ResourceType.container, other.id, Action.read)
    await make_user(db, "admin@acme.test", admin=True)
    await make_user(db, "eng@acme.test", roles=[readers])
    await make_user(db, "sales@acme.test", roles=[others])
    cid = c.id
    await db.commit()

    await login(client, "admin@acme.test")
    r = await client.post(
        "/api/admin/connectors",
        json={"name": "eng-wiki", "kind": "folder", "container_id": str(cid), "config": {"path": folder.name}},
    )
    assert r.status_code == 201
    conn_id = r.json()["id"]
    assert (await client.post(f"/api/admin/connectors/{conn_id}/test")).json()["ok"] is True
    preview = (await client.post(f"/api/admin/connectors/{conn_id}/preview")).json()
    assert (
        preview["items"] == [{"item": "vpn.md", "action": "create"}]
        and "Everyone with read access" in preview["warning"]
    )
    run = (await client.post(f"/api/admin/connectors/{conn_id}/sync")).json()
    assert run["status"] == "queued"
    assert await process_next(get_sessionmaker()) == uuid.UUID(run["id"])
    assert await drain() == 1  # the imported document goes through normal ingestion (real bge-m3)
    runs = (await client.get(f"/api/admin/connectors/{conn_id}/runs")).json()
    assert runs[0]["status"] == "done" and runs[0]["counts"]["created"] == 1

    retriever = Retriever(db, OllamaEmbedder())
    for email, expected in (("eng@acme.test", True), ("sales@acme.test", False)):
        u = await db.scalar(select(User).where(User.email == email))
        assert u is not None
        res = await retriever.retrieve(await effective_permissions(db, u), "How do I set up the VPN?", 8, 0.3)
        found = any("CANARY-WIKI-7RQ2" in h.text for h in res.hits)
        assert found is expected, email
        assert all(h.container_id != cid for h in res.hits) or expected

    actions = {a.action for a in (await db.scalars(select(AuditLog))).all()}
    assert {"admin.connector.create", "admin.connector.sync", "connector.sync"} <= actions
    sr = await db.scalar(select(SyncRun).where(SyncRun.connector_id == uuid.UUID(conn_id)))
    assert sr is not None and sr.triggered_by.startswith("user:")
    assert await db.scalar(select(Container.id).where(Container.id == cid)) == cid


def test_settings_parse_comma_separated_lists() -> None:
    s = Settings(connector_roots="/a, /b", ssrf_allowlist="127.0.0.1:1,10.0.0.0/8")  # type: ignore[arg-type]
    assert [str(p) for p in s.connector_roots] == ["/a", "/b"] and s.ssrf_allowlist == ["127.0.0.1:1", "10.0.0.0/8"]
