# 0028. Remove the OpenRouter free-only rule (supersedes ADR 0016's first barrier)

**Status:** Accepted (2026-11-11), implements PLAN-v0.3 section 4 and decision D1. Supersedes the free-only part of [ADR 0016](0016-openrouter-free-only-and-spend-ceiling.md), which is kept as history.

## Context
ADR 0016 had two barriers on OpenRouter: model ids had to end in `:free`, and a monthly ceiling fed by the reported cost. The owner decided (D1) that the free-only rule is removed from the app and the documentation once budget controls exist. They now do (ADR 0026, ADR 0027), and they work for every provider, not only OpenRouter.

## Decision
- Removed: `validate_openrouter_model`, the `free_only` flag of the OpenAI-compatible client and its per-request check, the start-up refusal of a paid `ERAG_OPENROUTER_MODEL`, the `openrouter_allow_paid` setting, the `free_only` / `allow_paid` fields of the Providers API and the "Free models only" line of the Providers screen.
- `ERAG_OPENROUTER_ALLOW_PAID` is **ignored with a warning** at start-up for one release (`app.config` logs "ERAG_OPENROUTER_ALLOW_PAID is ignored: ..."); Compose still passes it through only so that the warning appears.
- OpenRouter is bounded like any metered provider: its monthly ceiling (`ERAG_OPENROUTER_MONTHLY_BUDGET_USD`, default 0.01 USD), the preventive worst-case check with prices synced from OpenRouter's own model list, the daily request cap (now the default 100, as for every metered provider), `max_tokens` per request and the quotas.
- The default OpenRouter model stays a free one (`meta-llama/llama-3.3-8b-instruct:free`) so tests and the demo cost nothing. Free models remain a default, not a rule.
- The owner's own limit is enforced by configuration (a low monthly ceiling) and by the credit limit set on the OpenRouter key itself, which also holds if the app is misconfigured or an admin session is hijacked.

## Consequences
- Tests that asserted the rule were replaced: `test_paid_openrouter_models_are_allowed_and_the_old_switch_warns`, and the budget tests of ADR 0026 / 0027.
- Threat T34 is now covered by the budget controls instead of the model-id rule.
