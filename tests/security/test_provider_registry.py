"""LLM provider registry (ADR 0023 to 0025): locality, clearance caps, SSRF, step-up, write-only keys, default.

Real Postgres and real local sockets. No model is called here: outgoing chat requests are either refused
by the destination guard (asserted by counting connections on a real listening socket) or recorded and
blocked by the egress hook (ADR 0008). The end-to-end tests with a real model are in tests/providers.
"""

from __future__ import annotations

import socket
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import login_limiter
from app.core.config import get_settings
from app.core.netguard import BlockedDestination
from app.llm import registry
from app.llm.providers.base import (
    EgressBlocked,
    EgressRecord,
    ProviderError,
    add_egress_hook,
    remove_egress_hook,
)
from app.llm.providers.openai_compat import OpenAICompatProvider
from app.llm.router import invalidate_router_cache, load_router
from app.models import AuditLog, Classification, LlmModel, LlmProvider, Secret, Session
from app.services import vault
from app.services.settings_store import OPENROUTER_KEY, put_setting
from tests.conftest import TABLES
from tests.factories import login, make_user

PLAINTEXT = "sk-test-PROVIDER-KEY-CANARY-77aa55"
QWEN = "qwen3:4b-instruct-2507-q4_K_M"


@pytest.fixture(autouse=True)
def _reset_limiter() -> None:
    login_limiter.reset()


class CountingSocket:
    """A real TCP listener that only counts connection attempts (it never answers like a model)."""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.accepted = 0
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except (TimeoutError, OSError):
                continue
            self.accepted += 1
            conn.close()

    def close(self) -> None:
        self._stop = True
        self._thread.join(timeout=2)
        self.sock.close()


@pytest.fixture
def listener() -> Iterator[CountingSocket]:
    s = CountingSocket()
    yield s
    s.close()


@pytest.fixture
def egress() -> Iterator[list[EgressRecord]]:
    records: list[EgressRecord] = []

    async def guard(rec: EgressRecord) -> None:
        records.append(rec)
        raise EgressBlocked("blocked by test egress guard")

    add_egress_hook(guard)
    yield records
    remove_egress_hook(guard)


async def _admin(db: AsyncSession, client: httpx.AsyncClient) -> uuid.UUID:
    u = await make_user(db, "admin@acme.test", admin=True)
    uid = u.id
    await db.commit()
    await login(client, "admin@acme.test")
    return uid


async def _age_reauth(db: AsyncSession, user_id: uuid.UUID) -> None:
    await db.execute(
        update(Session).where(Session.user_id == user_id).values(reauth_at=datetime.now(UTC) - timedelta(minutes=10))
    )
    await db.commit()


async def _dump(db: AsyncSession) -> str:
    parts = []
    for t in TABLES:
        parts.append(str(await db.scalar(text(f"SELECT coalesce(string_agg(t::text, ' '), '') FROM {t} t"))))
    return "\n".join(parts)


def _provider(listing: dict, slug: str) -> dict:
    return next(p for p in listing["providers"] if p["slug"] == slug)


# --- Destination policy (pure, real resolver) --------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "self_hosted", "url", "expected"),
    [
        # API kind, not self-hosted: public HTTPS only.
        ("api", False, "https://1.1.1.1/v1", "cloud"),
        ("api", False, "http://1.1.1.1/v1", "refused"),  # public over plain http
        ("api", False, "http://127.0.0.1:11434/v1", "refused"),
        ("api", False, "http://localhost:11434/v1", "refused"),
        ("api", False, "http://10.1.2.3/v1", "refused"),
        ("api", False, "http://[::1]:8000/v1", "refused"),
        ("api", False, "http://[::ffff:127.0.0.1]/v1", "refused"),
        ("api", False, "http://100.64.0.10/v1", "refused"),
        ("api", False, "ftp://1.1.1.1/v1", "refused"),
        ("api", False, "https://user:pw@1.1.1.1/v1", "refused"),
        # API kind, self-hosted: private allowed and then local.
        ("api", True, "http://127.0.0.1:11434/v1", "local"),
        ("api", True, "http://192.168.1.20:8000/v1", "local"),
        ("api", True, "https://1.1.1.1/v1", "cloud"),
        # Ollama: private -> local, public -> cloud (https only).
        ("ollama", False, "http://127.0.0.1:11434", "local"),
        ("ollama", False, "http://localhost:11434", "local"),
        ("ollama", False, "http://172.17.0.1:11434", "local"),
        ("ollama", False, "http://169.254.10.20:11434", "local"),  # link-local
        ("ollama", False, "http://100.100.1.1:11434", "local"),  # CGNAT (e.g. a tailnet)
        ("ollama", False, "https://8.8.8.8", "cloud"),
        ("ollama", False, "http://8.8.8.8:11434", "refused"),
        # Never, for any kind.
        ("ollama", False, "http://169.254.169.254", "refused"),  # cloud metadata endpoint
        ("ollama", False, "http://0.0.0.0:11434", "refused"),
        ("api", True, "http://224.0.0.1/v1", "refused"),
        ("api", True, "http://240.0.0.1/v1", "refused"),
    ],
)
async def test_locality_comes_from_the_address(kind: str, self_hosted: bool, url: str, expected: str) -> None:
    if expected == "refused":
        with pytest.raises(BlockedDestination):
            await registry.probe(url, kind, self_hosted)
    else:
        assert (await registry.probe(url, kind, self_hosted)).locality == expected


def test_clearance_is_capped_by_locality() -> None:
    s = get_settings()
    assert registry.effective_clearance(None, "local", s) == Classification.confidential
    assert registry.effective_clearance(None, "cloud", s) == Classification.internal
    assert registry.effective_clearance(Classification.confidential, "cloud", s) == Classification.internal
    assert registry.effective_clearance(Classification.public, "local", s) == Classification.public
    strict = s.model_copy(update={"cloud_max_classification": "public"})
    assert registry.effective_clearance(Classification.internal, "cloud", strict) == Classification.public
    loose = s.model_copy(update={"cloud_max_classification": "confidential"})
    assert registry.effective_clearance(Classification.confidential, "cloud", loose) == Classification.internal


# --- Request-time re-validation (real sockets) -----------------------------------------------------------


async def _drain(p: OpenAICompatProvider, cls: Classification = Classification.public) -> None:
    async for _ in p.stream_chat([{"role": "user", "content": "hi"}], classification=cls):
        pass


async def test_cloud_policy_provider_cannot_reach_a_private_socket(listener: CountingSocket) -> None:
    """A non-self-hosted API provider whose URL now points at loopback is refused before connecting."""
    p = OpenAICompatProvider(
        provider="acme-cloud",
        base_url=f"http://127.0.0.1:{listener.port}/v1",
        model="m",
        locality="cloud",
        max_classification=Classification.public,
        allow_private=False,
    )
    with pytest.raises(EgressBlocked, match="private address"):
        await _drain(p)
    assert listener.accepted == 0


async def test_local_provider_that_resolves_public_is_refused_at_request_time() -> None:
    """Cleared as local (confidential) when saved; if its address becomes public it is refused, not sent."""
    p = OpenAICompatProvider(
        provider="lan-ollama",
        base_url="http://1.1.1.1:11434/v1",
        model="m",
        locality="local",
        max_classification=Classification.confidential,
    )
    with pytest.raises(EgressBlocked, match="public address"):
        await _drain(p, Classification.confidential)


async def test_redirects_from_a_model_endpoint_are_not_followed(listener: CountingSocket) -> None:
    """A real local server answering 307 to another real socket: the redirect target is never contacted."""
    target = listener.port

    class Redirect(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.send_response(307)
            self.send_header("Location", f"http://127.0.0.1:{target}/v1/chat/completions")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        p = OpenAICompatProvider(
            provider="selfhosted",
            base_url=f"http://127.0.0.1:{server.server_address[1]}/v1",
            model="m",
            locality="local",
            max_classification=Classification.confidential,
        )
        with pytest.raises(ProviderError, match="HTTP 307"):
            await _drain(p)
    finally:
        server.shutdown()
    assert listener.accepted == 0


# --- Admin API ----------------------------------------------------------------------------------------


@pytest.mark.db
async def test_create_providers_with_locality_clearance_and_defaults(
    db: AsyncSession, client: httpx.AsyncClient
) -> None:
    await _admin(db, client)
    listing = (await client.get("/api/admin/providers")).json()
    assert {p["slug"] for p in listing["providers"]} == {"ollama", "openrouter"}  # built-ins bootstrapped

    r = await client.post(
        "/api/admin/providers",
        json={"name": "Acme Cloud", "kind": "api", "base_url": "https://1.1.1.1/v1", "models": ["acme/large"]},
    )
    assert r.status_code == 201, r.text
    cloud = r.json()
    assert cloud["slug"] == "acme-cloud" and cloud["locality"] == "cloud"
    assert cloud["requested_classification"] == "public" and cloud["max_classification"] == "public"
    assert cloud["metered"] and cloud["daily_request_limit"] == get_settings().provider_daily_request_cap
    assert cloud["monthly_budget_usd"] == get_settings().provider_monthly_budget_usd

    for bad in (
        {"max_classification": "confidential"},
        {"base_url": "http://127.0.0.1:9/v1"},  # private without self-hosted
        {"base_url": "http://1.1.1.1/v1"},  # public over http
        {"base_url": "http://169.254.169.254/v1", "self_hosted": True},
        {"monthly_budget_usd": 5.0},  # above ERAG_PROVIDER_MONTHLY_BUDGET_USD
        {"daily_request_limit": 100_000},  # above ERAG_PROVIDER_DAILY_REQUEST_CAP
        {"models": ["bad model id with spaces"]},
    ):
        body = {"name": f"x-{uuid.uuid4().hex[:6]}", "kind": "api", "base_url": "https://1.1.1.1/v1", **bad}
        assert (await client.post("/api/admin/providers", json=body)).status_code == 422, bad
    for slug in ("ollama", "openrouter", "claude-code"):
        body = {"name": "n", "slug": slug, "kind": "api", "base_url": "https://1.1.1.1/v1"}
        assert (await client.post("/api/admin/providers", json=body)).status_code == 422
    dup = {"name": "Acme Cloud", "kind": "api", "base_url": "https://1.1.1.1/v1"}
    assert (await client.post("/api/admin/providers", json=dup)).status_code == 409

    r = await client.post(
        "/api/admin/providers",
        json={"name": "Partner", "kind": "api", "base_url": "https://1.0.0.1/v1", "max_classification": "internal"},
    )
    assert r.status_code == 201 and r.json()["max_classification"] == "internal"

    # Ollama on a public address is a cloud provider; on loopback it is local and cleared for everything.
    r = await client.post(
        "/api/admin/providers", json={"name": "Hosted Ollama", "kind": "ollama", "base_url": "https://8.8.8.8"}
    )
    assert r.status_code == 201 and r.json()["locality"] == "cloud" and r.json()["max_classification"] == "public"
    bad = {"name": "H2", "kind": "ollama", "base_url": "https://8.8.4.4", "max_classification": "confidential"}
    assert (await client.post("/api/admin/providers", json=bad)).status_code == 422
    r = await client.post(
        "/api/admin/providers", json={"name": "LAN Ollama", "kind": "ollama", "base_url": "http://127.0.0.1:11434"}
    )
    lan = r.json()
    assert r.status_code == 201 and lan["locality"] == "local" and lan["max_classification"] == "confidential"
    assert not lan["metered"]  # a local Ollama costs nothing

    # The admin may lower a clearance, never raise it above the locality cap.
    assert (
        await client.patch(f"/api/admin/providers/{cloud['id']}", json={"max_classification": "confidential"})
    ).status_code == 422
    r = await client.patch(f"/api/admin/providers/{lan['id']}", json={"max_classification": "internal"})
    assert r.status_code == 200 and r.json()["max_classification"] == "internal"
    # Moving a local provider to a public address makes it cloud and resets its clearance to public.
    r = await client.patch(f"/api/admin/providers/{lan['id']}", json={"base_url": "https://8.8.8.8"})
    assert r.status_code == 200 and r.json()["locality"] == "cloud" and r.json()["max_classification"] == "public"

    actions = [a.action for a in (await db.scalars(select(AuditLog).order_by(AuditLog.id))).all()]
    assert actions.count("admin.provider.create") == 4 and "admin.provider.update" in actions


@pytest.mark.db
async def test_tampered_clearance_is_capped_at_build_time(db: AsyncSession, client: httpx.AsyncClient) -> None:
    """Even if the database says confidential, a cloud provider is built with at most the cloud cap."""
    await _admin(db, client)
    r = await client.post(
        "/api/admin/providers",
        json={"name": "Acme Cloud", "kind": "api", "base_url": "https://1.1.1.1/v1", "models": ["acme/large"]},
    )
    pid = r.json()["id"]
    await db.execute(
        update(LlmProvider).where(LlmProvider.id == uuid.UUID(pid)).values(max_classification="confidential")
    )
    await db.commit()
    invalidate_router_cache()
    p = (await load_router(db)).get("acme-cloud:acme/large")
    assert p is not None and p.locality == "cloud" and p.max_classification == Classification.internal
    invalidate_router_cache()
    strict = get_settings().model_copy(update={"cloud_max_classification": "public"})
    p = (await load_router(db, strict)).get("acme-cloud:acme/large")
    assert p is not None and p.max_classification == Classification.public

    # A row edited to point an API (not self-hosted) provider at loopback is dropped, not built.
    await db.execute(
        update(LlmProvider).where(LlmProvider.id == uuid.UUID(pid)).values(base_url="http://127.0.0.1:9/v1")
    )
    await db.commit()
    invalidate_router_cache()
    router = await load_router(db)
    assert router.get("acme-cloud:acme/large") is None
    assert router.states["acme-cloud"].available is False
    row = await db.get(LlmProvider, uuid.UUID(pid))
    assert row is not None
    with pytest.raises(BlockedDestination):
        await registry.discover_models(row, None)


@pytest.mark.db
async def test_sensitive_provider_actions_need_step_up(db: AsyncSession, client: httpx.AsyncClient) -> None:
    admin = await _admin(db, client)
    r = await client.post(
        "/api/admin/providers",
        json={"name": "Acme Cloud", "kind": "api", "base_url": "https://1.1.1.1/v1", "models": ["acme/large"]},
    )
    pid = r.json()["id"]
    await _age_reauth(db, admin)
    for resp in (
        await client.post("/api/admin/providers", json={"name": "B", "kind": "api", "base_url": "https://1.0.0.1/v1"}),
        await client.patch(f"/api/admin/providers/{pid}", json={"base_url": "https://1.0.0.1/v1"}),
        await client.patch(f"/api/admin/providers/{pid}", json={"api_key": "x" * 20}),
        await client.patch(f"/api/admin/providers/{pid}", json={"clear_api_key": True}),
        await client.patch(f"/api/admin/providers/{pid}", json={"max_classification": "internal"}),
        await client.patch(f"/api/admin/providers/{pid}", json={"self_hosted": True}),
        await client.delete(f"/api/admin/providers/{pid}"),
        await client.put("/api/admin/models/default", json={"model_id": "acme-cloud:acme/large"}),
        await client.put("/api/admin/models/default", json={"model_id": None}),
    ):
        assert resp.status_code == 403 and resp.json()["detail"] == "step_up_required", resp.text
    # Lowering limits, renaming and switching off are admin work without step-up.
    r = await client.patch(
        f"/api/admin/providers/{pid}", json={"name": "Acme", "enabled": False, "daily_request_limit": 5}
    )
    assert r.status_code == 200 and r.json()["daily_request_limit"] == 5


@pytest.mark.db
async def test_api_key_is_write_only_in_the_vault_and_audited(db: AsyncSession, client: httpx.AsyncClient) -> None:
    await _admin(db, client)
    r = await client.post(
        "/api/admin/providers",
        json={
            "name": "Self-hosted vLLM",
            "slug": "vllm",
            "kind": "api",
            "base_url": "http://127.0.0.1:11434/v1",
            "self_hosted": True,
            "api_key": PLAINTEXT,
            "models": [QWEN],
        },
    )
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["key_source"] == "vault" and created["secret_name"] == "provider.vllm.api_key"
    assert created["locality"] == "local" and created["metered"]  # an API endpoint may proxy a paid one
    listing = await client.get("/api/admin/providers")
    secrets = await client.get("/api/admin/secrets")
    for body in (r.text, listing.text, secrets.text):
        assert PLAINTEXT not in body
    assert (
        "provider: Self-hosted vLLM"
        in next(s for s in secrets.json() if s["name"] == "provider.vllm.api_key")["used_by"]
    )

    # The router gets the key from the vault (audited with the consumer), never from the database in clear.
    invalidate_router_cache()
    p = (await load_router(db)).get(f"vllm:{QWEN}")
    assert p is not None and getattr(p, "api_key", None) == PLAINTEXT
    uses = (await db.scalars(select(AuditLog).where(AuditLog.action == "secret.use"))).all()
    assert any(u.details.get("consumer") == "provider:vllm" for u in uses)
    dump = await _dump(db)
    assert PLAINTEXT not in dump and PLAINTEXT.encode().hex() not in dump

    # Rotating the key bumps the secret version and rebuilds the router without a restart.
    r = await client.patch(f"/api/admin/providers/{created['id']}", json={"api_key": PLAINTEXT + "-v2"})
    assert r.status_code == 200 and PLAINTEXT not in r.text
    p = (await load_router(db)).get(f"vllm:{QWEN}")
    assert p is not None and getattr(p, "api_key", None) == PLAINTEXT + "-v2"
    update_audit = (await db.scalars(select(AuditLog).where(AuditLog.action == "admin.provider.update"))).all()
    assert update_audit and update_audit[-1].details["api_key_changed"] is True
    assert PLAINTEXT not in str([a.details for a in update_audit])

    # Deleting the provider deletes the key it owned.
    assert (await client.delete(f"/api/admin/providers/{created['id']}")).status_code == 204
    db.expire_all()
    assert await db.scalar(select(Secret).where(Secret.name == "provider.vllm.api_key")) is None


@pytest.mark.db
async def test_default_model_respects_clearance_and_cannot_be_removed(
    db: AsyncSession, client: httpx.AsyncClient, egress: list[EgressRecord]
) -> None:
    await _admin(db, client)
    r = await client.post(
        "/api/admin/providers",
        json={
            "name": "Acme Cloud",
            "kind": "api",
            "base_url": "https://1.1.1.1/v1",
            "models": ["acme/large", "acme/small"],
            "max_classification": "internal",
        },
    )
    cloud = r.json()
    assert (await client.put("/api/admin/models/default", json={"model_id": "nope:x"})).status_code == 404
    r = await client.put("/api/admin/models/default", json={"model_id": "acme-cloud:acme/large"})
    assert r.status_code == 200
    models = (await client.get("/api/models")).json()  # hot reload: no restart
    assert [m["id"] for m in models if m["is_default"]] == ["acme-cloud:acme/large"]

    router = await load_router(db)
    assert router.default_id == "acme-cloud:acme/large"
    assert router.choose(Classification.public).model_id == "acme-cloud:acme/large"
    assert router.choose(Classification.internal).model_id == "acme-cloud:acme/large"
    # Confidential never goes to the default when it is not cleared: local model instead.
    assert router.choose(Classification.confidential).provider == "ollama"
    # A user's explicit choice still wins over the default when it is cleared.
    assert router.choose(Classification.public, "acme-cloud:acme/small").model_id == "acme-cloud:acme/small"

    # The provider-level guard refuses confidential even if handed the request directly.
    default = router.get("acme-cloud:acme/large")
    assert default is not None
    with pytest.raises(ProviderError):
        async for _ in default.stream_chat(
            [{"role": "user", "content": "x"}], classification=Classification.confidential
        ):
            pass
    assert egress == []

    # The default (or its provider) can be neither deleted nor disabled.
    large = next(m for m in cloud["models"] if m["model"] == "acme/large")
    base = f"/api/admin/providers/{cloud['id']}"
    assert (await client.delete(base)).status_code == 409
    assert (await client.patch(base, json={"enabled": False})).status_code == 409
    assert (await client.patch(f"{base}/models/{large['id']}", json={"enabled": False})).status_code == 409
    assert (await client.delete(f"{base}/models/{large['id']}")).status_code == 409
    small = next(m for m in cloud["models"] if m["model"] == "acme/small")
    assert (await client.patch(f"{base}/models/{small['id']}", json={"enabled": False})).status_code == 200
    assert (
        await client.put("/api/admin/models/default", json={"model_id": "acme-cloud:acme/small"})
    ).status_code == 409

    # Back to automatic (prefer local), then deletion works.
    assert (await client.put("/api/admin/models/default", json={"model_id": None})).status_code == 200
    assert (await load_router(db)).choose(Classification.public).provider == "ollama"
    assert (await client.delete(base)).status_code == 204
    audit = (await db.scalars(select(AuditLog).where(AuditLog.action == "admin.model.default"))).all()
    assert [a.details["new"] for a in audit] == ["acme-cloud:acme/large", None]


@pytest.mark.db
async def test_builtin_providers_bootstrap_idempotently_from_the_environment(
    db: AsyncSession, client: httpx.AsyncClient
) -> None:
    s = get_settings()
    # A pre-registry installation configured OpenRouter on the old Providers screen: it is imported once.
    await put_setting(
        db, OPENROUTER_KEY, {"model": "x/legacy:free", "enabled": False, "monthly_budget_usd": 0.004}, None
    )
    await db.commit()
    await registry.ensure_builtin_providers(db, s)
    await registry.ensure_builtin_providers(db, s)
    rows = (await db.scalars(select(LlmProvider).order_by(LlmProvider.slug))).all()
    assert [(r.slug, r.builtin) for r in rows] == [("ollama", True), ("openrouter", True)]
    ollama, openrouter = rows
    assert [(m.model, m.env_managed) for m in ollama.models] == [(s.local_chat_model, True)]
    assert ollama.base_url == s.ollama_base_url and ollama.kind == "ollama"
    assert [m.model for m in openrouter.models] == ["x/legacy:free"]
    assert openrouter.enabled is False and openrouter.monthly_budget_usd == 0.004
    assert openrouter.secret_name == vault.OPENROUTER_SECRET

    # The env model drives the built-in row (renamed in place, so the default marker would follow).
    other = s.model_copy(update={"local_chat_model": "qwen3:4b"})
    await registry.ensure_builtin_providers(db, other)
    db.expire_all()
    models = (await db.scalars(select(LlmModel).join(LlmProvider).where(LlmProvider.slug == "ollama"))).all()
    assert [m.model for m in models] == ["qwen3:4b"]
    await registry.ensure_builtin_providers(db, s)

    # Model ids keep the provider:model format, so stored messages stay valid.
    invalidate_router_cache()
    assert (await load_router(db, s)).get(f"ollama:{s.local_chat_model}") is not None

    # claude-code is env-gated and never stored.
    dev = s.model_copy(update={"claude_code_enabled": True})
    invalidate_router_cache()
    assert any(p.provider == "claude-code" for p in (await load_router(db, dev)).providers)
    assert await db.scalar(select(LlmProvider).where(LlmProvider.slug == "claude-code")) is None
    invalidate_router_cache()

    await _admin(db, client)
    listing = (await client.get("/api/admin/providers")).json()
    builtin = _provider(listing, "ollama")
    assert (await client.delete(f"/api/admin/providers/{builtin['id']}")).status_code == 409
    r = await client.patch(f"/api/admin/providers/{builtin['id']}", json={"base_url": "http://127.0.0.1:1"})
    assert r.status_code == 409
    env_model = builtin["models"][0]
    assert (await client.delete(f"/api/admin/providers/{builtin['id']}/models/{env_model['id']}")).status_code == 409
    # OpenRouter keeps the free-only rule for added models.
    orow = _provider(listing, "openrouter")
    r = await client.post(f"/api/admin/providers/{orow['id']}/models", json={"model": "openai/gpt-5"})
    assert r.status_code == 422
    assert orow["free_only"] is True and orow["daily_cap_env"] is None


@pytest.mark.db
async def test_openrouter_ceiling_still_only_lowers(db: AsyncSession, client: httpx.AsyncClient) -> None:
    await _admin(db, client)
    listing = (await client.get("/api/admin/providers")).json()
    orow = _provider(listing, "openrouter")
    cap = get_settings().openrouter_monthly_budget_usd
    assert orow["budget_cap_usd"] == cap
    r = await client.patch(f"/api/admin/providers/{orow['id']}", json={"monthly_budget_usd": cap * 10})
    assert r.status_code == 422
    r = await client.patch(f"/api/admin/providers/{orow['id']}", json={"monthly_budget_usd": cap / 2})
    assert r.status_code == 200 and r.json()["monthly_budget_usd"] == cap / 2
    # The legacy endpoint and the registry agree.
    assert (await client.get("/api/admin/providers/openrouter")).json()["monthly_budget_usd"] == cap / 2


async def test_discovery_reports_http_errors_and_never_follows_redirects(listener: CountingSocket) -> None:
    """Real local servers: a 401 is reported as such; a redirect towards another socket is not followed."""
    target = listener.port

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.startswith("/denied"):
                self.send_response(401)
            else:
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{target}/v1/models")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        denied = LlmProvider(slug="s1", kind="api", self_hosted=True, base_url=f"http://127.0.0.1:{port}/denied/v1")
        with pytest.raises(registry.ProviderConfigError, match="HTTP 401"):
            await registry.discover_models(denied, "k")
        moved = LlmProvider(slug="s2", kind="api", self_hosted=True, base_url=f"http://127.0.0.1:{port}/v1")
        result = await registry.test_connection(moved, None)
        assert result["ok"] is False and "HTTP 302" in result["error"]
    finally:
        server.shutdown()
    assert listener.accepted == 0
