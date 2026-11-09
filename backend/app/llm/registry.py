"""LLM provider registry (ADR 0023): providers and models stored in the database.

- Two kinds: `ollama` (an Ollama instance; chat through its OpenAI-compatible `/v1`) and `api` (any
  OpenAI-compatible HTTP endpoint). Native non-OpenAI APIs are out of scope.
- Locality is never chosen by the admin: it follows from the addresses the URL resolves to
  (`netguard.check_provider_url`). Only loopback/private/link-local addresses make a provider local.
- Clearance is capped by locality: local up to confidential, cloud at most
  min(internal, ERAG_CLOUD_MAX_CLASSIFICATION). The admin may lower it, never raise it.
- The built-in `ollama` and `openrouter` providers are created from the environment (idempotent) and
  keep the environment as their source for URL and model.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.netguard import (
    BlockedDestination,
    PinnedTransport,
    ProviderEndpoint,
    UnresolvableHost,
    check_provider_url,
    provider_checker,
)
from app.llm.providers.base import LLMProvider
from app.llm.providers.openai_compat import OpenAICompatProvider
from app.models import AppSetting, Classification, LlmModel, LlmProvider
from app.services.usage import ModelLimits

log = logging.getLogger(__name__)

KINDS = ("ollama", "api")
OLLAMA = "ollama"
OPENROUTER = "openrouter"
BUILTIN_SLUGS = {OLLAMA, OPENROUTER}
RESERVED_SLUGS = BUILTIN_SLUGS | {"claude-code", "default", "automatic"}
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$")
OPENROUTER_SECRET = "openrouter_api_key"  # noqa: S105 - the secret's name (ADR 0018)
OPENROUTER_SETTING = "provider.openrouter"  # legacy app_settings row (ADR 0018), still honoured


class ProviderConfigError(ValueError):
    pass


def secret_name_for(slug: str) -> str:
    return f"provider.{slug}.api_key"


def validate_slug(slug: str) -> str:
    if not SLUG_RE.match(slug):
        raise ProviderConfigError("slug: 2-40 lowercase letters, digits or '-', starting with a letter or digit")
    if slug in RESERVED_SLUGS:
        raise ProviderConfigError(f"slug {slug!r} is reserved")
    return slug


def validate_model_name(model: str) -> str:
    if not MODEL_RE.match(model):
        raise ProviderConfigError(f"invalid model id {model!r}")
    return model


# --- Policy ---------------------------------------------------------------------------------------


def cloud_cap(s: Settings) -> Classification:
    """Highest classification any cloud provider may receive (never confidential)."""
    env = Classification(s.cloud_max_classification)
    return env if env.rank < Classification.confidential.rank else Classification.internal


def locality_cap(locality: str, s: Settings) -> Classification:
    return Classification.confidential if locality == "local" else cloud_cap(s)


def effective_clearance(requested: Classification | None, locality: str, s: Settings) -> Classification:
    cap = locality_cap(locality, s)
    if requested is None or requested.rank > cap.rank:
        return cap
    return requested


def allows_private(kind: str, self_hosted: bool) -> bool:
    """Private/loopback addresses only for Ollama or explicitly self-hosted API providers."""
    return kind == "ollama" or self_hosted


def chat_base(kind: str, base_url: str) -> str:
    base = base_url.rstrip("/")
    return f"{base}/v1" if kind == "ollama" else base


def is_openrouter(row: LlmProvider) -> bool:
    return row.builtin and row.slug == OPENROUTER


def metered(row: LlmProvider, locality: str) -> bool:
    """Every API provider (a 'self-hosted' one may be a proxy to a paid API) and any non-local Ollama."""
    return row.kind == "api" or locality != "local"


def budget_cap(row: LlmProvider, s: Settings) -> float:
    """The environment default for the monthly ceiling (an initial default, not a hard ceiling: ADR 0026)."""
    return s.openrouter_monthly_budget_usd if is_openrouter(row) else s.provider_monthly_budget_usd


def effective_budget(row: LlmProvider, s: Settings) -> float:
    """The admin's value when set (it may be lower or higher than the environment default), else the default."""
    value = budget_cap(row, s) if row.monthly_budget_usd is None else row.monthly_budget_usd
    return max(0.0, value)


def daily_cap_env(row: LlmProvider, s: Settings) -> int | None:
    """Default daily request cap, for every metered provider including OpenRouter since paid models are
    allowed there (ADR 0028)."""
    return s.provider_daily_request_cap


def effective_daily_cap(row: LlmProvider, s: Settings) -> int | None:
    return daily_cap_env(row, s) if row.daily_request_limit is None else max(0, row.daily_request_limit)


def model_limits(row: LlmProvider, m: LlmModel, locality: str, s: Settings) -> ModelLimits:
    """Effective limits and price of one model: model overrides, else provider values, else defaults."""
    is_metered = metered(row, locality)
    price_known = m.input_usd_per_mtok is not None or m.output_usd_per_mtok is not None
    local_free = not is_metered  # a local Ollama runs on our own hardware: price 0 by default
    return ModelLimits(
        provider=row.slug,
        model=m.model,
        metered=is_metered,
        max_output_tokens=max(1, m.max_output_tokens or row.max_output_tokens or s.provider_max_output_tokens),
        max_input_tokens=m.max_input_tokens if m.max_input_tokens is not None else row.max_input_tokens,
        provider_tokens_per_day=row.tokens_per_day,
        provider_tokens_per_month=row.tokens_per_month,
        model_tokens_per_day=m.tokens_per_day,
        model_tokens_per_month=m.tokens_per_month,
        provider_monthly_usd=effective_budget(row, s) if is_metered else row.monthly_budget_usd,
        model_monthly_usd=m.monthly_budget_usd,
        daily_requests=effective_daily_cap(row, s) if is_metered else row.daily_request_limit,
        max_turn_tokens=row.max_turn_tokens,
        input_usd_per_mtok=float(m.input_usd_per_mtok or 0.0),
        output_usd_per_mtok=float(m.output_usd_per_mtok or 0.0),
        price_known=price_known or local_free,
        supports_tools=m.supports_tools,
    )


async def probe(url: str, kind: str, self_hosted: bool) -> ProviderEndpoint:
    """Save-time check of a provider URL (raises BlockedDestination). Cloud endpoints must use HTTPS."""
    ep = await check_provider_url(chat_base(kind, url), allow_private=allows_private(kind, self_hosted))
    if ep.locality == "cloud" and ep.target.scheme != "https":
        raise BlockedDestination(
            "a provider on a public address must use https (keys and prompts would travel in clear)"
        )
    return ep


# --- Built-in providers from the environment ---------------------------------------------------------


async def _legacy_openrouter(db: AsyncSession) -> dict[str, Any]:
    row = await db.get(AppSetting, OPENROUTER_SETTING)
    return dict(row.value) if row is not None else {}


def _set_env_model(row: LlmProvider, desired: str) -> None:
    current = next((m for m in row.models if m.env_managed), None)
    same = next((m for m in row.models if m.model == desired), None)
    if current is not None and current.model == desired:
        return
    if same is not None:
        same.env_managed = True
        if current is not None:
            current.env_managed = False
        return
    if current is not None:
        current.model = desired  # follow the environment (keeps the default marker on the same row)
    else:
        row.models.append(LlmModel(model=desired, env_managed=True, enabled=True))


async def ensure_builtin_providers(db: AsyncSession, settings: Settings | None = None) -> None:
    """Create or refresh the built-in `ollama` and `openrouter` rows from the environment. Idempotent; commits."""
    s = settings or get_settings()
    fresh = {"populate_existing": True}  # the caller's session may hold rows changed by another request
    query = select(LlmProvider).where(LlmProvider.builtin).execution_options(**fresh)
    rows = {r.slug: r for r in (await db.scalars(query)).all()}
    changed = False

    ollama = rows.get(OLLAMA)
    if ollama is None:
        ollama = LlmProvider(
            slug=OLLAMA, name="Ollama (host)", kind="ollama", builtin=True, base_url=s.ollama_base_url, locality="local"
        )
        db.add(ollama)
        ollama.models = []
        changed = True
    if ollama.base_url != s.ollama_base_url:
        ollama.base_url = s.ollama_base_url
        changed = True

    legacy = await _legacy_openrouter(db)
    openrouter = rows.get(OPENROUTER)
    if openrouter is None:
        budget = legacy.get("monthly_budget_usd")
        openrouter = LlmProvider(
            slug=OPENROUTER,
            name="OpenRouter",
            kind="api",
            builtin=True,
            base_url=s.openrouter_base_url,
            locality="cloud",
            enabled=bool(legacy.get("enabled", True)),
            secret_name=OPENROUTER_SECRET,
            monthly_budget_usd=float(budget) if budget is not None else None,
        )
        db.add(openrouter)
        openrouter.models = []
        changed = True
    if openrouter.base_url != s.openrouter_base_url:
        openrouter.base_url = s.openrouter_base_url
        changed = True

    before = [(m.model, m.env_managed) for r in (ollama, openrouter) for m in r.models]
    _set_env_model(ollama, s.local_chat_model)
    _set_env_model(openrouter, str(legacy.get("model") or s.openrouter_model))
    after = [(m.model, m.env_managed) for r in (ollama, openrouter) for m in r.models]
    if changed or before != after:
        await db.commit()


# --- Keys ---------------------------------------------------------------------------------------------


async def resolve_key(
    db: AsyncSession, row: LlmProvider, settings: Settings | None = None, *, reveal: bool = True
) -> tuple[str | None, str]:
    """(key, source): the vault first; for the built-in OpenRouter the env var is the bootstrap fallback.

    reveal=False gives the status only (no decryption, no audit entry); the key is then a marker."""
    from app.services import vault

    s = settings or get_settings()
    if row.secret_name and await vault.exists(db, row.secret_name):
        if not reveal:
            return "<vault>", "vault"
        return await vault.reveal(db, row.secret_name, consumer=f"provider:{row.slug}"), "vault"
    if is_openrouter(row) and s.openrouter_api_key is not None and s.openrouter_api_key.get_secret_value():
        return s.openrouter_api_key.get_secret_value(), "env"
    return None, "none"


# --- Building providers for the router -----------------------------------------------------------------


@dataclass
class ProviderState:
    slug: str
    locality: str
    clearance: Classification
    available: bool
    reason: str = ""


@dataclass
class Built:
    providers: list[LLMProvider] = field(default_factory=list)
    budgets: dict[str, float] = field(default_factory=dict)
    daily_caps: dict[str, int | None] = field(default_factory=dict)
    limits: dict[str, ModelLimits] = field(default_factory=dict)
    default_id: str | None = None
    states: dict[str, ProviderState] = field(default_factory=dict)


async def current_endpoint(row: LlmProvider) -> tuple[str, str]:
    """(locality, problem). Re-evaluated from DNS every time the router is built.

    An unresolvable host is treated as cloud (the most restrictive); a policy violation disables it."""
    try:
        ep = await check_provider_url(
            chat_base(row.kind, row.base_url), allow_private=allows_private(row.kind, row.self_hosted)
        )
    except UnresolvableHost as exc:
        return "cloud", str(exc)
    except BlockedDestination as exc:
        return "blocked", str(exc)
    if ep.locality == "cloud" and ep.target.scheme != "https" and not row.builtin:
        return "blocked", "public address over plain http"
    return ep.locality, ""


JEV_ROUTER_MODEL = "typesafe/jev-router"  # OpenRouter's Jev Router, a virtual model (ADR 0032)
JEV_ROUTER_TIERS = ("low", "medium", "high")


def is_jev_router(row: LlmProvider, model: str) -> bool:
    return is_openrouter(row) and model == JEV_ROUTER_MODEL


def model_clearance(row: LlmProvider, m: LlmModel, locality: str, s: Settings) -> Classification:
    """The provider's clearance, lowered by the model's own (the Jev Router sees the whole conversation and
    defaults to public, as a hosted decider would)."""
    clearance = effective_clearance(row.max_classification, locality, s)
    own = m.max_classification
    if own is None and is_jev_router(row, m.model):
        own = Classification.public
    return own if own is not None and own.rank < clearance.rank else clearance


def validate_jev_router_options(raw: Any) -> dict[str, Any]:
    """Plugin options as documented by OpenRouter (routing/routers/jev-router): cost_tier low|medium|high (or
    absent for Jev's default policy), allowed_models and excluded_models as lists of patterns (at most 1,024
    patterns of 1,024 characters, 65,536 characters in total per list). Unknown fields are refused, as
    OpenRouter refuses them with HTTP 400."""
    if not isinstance(raw, dict):
        raise ProviderConfigError("jev_router options must be an object")
    unknown = set(raw) - {"cost_tier", "allowed_models", "excluded_models"}
    if unknown:
        raise ProviderConfigError(f"unknown Jev Router option(s): {', '.join(sorted(unknown))}")
    out: dict[str, Any] = {}
    tier = raw.get("cost_tier")
    if tier is not None:
        if tier not in JEV_ROUTER_TIERS:
            raise ProviderConfigError("cost_tier must be low, medium or high")
        out["cost_tier"] = tier
    for key in ("allowed_models", "excluded_models"):
        items = raw.get(key)
        if items is None:
            continue
        if not isinstance(items, list) or not all(isinstance(x, str) and 0 < len(x) <= 1024 for x in items):
            raise ProviderConfigError(f"{key} must be a list of model patterns")
        if len(items) > 1024 or sum(len(x) for x in items) > 65_536:
            raise ProviderConfigError(f"{key} is too long")
        if items:
            out[key] = list(dict.fromkeys(items))
    return out


def jev_router_plugin(options: dict[str, Any]) -> dict[str, Any]:
    return {"id": "jev-router", **validate_jev_router_options(options.get("jev_router") or {})}


def make_provider(row: LlmProvider, m: LlmModel, locality: str, key: str | None, s: Settings) -> OpenAICompatProvider:
    model = m.model
    clearance = model_clearance(row, m, locality, s)
    common: dict[str, Any] = {
        "provider": row.slug,
        "base_url": chat_base(row.kind, row.base_url),
        "model": model,
        "locality": "local" if locality == "local" else "cloud",
        "max_classification": clearance,
        "api_key": key,
        "allow_private": allows_private(row.kind, row.self_hosted),
        "cloud_cap": cloud_cap(s),
    }
    if is_openrouter(row):
        body: dict[str, Any] = {"usage": {"include": True}}  # OpenRouter reports the real cost of each request
        if model == JEV_ROUTER_MODEL:
            body["plugins"] = [jev_router_plugin(m.options or {})]
        return OpenAICompatProvider(
            **common,
            extra_headers={"HTTP-Referer": "https://badiapartners.com", "X-Title": "EnterpriseRAG"},
            extra_body=body,
        )
    if row.builtin and row.slug == OLLAMA:
        return OpenAICompatProvider(
            **common,
            strip_think=s.local_chat_strip_think,
            extra_body={"stream_options": {"include_usage": True}},  # token accounting (ADR 0026)
        )
    # Standard OpenAI streaming option: a final chunk with `usage` (and `cost` where the provider reports it).
    return OpenAICompatProvider(**common, extra_body={"stream_options": {"include_usage": True}})


async def build_from_db(db: AsyncSession, settings: Settings | None = None) -> Built:
    s = settings or get_settings()
    out = Built()
    query = (
        select(LlmProvider)
        .order_by(LlmProvider.builtin.desc(), LlmProvider.slug)
        .execution_options(populate_existing=True)
    )
    rows = (await db.scalars(query)).all()
    for row in rows:
        locality, problem = await current_endpoint(row)
        if locality != "blocked" and locality != row.locality and not problem:
            row.locality = locality  # keep the stored (displayed) locality in step with DNS
        if locality == "blocked":
            log.error("provider %s disabled: %s", row.slug, problem)
            out.states[row.slug] = ProviderState(row.slug, row.locality, Classification.public, False, problem)
            continue
        clearance = effective_clearance(row.max_classification, locality, s)
        if not row.enabled:
            out.states[row.slug] = ProviderState(row.slug, locality, clearance, False, "disabled")
            continue
        key, _source = await resolve_key(db, row, s)
        if is_openrouter(row) and not key:
            out.states[row.slug] = ProviderState(row.slug, locality, clearance, False, "no API key")
            continue
        out.states[row.slug] = ProviderState(row.slug, locality, clearance, True, problem)
        for m in row.models:
            if not m.enabled:
                continue
            try:
                p = make_provider(row, m, locality, key, s)
            except ProviderConfigError as exc:
                log.error("model %s:%s skipped: %s", row.slug, m.model, exc)
                continue
            out.providers.append(p)
            out.limits[p.model_id] = model_limits(row, m, locality, s)
            if m.is_default:
                out.default_id = p.model_id
        if metered(row, locality):
            out.budgets[row.slug] = effective_budget(row, s)
            out.daily_caps[row.slug] = effective_daily_cap(row, s)
    await db.commit()  # persist audited secret use and refreshed localities
    return out


# --- Test connection and model discovery -----------------------------------------------------------------


def _client(row: LlmProvider, locality: str) -> httpx.AsyncClient:
    transport = PinnedTransport(
        [],
        checker=provider_checker(
            allow_private=allows_private(row.kind, row.self_hosted), require_private=locality == "local"
        ),
    )
    return httpx.AsyncClient(transport=transport, timeout=15.0, follow_redirects=False)


MAX_DISCOVERY_BYTES = 2_000_000


async def _get_json(client: httpx.AsyncClient, url: str, key: str | None) -> Any:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    async with client.stream("GET", url, headers=headers) as resp:
        if resp.status_code >= 300:
            raise ProviderConfigError(f"GET {url} returned HTTP {resp.status_code}")
        body = b""
        async for chunk in resp.aiter_bytes():
            body += chunk
            if len(body) > MAX_DISCOVERY_BYTES:
                raise ProviderConfigError("model list too large")
    return json.loads(body)


async def discover_models(row: LlmProvider, key: str | None) -> list[str]:
    """Ollama: GET /api/tags. OpenAI-compatible: GET /models. Through the same egress guard as chat."""
    locality, problem = await current_endpoint(row)
    if locality == "blocked":
        raise BlockedDestination(problem)
    async with _client(row, locality) as client:
        try:
            if row.kind == "ollama":
                data = await _get_json(client, f"{row.base_url.rstrip('/')}/api/tags", key)
                names = [str(m.get("name") or m.get("model")) for m in data.get("models", [])]
            else:
                data = await _get_json(client, f"{row.base_url.rstrip('/')}/models", key)
                items = data.get("data", data) if isinstance(data, dict) else data
                names = [str(m.get("id")) for m in items if isinstance(m, dict) and m.get("id")]
        except httpx.HTTPError as exc:
            raise ProviderConfigError(f"cannot reach the provider: {type(exc).__name__}") from exc
        except (ProviderConfigError, BlockedDestination):
            raise
        except (ValueError, AttributeError, TypeError) as exc:
            raise ProviderConfigError("the provider returned an unexpected model list") from exc
    return sorted({n for n in names if n and n != "None" and MODEL_RE.match(n)})


async def test_connection(row: LlmProvider, key: str | None) -> dict[str, Any]:
    """A read-only request (no chat completion, so it costs nothing). Never returns the key."""
    locality, problem = await current_endpoint(row)
    if locality == "blocked":
        return {"ok": False, "locality": row.locality, "error": problem}
    try:
        models = await discover_models(row, key)
    except (ProviderConfigError, BlockedDestination) as exc:
        return {"ok": False, "locality": locality, "error": str(exc)}
    return {"ok": True, "locality": locality, "models_available": len(models), "warning": problem or None}


def parse_openrouter_prices(data: Any) -> dict[str, tuple[float, float]]:
    """OpenRouter's `GET /models`: `pricing.prompt` and `pricing.completion` are USD per token, as strings.
    Returned as USD per million tokens. Models without a valid price are left out."""
    items = data.get("data", []) if isinstance(data, dict) else []
    out: dict[str, tuple[float, float]] = {}
    for m in items:
        if not isinstance(m, dict) or not isinstance(m.get("id"), str):
            continue
        pricing = m.get("pricing") or {}
        try:
            prompt = float(pricing["prompt"]) * 1_000_000
            completion = float(pricing["completion"]) * 1_000_000
        except (KeyError, TypeError, ValueError):
            continue
        if prompt < 0 or completion < 0:  # "-1" marks a variable-price router model
            continue
        out[m["id"]] = (round(prompt, 6), round(completion, 6))
    return out


async def openrouter_prices(row: LlmProvider, key: str | None) -> dict[str, tuple[float, float]]:
    """The price list from OpenRouter's model list, through the same egress guard as chat."""
    locality, problem = await current_endpoint(row)
    if locality == "blocked":
        raise BlockedDestination(problem)
    async with _client(row, locality) as client:
        try:
            data = await _get_json(client, f"{row.base_url.rstrip('/')}/models", key)
        except httpx.HTTPError as exc:
            raise ProviderConfigError(f"cannot reach the provider: {type(exc).__name__}") from exc
    return parse_openrouter_prices(data)
