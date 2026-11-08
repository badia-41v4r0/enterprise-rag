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

    # Database (asyncpg driver; psycopg is avoided because it is LGPL, see ADR 0004)
    database_url: str = "postgresql+asyncpg://erag:erag@localhost:55432/erag"

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
    session_ttl_hours: int = 12
    cookie_secure: bool = False
    session_cookie_name: str = "erag_session"

    # Storage
    blob_dir: Path = REPO_ROOT / "data" / "blobs"
    max_upload_mb: int = 25

    # Ollama (embeddings always local; local chat model)
    ollama_base_url: str = "http://localhost:11434"
    embed_model: str = "bge-m3"
    embed_dim: int = 1024
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
    # Only models whose id ends in ":free" are accepted unless this is set (ADR 0016).
    openrouter_allow_paid: bool = False
    # Hard monthly spend ceiling for OpenRouter, in USD. 0.01 is a bit under 0.01 EUR at current rates;
    # the cap is checked before every request and the real cost is recorded after it (ADR 0016).
    openrouter_monthly_budget_usd: float = 0.01

    # Dev-only provider that shells out to `claude -p`. Never enabled in the demo/production.
    claude_code_enabled: bool = False
    claude_code_bin: str = "claude"

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

    @field_validator("connector_roots", "ssrf_allowlist", "allowed_hosts", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip().startswith("["):
            return [x.strip() for x in v.split(",") if x.strip()]
        return v

    # Rate limiting (requests per minute)
    chat_rate_limit_per_minute: int = 20
    login_rate_limit_per_minute: int = 10

    # Frontend build to serve (same origin)
    frontend_dist: Path = REPO_ROOT / "frontend" / "dist"

    # Worker
    worker_poll_seconds: float = 1.0
    job_max_attempts: int = 3

    allowed_hosts: list[str] = Field(default_factory=lambda: ["*"])

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"

    def check_secrets(self) -> None:
        """Refuse to run in production with the development secrets."""
        if not self.is_prod:
            return
        for name in ("secret_key", "mcp_context_secret"):
            value = getattr(self, name).get_secret_value()
            if value.startswith("dev-insecure") or len(value) < 32:
                raise RuntimeError(f"ERAG_{name.upper()} must be set to a strong value in prod")
        if self.secrets_key is None and self.secrets_key_file is None:
            raise RuntimeError("ERAG_SECRETS_KEY or ERAG_SECRETS_KEY_FILE is required in prod (erag secrets init)")


@lru_cache
def get_settings() -> Settings:
    return Settings()
