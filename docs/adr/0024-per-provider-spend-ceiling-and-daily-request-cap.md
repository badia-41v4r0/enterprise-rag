# 0024. Per-provider spend ceiling and daily request cap

**Status:** Accepted (2026-11-10) — **Owner review** (default values)

## Context
ADR 0016 bounds OpenRouter spend with two barriers: free models only, and a monthly ceiling fed by the cost OpenRouter reports in `usage.cost`. Admin-added OpenAI-compatible providers are usually paid, there is no general way to tell a free model from a paid one, and most providers (OpenAI, Groq, vLLM, LM Studio, Ollama) do not report the cost of a request at all. A provider whose cost is unknown must still not be able to spend unbounded money.

## Decision
- **Who is metered.** Every `api` provider, including self-hosted ones (a "self-hosted" endpoint on the LAN may be a gateway such as LiteLLM forwarding to a paid API), and any Ollama that is not local. A local Ollama is not metered (it runs on our own hardware). The orchestrator meters a provider when the router lists a ceiling for it or it is cloud (unchanged for OpenRouter and the dev `claude-code` provider).
- **Monthly ceiling.** As in ADR 0016 (`provider_spend`, per provider slug and UTC month, checked before every request). The cost is taken from `usage.cost` when the response reports it. For non-OpenRouter API providers the request asks for the final usage chunk with the standard `stream_options: {"include_usage": true}` (verified against Ollama's `/v1`). Environment ceiling `ERAG_PROVIDER_MONTHLY_BUDGET_USD`, default **0.01 USD**; the admin panel can only lower it per provider. OpenRouter keeps `ERAG_OPENROUTER_MONTHLY_BUDGET_USD` and the free-only rule exactly as before.
- **Daily request cap.** New table `provider_daily_usage` (provider slug, UTC day, requests). Before a request leaves the process the counter is incremented atomically (`INSERT ... ON CONFLICT DO UPDATE ... RETURNING`); if the new value is above the cap the request is refused (`BudgetExceeded`, shown to the user as a model error). The request is counted before it is sent, so failures and requests that report no cost are bounded too. Environment cap `ERAG_PROVIDER_DAILY_REQUEST_CAP`, default **100** requests per provider and day; the admin panel can only lower it (0 blocks the provider). OpenRouter has no daily cap unless the admin sets one (requests are still counted for display). Every tool-loop iteration is one request.
- **Keyed by slug.** Spend and request counters are keyed by the provider slug, so deleting a provider and recreating it with the same slug does not reset its month's spend or day's count.
- Values above the environment caps are refused on save (422) and, if written to the database directly, capped when the router is built.

## Consequences
- For a provider that reports no cost, the effective bound is "daily cap x price of the largest request". With the defaults that is 100 requests per day per provider. The admin should lower the cap for expensive models; capping output tokens per request (`max_tokens`) is not implemented (decision for the owner).
- The counters are per installation and use the database, so they hold across restarts and app instances.
- Tests: `tests/integration/test_spend.py` (counting, refusal, per day/provider, cap 0, effective values can only be lowered, who is metered), `tests/providers/test_provider_registry_real.py` (real Ollama `/v1`: requests counted, reported cost 0 recorded, cap reached refuses with no text generated).
