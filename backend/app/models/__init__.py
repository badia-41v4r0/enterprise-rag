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


class McpServer(TimestampMixin, Base):
    __tablename__ = "mcp_servers"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    # For stdio servers: a python module run as `python -m <command>`; see ADR 0010.
    command: Mapped[str] = mapped_column(String(500))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


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


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now(), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    resource: Mapped[str] = mapped_column(String(300), default="")
    decision: Mapped[str] = mapped_column(String(20), default="allow")
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
