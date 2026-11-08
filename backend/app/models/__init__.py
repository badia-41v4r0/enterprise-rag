"""SQLAlchemy models. See docs/PLAN.md section 5 for the data model."""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

EMBED_DIM = 1024  # bge-m3. Changing the embedding model means a new migration + reindex.


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> uuid.UUID:
    return uuid.uuid4()


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB().with_variant(JSON(), "sqlite")}


class Classification(enum.StrEnum):
    public = "public"
    internal = "internal"
    confidential = "confidential"

    @property
    def rank(self) -> int:
        return _CLASS_RANK[self]

    @classmethod
    def max(cls, *values: Classification | str | None) -> Classification:
        best = cls.public
        for v in values:
            if v is None:
                continue
            c = cls(v)
            if c.rank > best.rank:
                best = c
        return best


_CLASS_RANK = {Classification.public: 0, Classification.internal: 1, Classification.confidential: 2}


class ResourceType(enum.StrEnum):
    container = "container"
    document = "document"
    tool = "tool"


class Action(enum.StrEnum):
    read = "read"
    # Upload new documents and new versions of documents the user can read (ADR 0017).
    contribute = "contribute"
    manage = "manage"
    execute = "execute"


class VersionStatus(enum.StrEnum):
    pending = "pending"
    processing = "processing"
    ready = "ready"
    failed = "failed"
    superseded = "superseded"


class JobState(enum.StrEnum):
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"


def _enum(e: type[enum.Enum], name: str) -> Enum:
    return Enum(e, name=name, values_callable=lambda x: [m.value for m in x])


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now())


# --- Identity -----------------------------------------------------------------------------------


class User(TimestampMixin, Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(200), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # MFA (ADR 0019). The TOTP secret is stored encrypted (AAD = "mfa:<user id>"). While enrolling,
    # the secret is stored with mfa_enabled = False and only becomes active after a valid code.
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    mfa_secret_ct: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    mfa_secret_nonce: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    mfa_key_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Highest TOTP time step accepted so far: a code can never be used twice.
    mfa_last_step: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")

    roles: Mapped[list[Role]] = relationship(secondary="user_roles", lazy="selectin", back_populates="users")


class Session(TimestampMixin, Base):
    __tablename__ = "sessions"
    # Only the SHA-256 of the session token is stored; the raw token lives in the cookie.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Password accepted, second factor still missing: the session grants nothing but /mfa/verify.
    mfa_pending: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    mfa_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Last time the user proved who they are (login or step-up); sensitive actions need it recent.
    reauth_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RecoveryCode(Base):
    __tablename__ = "mfa_recovery_codes"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    code_hash: Mapped[str] = mapped_column(String(64))  # HMAC-SHA256, never the code
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Role(TimestampMixin, Base):
    __tablename__ = "roles"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")

    users: Mapped[list[User]] = relationship(secondary="user_roles", lazy="raise", back_populates="roles")


class UserRole(Base):
    __tablename__ = "user_roles"
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True)


# --- Resources ----------------------------------------------------------------------------------


class Container(TimestampMixin, Base):
    __tablename__ = "containers"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    classification: Mapped[Classification] = mapped_column(_enum(Classification, "classification"))


class Document(TimestampMixin, Base):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("container_id", "filename", name="uq_document_container_filename"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    container_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("containers.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(500))
    filename: Mapped[str] = mapped_column(String(500))
    # FK added with use_alter to avoid the documents <-> document_versions cycle at create time.
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document_versions.id", use_alter=True, ondelete="SET NULL"), nullable=True
    )
    restricted: Mapped[bool] = mapped_column(Boolean, default=False)


class DocumentVersion(TimestampMixin, Base):
    __tablename__ = "document_versions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    blob_key: Mapped[str] = mapped_column(String(200))
    mime_type: Mapped[str] = mapped_column(String(200), default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[VersionStatus] = mapped_column(_enum(VersionStatus, "version_status"), default=VersionStatus.pending)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)


class Chunk(Base):
    """A retrievable unit. Carries only location (container/document/version), never ACLs (ADR 0002)."""

    __tablename__ = "chunks"
    __table_args__ = (
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("document_versions.id", ondelete="CASCADE"), index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    container_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("containers.id", ondelete="CASCADE"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBED_DIM))
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", default=dict)


class Grant(TimestampMixin, Base):
    __tablename__ = "grants"
    __table_args__ = (
        UniqueConstraint("role_id", "resource_type", "resource_id", "action", name="uq_grant"),
        Index("ix_grants_resource", "resource_type", "resource_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    role_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("roles.id", ondelete="CASCADE"), index=True)
    resource_type: Mapped[ResourceType] = mapped_column(_enum(ResourceType, "resource_type"))
    resource_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    action: Mapped[Action] = mapped_column(_enum(Action, "grant_action"))


# --- Tools (MCP) --------------------------------------------------------------------------------


class ServerStatus(enum.StrEnum):
    pending = "pending"  # registered, never offered to a model
    approved = "approved"
    disabled = "disabled"


class McpServer(TimestampMixin, Base):
    __tablename__ = "mcp_servers"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    # For stdio servers: a python module run as `python -m <command>`; see ADR 0010.
    command: Mapped[str] = mapped_column(String(500))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # --- Registry of remote/isolated servers (R4/R5, ADR 0021) ---
    builtin: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    transport: Mapped[str] = mapped_column(String(20), default="stdio", server_default="stdio")  # stdio|http|runner
    url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    status: Mapped[ServerStatus] = mapped_column(
        _enum(ServerStatus, "mcp_server_status"), default=ServerStatus.approved, server_default="approved"
    )
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    auth_secret: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Clearance: highest classification that may be SENT to it (arguments), and the label put on
    # everything it RETURNS. Builtin servers are scoped by the signed context (ADR 0010).
    max_classification: Mapped[Classification] = mapped_column(
        _enum(Classification, "classification"), default=Classification.confidential, server_default="confidential"
    )
    result_classification: Mapped[Classification] = mapped_column(
        _enum(Classification, "classification"), default=Classification.public, server_default="public"
    )
    send_permission_context: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    limits: Mapped[dict[str, Any]] = mapped_column(default=dict)
    runtime: Mapped[dict[str, Any]] = mapped_column(default=dict)  # R5: image, resources, env, secrets, mounts
    last_discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)


class Tool(TimestampMixin, Base):
    __tablename__ = "tools"
    __table_args__ = (UniqueConstraint("server_id", "name", name="uq_tool_server_name"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    server_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("mcp_servers.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    input_schema: Mapped[dict[str, Any]] = mapped_column("schema", default=dict)
    classification: Mapped[Classification] = mapped_column(_enum(Classification, "classification"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Rug-pull defence (ADR 0021): hash of name + description + schema as discovered, and the hash the
    # admin approved. A mismatch at call time disables the tool and flags it for review.
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approved_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    arg_constraints: Mapped[dict[str, Any]] = mapped_column(default=dict)


# --- Chat ---------------------------------------------------------------------------------------


class Conversation(TimestampMixin, Base):
    __tablename__ = "conversations"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(300), default="New conversation")


class Message(TimestampMixin, Base):
    __tablename__ = "messages"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    citations: Mapped[dict[str, Any]] = mapped_column(default=dict)
    model_used: Mapped[str | None] = mapped_column(String(200), nullable=True)
    classification: Mapped[Classification] = mapped_column(
        _enum(Classification, "classification"), default=Classification.public
    )


# --- Jobs and audit -----------------------------------------------------------------------------


class IngestionJob(TimestampMixin, Base):
    __tablename__ = "ingestion_jobs"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("document_versions.id", ondelete="CASCADE"), index=True)
    state: Mapped[JobState] = mapped_column(_enum(JobState, "job_state"), default=JobState.queued, index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ProviderSpend(Base):
    """Money spent per cloud provider and calendar month (UTC), see ADR 0016."""

    __tablename__ = "provider_spend"
    provider: Mapped[str] = mapped_column(String(50), primary_key=True)
    month: Mapped[str] = mapped_column(String(7), primary_key=True)  # "YYYY-MM"
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    requests: Mapped[int] = mapped_column(Integer, default=0)


class ProviderDailyUsage(Base):
    """Requests sent per provider and UTC day: the request cap for providers that do not report cost (ADR 0024)."""

    __tablename__ = "provider_daily_usage"
    provider: Mapped[str] = mapped_column(String(50), primary_key=True)
    day: Mapped[str] = mapped_column(String(10), primary_key=True)  # "YYYY-MM-DD"
    requests: Mapped[int] = mapped_column(Integer, default=0)


class LlmUsage(Base):
    """Requests, tokens and cost per provider, model, user and UTC day (PLAN-v0.3 R6, ADR 0026).

    `user_id` NULL is usage with no user (for example a decider test). Only figures are stored, never text."""

    __tablename__ = "llm_usage"
    __table_args__ = (
        Index(
            "uq_llm_usage_key", "provider", "model", "user_id", "day", unique=True, postgresql_nulls_not_distinct=True
        ),
        Index("ix_llm_usage_user_month", "user_id", "month"),
        Index("ix_llm_usage_provider_month", "provider", "month"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(50))
    model: Mapped[str] = mapped_column(String(200))
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    day: Mapped[str] = mapped_column(String(10))
    month: Mapped[str] = mapped_column(String(7))
    requests: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    completion_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    estimated_requests: Mapped[int] = mapped_column(Integer, default=0)  # tokens estimated (chars / 4)


class Quota(Base):
    """A user's allowance (override) or a role's standard cap per member or collective pool (ADR 0027)."""

    __tablename__ = "quotas"
    __table_args__ = (
        Index(
            "uq_quota_scope",
            "subject",
            "user_id",
            "role_id",
            "kind",
            "provider",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    subject: Mapped[str] = mapped_column(String(10))  # "user" | "role"
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    role_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(12))  # "standard" | "collective"
    provider: Mapped[str | None] = mapped_column(String(40), nullable=True)  # NULL = every provider
    tokens_per_day: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    tokens_per_month: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    usd_per_day: Mapped[float | None] = mapped_column(Float, nullable=True)
    usd_per_month: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class QuotaPoolUsage(Base):
    """What a collective (role) pool has consumed per UTC day ("YYYY-MM-DD") and month ("YYYY-MM")."""

    __tablename__ = "quota_pool_usage"
    quota_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("quotas.id", ondelete="CASCADE"), primary_key=True)
    period: Mapped[str] = mapped_column(String(10), primary_key=True)
    tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)


# --- LLM provider registry (ADR 0023) ------------------------------------------------------------


class LlmProvider(TimestampMixin, Base):
    """A chat model endpoint: an Ollama instance or an OpenAI-compatible HTTP API.

    `slug` is the prefix of every model id (`<slug>:<model>`); the built-in `ollama` and `openrouter`
    rows are created from the environment on start-up. `locality` is derived from the address the
    URL resolves to (never chosen by the admin) and is re-checked on every request.
    """

    __tablename__ = "llm_providers"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    slug: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(20))  # "ollama" | "api"
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    base_url: Mapped[str] = mapped_column(String(1000))
    # API kind only: an admin-declared self-hosted endpoint, which may live on a private address.
    self_hosted: Mapped[bool] = mapped_column(Boolean, default=False)
    locality: Mapped[str] = mapped_column(String(10), default="cloud")  # "local" | "cloud", as of the last check
    # Requested clearance; NULL means "the most the locality allows". Capped at build and request time.
    max_classification: Mapped[Classification | None] = mapped_column(
        _enum(Classification, "classification"), nullable=True
    )
    secret_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    monthly_budget_usd: Mapped[float | None] = mapped_column(Float, nullable=True)  # NULL = the env ceiling
    daily_request_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)  # NULL = the env cap
    # Token limits (PLAN-v0.3 R6). NULL = the environment default (max output) or no limit (the others).
    max_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)  # sent as max_tokens
    max_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_per_day: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    tokens_per_month: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    max_turn_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)  # across the tool loop
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    models: Mapped[list[LlmModel]] = relationship(
        back_populates="provider", cascade="all, delete-orphan", order_by="LlmModel.model", lazy="selectin"
    )


class LlmModel(TimestampMixin, Base):
    __tablename__ = "llm_models"
    __table_args__ = (
        UniqueConstraint("provider_id", "model", name="uq_llm_model_provider_model"),
        Index("uq_llm_models_single_default", "is_default", unique=True, postgresql_where=text("is_default")),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    provider_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("llm_providers.id", ondelete="CASCADE"), index=True)
    model: Mapped[str] = mapped_column(String(200))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # The model named by ERAG_LOCAL_CHAT_MODEL / ERAG_OPENROUTER_MODEL for a built-in provider.
    env_managed: Mapped[bool] = mapped_column(Boolean, default=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    # Per-model overrides of the provider limits (NULL = the provider's value) and extra per-model caps.
    max_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_per_day: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    tokens_per_month: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    monthly_budget_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Price in USD per million tokens. NULL = unknown; "admin" | "openrouter" | "local" says where it came from.
    input_usd_per_mtok: Mapped[float | None] = mapped_column(Float, nullable=True)
    output_usd_per_mtok: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # The model router only picks tool-capable models when tools are offered (PLAN-v0.3 R7).
    supports_tools: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    provider: Mapped[LlmProvider] = relationship(back_populates="models", lazy="selectin")


# --- Model router (PLAN-v0.3 R7 to R9, ADR 0029 to 0032) ------------------------------------------


class RouterDecider(TimestampMixin, Base):
    """A configured decider: a prompted registry model, an open Jev-style server or the official Jev API.

    The rules decider is built in and always last; it is not stored. Locality is derived from the address
    (as for providers) and caps the clearance: a non-local decider is never cleared for confidential."""

    __tablename__ = "router_deciders"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    kind: Mapped[str] = mapped_column(String(20))  # "prompted" | "protocol" | "jev_official"
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    base_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    model: Mapped[str | None] = mapped_column(String(250), nullable=True)
    secret_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    self_hosted: Mapped[bool] = mapped_column(Boolean, default=False)
    locality: Mapped[str] = mapped_column(String(10), default="cloud")
    max_classification: Mapped[Classification | None] = mapped_column(
        _enum(Classification, "classification"), nullable=True
    )
    see_context: Mapped[bool] = mapped_column(Boolean, default=False)
    timeout_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    monthly_budget_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    daily_request_cap: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_usd_per_mtok: Mapped[float | None] = mapped_column(Float, nullable=True)
    output_usd_per_mtok: Mapped[float | None] = mapped_column(Float, nullable=True)
    options: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class RouterLane(TimestampMixin, Base):
    """Conditions on the decider's answers and an ordered list of target model ids (first permitted wins)."""

    __tablename__ = "router_lanes"
    __table_args__ = (
        Index("uq_router_lanes_single_default", "is_default", unique=True, postgresql_where=text("is_default")),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    conditions: Mapped[dict[str, Any]] = mapped_column(default=dict)
    targets: Mapped[list[str]] = mapped_column(JSONB, default=list)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class RouterDecision(Base):
    """One routing decision. Never the question nor any context (PLAN-v0.3 section 5)."""

    __tablename__ = "router_decisions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now(), index=True)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    classification: Mapped[Classification] = mapped_column(_enum(Classification, "classification"))
    tools_offered: Mapped[bool] = mapped_column(Boolean, default=False)
    permitted: Mapped[list[str]] = mapped_column(JSONB, default=list)
    decider: Mapped[str] = mapped_column(String(100))
    attempts: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    answers: Mapped[dict[str, Any]] = mapped_column(default=dict)
    lane: Mapped[str | None] = mapped_column(String(100), nullable=True)
    chosen_model: Mapped[str | None] = mapped_column(String(250), nullable=True)
    reason: Mapped[str] = mapped_column(String(500), default="")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)


class Secret(TimestampMixin, Base):
    """Encrypted credential (ADR 0018). The value is write-only through the API."""

    __tablename__ = "secrets"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    nonce: Mapped[bytes] = mapped_column(LargeBinary)
    key_id: Mapped[str] = mapped_column(String(16))
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_by: Mapped[str | None] = mapped_column(String(200), nullable=True)


class AppSetting(Base):
    """Runtime settings editable from the admin UI (e.g. the OpenRouter provider)."""

    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


# --- Connectors (R3, ADR 0020) --------------------------------------------------------------------


class Connector(TimestampMixin, Base):
    """An information source synced into ONE container; its documents inherit that container's access."""

    __tablename__ = "connectors"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    kind: Mapped[str] = mapped_column(String(50))
    container_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("containers.id", ondelete="CASCADE"), index=True)
    config: Mapped[dict[str, Any]] = mapped_column(default=dict)
    secret_refs: Mapped[dict[str, Any]] = mapped_column(default=dict)  # {"names": [...]} assigned secrets
    schedule_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(20), default="idle")
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    @property
    def secret_names(self) -> list[str]:
        return [str(n) for n in (self.secret_refs or {}).get("names", [])]


class ConnectorItem(Base):
    __tablename__ = "connector_items"
    __table_args__ = (UniqueConstraint("connector_id", "external_id", name="uq_connector_item"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    connector_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("connectors.id", ondelete="CASCADE"), index=True)
    external_id: Mapped[str] = mapped_column(String(2000))
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    version_hint: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SyncRun(TimestampMixin, Base):
    __tablename__ = "sync_runs"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    connector_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("connectors.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)  # queued|running|done|failed
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    triggered_by: Mapped[str] = mapped_column(String(100), default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    counts: Mapped[dict[str, Any]] = mapped_column(default=dict)
    errors: Mapped[dict[str, Any]] = mapped_column(default=dict)  # {"items": [{"item": ..., "error": ...}]}


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now(), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    resource: Mapped[str] = mapped_column(String(300), default="")
    decision: Mapped[str] = mapped_column(String(20), default="allow")
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
