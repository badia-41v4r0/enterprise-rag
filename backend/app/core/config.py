"""Application settings, loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="ERAG_", extra="ignore", enable_decoding=False)

    env: str = "dev"
    log_level: str = "INFO"
    log_json: bool = True
    # Observability (R19): Prometheus metrics on an internal port, never routed by the proxy (0 = off). The
    # worker uses metrics_port + 1. Traces go to an OTLP collector when the OpenTelemetry SDK is installed and
    # otel_endpoint is set; spans carry ids and sizes only, never text.
    metrics_host: str = "127.0.0.1"
    metrics_port: int = 9464
    otel_endpoint: str = ""
    otel_service_name: str = "enterprise-rag"

    # Database (asyncpg driver; psycopg is avoided because it is LGPL, see ADR 0004)
    database_url: str = "postgresql+asyncpg://erag:erag@localhost:55432/erag"
    # Connections (R18): pool per process; behind PgBouncer in transaction mode set db_pgbouncer=true (no
    # asyncpg statement cache, unique prepared statement names). LISTEN/NOTIFY needs a direct connection:
    # db_direct_url (default: database_url).
    db_pool_size: int = 10
    db_max_overflow: int = 10
    # Short side operations (rate limits, session activity, reservations, queue leases) use their own pool so
    # they never wait behind the requests that need them (R20).
    db_side_pool_size: int = 5
    db_side_max_overflow: int = 20
    db_pgbouncer: bool = False
    db_direct_url: str = ""
    # Evaluation and benchmark databases (ADR 0048). Default: the application's server with the database names
    # `erag_eval` / `erag_bench`. The eval and bench commands refuse to run against the application database.
    eval_database_url: str | None = None
    bench_database_url: str | None = None

    # Secrets. The defaults are only usable in dev; the app refuses to start with them in prod.
    secret_key: SecretStr = SecretStr("dev-insecure-secret-change-me")
    mcp_context_secret: SecretStr = SecretStr("dev-insecure-mcp-secret-change-me")
    # Master key of the credential store (ADR 0018): 32 random bytes, base64. Generate it with
    # `erag secrets init`. Either the value or a file holding it; required in prod. In dev, without
    # either, a key derived from ERAG_SECRET_KEY is used (insecure, for local demos only).
    secrets_key: SecretStr | None = None
    secrets_key_file: Path | None = None

    # MFA and step-up re-authentication (ADR 0019)
    mfa_issuer: str = "EnterpriseRAG"
    step_up_seconds: int = 300
    mfa_max_attempts: int = 5

    # Sessions
    session_ttl_hours: int = 12  # absolute lifetime
    session_idle_minutes: int = 120  # a session unused for this long ends; 0 = no idle timeout (R16)
    # Conversations with no activity for this many days are deleted (audited); 0 = keep forever (R16).
    conversation_retention_days: int = 0
    maintenance_interval_minutes: int = 15  # expired sessions and retention, in the app process
    # A chat request waits at most this long for a generation slot when a model pool is full (R17).
    generation_queue_timeout_seconds: float = 120.0
    cookie_secure: bool = False
    session_cookie_name: str = "erag_session"

    # Storage
    blob_dir: Path = REPO_ROOT / "data" / "blobs"
    # Blob store (R18): "local" (blob_dir) or "s3" (any S3-compatible server; see app.ingestion.s3).
    blob_backend: str = "local"
    s3_endpoint: str = ""
    s3_bucket: str = ""
    s3_region: str = "us-east-1"
    s3_prefix: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_sse: str = "AES256"
    max_upload_mb: int = 25

    # Ollama (embeddings always local; local chat model)
    ollama_base_url: str = "http://localhost:11434"
    embed_model: str = "bge-m3"
    embed_dim: int = 1024
    # Query embeddings first (R17): chat questions use their own endpoint when set, so a large ingestion never
    # slows down every chat. Bulk ingestion spreads batches over a pool (comma-separated URLs; default: the
    # Ollama URL) with several requests in flight per worker (R18).
    query_embed_url: str = ""
    ingest_embed_urls: str = ""
    ingest_embed_concurrency: int = 2
    embed_batch_size: int = 16
    # Non-thinking instruct variant: fast, no reasoning preamble (ADR 0007).
    local_chat_model: str = "qwen3:4b-instruct-2507-q4_K_M"
    # Set true only for a "thinking" model that emits reasoning inline before `</think>`.
    local_chat_strip_think: bool = False

    # OpenRouter (cloud). Disabled unless a key is present.
    openrouter_api_key: SecretStr | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "meta-llama/llama-3.3-8b-instruct:free"
    # Highest classification a cloud model may process (confidential is never allowed).
    cloud_max_classification: str = "internal"
    # Paid OpenRouter models are allowed (ADR 0028 supersedes the free-only rule of ADR 0016); the default
    # model stays a free one. Default monthly spend ceiling for OpenRouter, in USD (0.01 is a bit under 0.01
    # EUR); the worst case of each request is checked before sending and the real cost recorded after it.
    openrouter_monthly_budget_usd: float = 0.01

    # Providers added from the admin panel (ADR 0023/0024/0026). Every OpenAI-compatible API provider (and any
    # Ollama on a public address) is metered: a monthly spend ceiling in USD (reported cost, or tokens x price)
    # and a daily request cap. These are the initial defaults: an admin may lower or raise them per provider
    # (step-up, audited). OpenRouter's default ceiling is ERAG_OPENROUTER_MONTHLY_BUDGET_USD.
    provider_monthly_budget_usd: float = 0.01
    provider_daily_request_cap: int = 100
    # Default maximum output tokens per request (sent as `max_tokens`) when neither the provider nor the model
    # sets one; also the output side of the preventive worst-case cost check (ADR 0026).
    provider_max_output_tokens: int = 2048

    # Dev-only provider that shells out to `claude -p`. Never enabled in the demo/production.
    claude_code_enabled: bool = False
    claude_code_bin: str = "claude"

    # Local model files (reranker, tokenizer), installed only with `erag models install` (ADR 0047).
    models_dir: Path = REPO_ROOT / "data" / "models"
    # In-process reranker: concurrent rerank batches per process, ONNX Runtime threads (0 = its default),
    # how long a request may wait for a slot plus scoring before falling back to the fused order.
    reranker_concurrency: int = 2
    reranker_threads: int = 0
    reranker_timeout_seconds: float = 8.0
    reranker_use_cuda: bool = False

    # Retrieval / context
    retrieval_top_k: int = 8
    # Cosine similarity floor for bge-m3 (calibrated on the ACME corpus: relevant >= ~0.48, noise ~0.25-0.45).
    retrieval_min_score: float = 0.45
    context_token_budget: int = 2500
    history_max_messages: int = 6

    # MCP
    mcp_token_ttl_seconds: int = 60
    tool_max_iterations: int = 3
    tool_result_max_bytes: int = 16_000
    tool_rate_limit_per_minute: int = 20
    mcp_files_root: Path = REPO_ROOT / "demo" / "acme" / "docs"
    mcp_tickets_db: Path = REPO_ROOT / "data" / "tickets.sqlite3"

    # Connectors (ADR 0020). Folder connectors may only read below these roots (comma-separated or a
    # JSON list); mount them read-only into the app and the worker.
    connector_roots: list[Path] = Field(default_factory=list)
    connector_max_items: int = 1000
    connector_max_item_mb: int = 25
    connector_scheduler_seconds: float = 30.0
    # Outbound destinations that are not public addresses are refused unless listed here
    # (`host`, `host:port` or a CIDR), for web connectors and remote MCP servers alike.
    ssrf_allowlist: list[str] = Field(default_factory=list)

    @field_validator("secrets_key", "setup_token_sha256", mode="before")
    @classmethod
    def _empty_is_unset(cls, v: object) -> object:
        # Docker Compose passes unset variables as empty strings (e.g. ERAG_SECRETS_KEY: ${ERAG_SECRETS_KEY:-}).
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator(
        "connector_roots", "ssrf_allowlist", "allowed_hosts", "runner_images", "runner_mount_roots", mode="before"
    )
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip().startswith("["):
            return [x.strip() for x in v.split(",") if x.strip()]
        return v

    # Isolated MCP runner (ADR 0022). The app talks to the runner, never to the container engine.
    runner_url: str | None = None  # e.g. http://127.0.0.1:8790 ; unset = runner disabled
    runner_token: SecretStr | None = None  # shared secret between app and runner
    # Runner side: images it may start, pinned by image ID ("name=sha256:...", comma-separated).
    runner_images: list[str] = Field(default_factory=list)
    runner_socket_dir: Path = Path("/tmp/erag-runner-sockets")  # noqa: S108 - per-MCP subdirs, see ADR 0022
    runner_mount_roots: list[Path] = Field(default_factory=list)  # host paths MCPs may mount read-only
    runner_docker_socket: Path = Path("/var/run/docker.sock")
    runner_idle_minutes: int = 15
    runner_max_cpus: float = 2.0
    runner_max_memory_mb: int = 2048

    # Rate limiting (requests per minute)
    chat_rate_limit_per_minute: int = 20
    login_rate_limit_per_minute: int = 10

    # Frontend build to serve (same origin)
    frontend_dist: Path = REPO_ROOT / "frontend" / "dist"

    # Worker
    worker_poll_seconds: float = 1.0
    job_max_attempts: int = 3
    # R18: parallel jobs per worker process, the first retry delay (doubled each attempt, capped at an hour), the
    # heartbeat lease of a running job or sync, and the queue depth above which connectors stop enqueueing.
    worker_concurrency: int = 2
    job_retry_base_seconds: float = 30.0
    job_lease_seconds: float = 120.0
    ingest_queue_max: int = 5000
    # Memory cap per job: a blob larger than this is refused before it is read into memory.
    ingest_max_blob_mb: int = 200
    # How often the worker records that it is alive (read by the setup environment check).
    worker_heartbeat_seconds: float = 20.0

    # Guided first-run setup (PLAN-v0.4, ADR 0034). A fresh installation starts in setup mode; the one-time
    # setup token is printed by `erag init` (only its SHA-256 is kept, here) or generated at first start and
    # written to the log and to `setup_token_file` (default: next to the blob directory, mode 0600).
    setup_token_sha256: str | None = None
    setup_token_file: Path | None = None
    setup_token_ttl_hours: int = 24
    # Demo (ACME Corp). `seed_demo` seeds it at start-up for development and scripts/demo_scenarios.py (ADR 0040);
    # a real installation chooses "Demo with mock data" in the wizard instead.
    seed_demo: bool = False
    demo_password: SecretStr = SecretStr("acme-demo-password")
    # Ollama's model registry, asked for a model's size before the administrator confirms a download (ADR 0039).
    ollama_registry_url: str = "https://registry.ollama.ai"

    @property
    def setup_token_path(self) -> Path:
        return self.setup_token_file or self.blob_dir.parent / "setup-token"

    allowed_hosts: list[str] = Field(default_factory=lambda: ["*"])

    @property
    def eval_db_url(self) -> str:
        return self.eval_database_url or self.database_url.rsplit("/", 1)[0] + "/erag_eval"

    @property
    def bench_db_url(self) -> str:
        return self.bench_database_url or self.database_url.rsplit("/", 1)[0] + "/erag_bench"

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"

    def weak_secrets(self) -> list[str]:
        """Names of the environment secrets that still hold a development default (or are too short)."""
        weak = [
            f"ERAG_{name.upper()}"
            for name in ("secret_key", "mcp_context_secret")
            if _is_weak(getattr(self, name).get_secret_value())
        ]
        if self.runner_url and (self.runner_token is None or _is_weak(self.runner_token.get_secret_value())):
            weak.append("ERAG_RUNNER_TOKEN")
        return weak

    def check_secrets(self) -> None:
        """Refuse to run in production with the development secrets."""
        if not self.is_prod:
            return
        for name in self.weak_secrets():
            if name == "ERAG_RUNNER_TOKEN":
                raise RuntimeError("ERAG_RUNNER_TOKEN must be set to a strong value in prod when the runner is used")
            raise RuntimeError(f"{name} must be set to a strong value in prod")
        if (self.secrets_key is None or not self.secrets_key.get_secret_value()) and self.secrets_key_file is None:
            raise RuntimeError("ERAG_SECRETS_KEY or ERAG_SECRETS_KEY_FILE is required in prod (erag secrets init)")


def _is_weak(value: str) -> bool:
    return value.startswith("dev-insecure") or len(value) < 32


# Settings that no longer have any effect; a warning is logged once at start-up when one is still set.
REMOVED_SETTINGS = {
    "ERAG_OPENROUTER_ALLOW_PAID": "paid OpenRouter models are allowed; spend is bounded by the provider ceiling, "
    "the preventive worst-case check, the daily request cap and quotas (ADR 0028). Remove the variable.",
}


def warn_removed_settings() -> list[str]:
    import logging
    import os

    found = [name for name in REMOVED_SETTINGS if os.environ.get(name, "").strip()]
    for name in found:
        logging.getLogger("app.config").warning("%s is ignored: %s", name, REMOVED_SETTINGS[name])
    return found


@lru_cache
def get_settings() -> Settings:
    return Settings()
