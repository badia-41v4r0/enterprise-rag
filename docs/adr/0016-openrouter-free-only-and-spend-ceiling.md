# 0016. OpenRouter: free models only and a hard monthly spend ceiling

**Status:** Accepted (2026-11-08)

## Context
The project budget is a few euros in total. OpenRouter is the only cloud provider. A wrong model id or a runaway loop must not be able to spend real money.

## Decision
Two independent barriers:

1. **Free-only.** `ERAG_OPENROUTER_MODEL` must end in `:free`, otherwise the app refuses to start (`validate_openrouter_model`). The provider repeats the check before every request (`free_only`), so even a code path that builds a provider by hand cannot call a paid model. Paid models require `ERAG_OPENROUTER_ALLOW_PAID=true`.
2. **Spend ceiling.** `ERAG_OPENROUTER_MONTHLY_BUDGET_USD` (default `0.01`, slightly under 0.01 EUR). The requests ask OpenRouter to report the real cost (`usage: {include: true}`); the orchestrator adds it to `provider_spend` (per provider and UTC month) after each cloud request, and refuses the next request once the month's total reaches the ceiling (`BudgetExceeded`, shown to the user as a model error). A ceiling of `0` blocks all cloud calls.

## Consequences
- With free models the recorded cost is 0, so the ceiling is a safety net, not the normal path.
- Cost is recorded only when the stream finishes; a request that fails midway is not counted (acceptable for free models).
- Free OpenRouter models are rate limited and their providers may log prompts. Combine with `ERAG_CLOUD_MAX_CLASSIFICATION` (see PROGRESS.md decision 4) so only suitable data is ever sent.
- Tests: `tests/integration/test_spend.py` (real Postgres, no model). The real OpenRouter provider test runs only when a key is configured.
