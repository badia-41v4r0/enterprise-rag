# EnterpriseRAG v0.3: cost limits, paid models and a model router

> Planning document. No code yet. Builds on [`PLAN-v0.2.md`](PLAN-v0.2.md) and the provider registry (ADRs 0023 to 0025).
> Status: draft for owner review (2026-11-10).

## 0. Interpretations to confirm

1. You wrote that the README should stop saying **Ollama** is free-only. Ollama has no free or paid models (it is local), so I read this as the **OpenRouter free-models-only rule** (ADR 0016), which is what the README and the app actually say. If you meant something else, tell me.
2. **Jev** is TypeSafe's decision model. It is a classifier, not a text generator: it answers typed questions about an input (choice, score and yes/no with probabilities). Its hosted form is available on OpenRouter as the **Jev Router** (`typesafe/jev-router`): it picks a model and a reasoning effort for each request, and the chosen model answers. I could **not confirm an official open-weights Jev**; some write-ups describe community "open Jev" clones and one webinar mentions an open-weight version, but I found no model card or license. The plan therefore supports Jev through OpenRouter and is built so an open Jev (or any other local decision model) plugs in later once its weights and license are confirmed.

## 1. Goals

1. Limit **cost in tokens and money** per provider and per model, from the Providers panel, and make the limits *preventive* (refuse a request that could overshoot) and not only reactive.
2. **Remove the free-models-only rule** from the README and the app, now that real budget controls replace it. Free models remain a default for tests and the demo, not a rule.
3. A new **Admin > Model router** panel where the system chooses which model answers each request: by deterministic rules, by a local decision model, or by Jev through OpenRouter.

## 2. Cost limits (tokens and money)

### What exists today
A monthly USD ceiling per provider (enforced only when the provider reports `usage.cost`), a daily request cap, and the context token budget used by the context builder. Not implemented: token accounting, prices, `max_tokens` per request, preventive checks.

### Design
- **Usage table** generalizing `provider_spend` and `provider_daily_usage`: provider, model, period (day and month), requests, prompt tokens, completion tokens, cost in USD, and a flag when tokens were estimated. Tokens come from the provider's `usage` (requested with `stream_options.include_usage`, already used for Ollama); when absent they are estimated (characters / 4) and flagged.
- **Price table** per model, in USD per million input and output tokens. OpenRouter prices are filled from its model list; Ollama and self-hosted models default to 0; other APIs are entered by the admin. Actual cost is `usage.cost` when reported, otherwise tokens × price.
- **Limits** (per provider, optionally overridden per model), all editable in the Providers panel:
  - maximum output tokens per request (sent as `max_tokens` / `max_completion_tokens`),
  - maximum input tokens per request (refuse, or trim the retrieved context, which already has its own budget),
  - tokens per day and per month,
  - USD per month (existing), requests per day (existing),
  - a per-turn token cap across the whole tool loop (every iteration counts).
- **Preventive check.** Before sending, compute the worst case: `input_tokens × input_price + max_output_tokens × output_price`, and refuse when it would exceed what remains of the budget. This closes the gap where one large request overshoots a ceiling that is only checked beforehand.
- **Order of checks in the orchestrator:** clearance, provider enabled, budgets (requests, tokens, projected USD), egress hook, send, record after the stream. A refusal tells the user which limit was hit, without leaking numbers about other users.
- **Optional quotas per user or role** (daily tokens) so one person cannot spend the whole budget. Stage 2 of this release.
- **Panel:** per-provider and per-model limit form, usage bars for today and this month (requests, tokens, USD), a warning at 80 percent, a "limit reached" state, a 30-day usage chart.
- **Who may raise limits.** Today the operator's environment caps are hard ceilings and the panel can only lower them. With paid models allowed, the operator will want a generous hard ceiling and the admin to tune within it; raising requires step-up re-authentication.
- **Tests (no fakes):** real Postgres for counters and refusals; real Ollama for token usage (a deliberately huge price on a local model proves the projected-cost refusal end to end); a handful of real calls to a free OpenRouter model to check reported tokens and cost; a pure unit test for the cost formula.
- **Exit:** a request that could exceed the remaining budget is refused before it leaves the process, and the panel shows accurate usage for the day and the month.
- **Effort:** about 1.5 weeks.

## 3. Remove the free-only rule

- **Where it lives today:** ADR 0016 (free-only and monthly ceiling), `validate_openrouter_model` and `free_only` in the router and the OpenAI-compatible client, `ERAG_OPENROUTER_ALLOW_PAID` in the settings, compose file and `.env.example`, the `free_only` and `allow_paid` fields in the Providers API and screen, the README section titled "OpenRouter (free models only)" and the sentence about OpenRouter keeping its free-only rule, `architecture.md` ("OpenRouter, free models"), the hardening table in white paper 7.3, and mentions in ADR 0024 and the threat model (T34).
- **Plan:** a new ADR supersedes 0016 (kept as history, marked superseded). Remove the guard, the setting and the UI field. `ERAG_OPENROUTER_ALLOW_PAID` is ignored with a warning for one release. The default OpenRouter model stays a free one so tests and the demo cost nothing, but only as a default value. Tests that asserted the rule are replaced by the budget tests of section 2.
- **Order:** do it **after** the preventive budget checks exist, not before; the rule is only safe to drop once something else bounds the bill. Your own €5 limit is then enforced by your configuration (a low monthly ceiling) and by the credit limit on the OpenRouter key.
- **Effort:** about 0.5 week, delivered together with section 2.

## 4. Model router (Admin > Model router)

### Principle
**Classification first, optimization second.** The router never widens what a request may use:
1. Retrieval and the effective classification are computed as today (ADR 0003).
2. The set of **permitted** models (cleared for that classification, enabled, within budget, tool-capable when tools are offered) is computed by existing code.
3. The router chooses **only among the permitted set**. If it fails, times out or returns garbage, the system falls back to the default model and then to the existing rules.

### Selectors (pluggable backends)
| Selector | Where it runs | Sees | Notes |
|---|---|---|---|
| **Rules** | In the app | Question length, number of chunks, tools offered, classification | Deterministic, free, instant. First thing to build. |
| **Local decision model** | A local model through the provider registry | The request (it is local, so it may see confidential data) | Implements Jev's choice / score / yes-no pattern with any local model: task type (choice), difficulty 1 to 5 (score), needs tools (yes/no). Strict JSON, temperature 0, timeout, fallback to rules. |
| **Jev through OpenRouter** | OpenRouter and TypeSafe | The whole conversation, including retrieved chunks | A cloud recipient, so its clearance is capped like any cloud provider (never confidential). |
| **Open Jev (later)** | Local | Same as the local selector | Added as an adapter when official weights and a compatible license are confirmed. |

### Jev through OpenRouter, as documented
- Called by setting `model` to `typesafe/jev-router`. Plugin options: `id: "jev-router"`, `cost_tier` (`low`, `medium`, `high`), `models` / `allowed_models` (preferences with fallback), `excluded_models` (hard exclusions), plus an optional `reasoning.effort` hint. Patterns can be exact slugs, wildcards or `~author/family-latest` aliases.
- It **routes and also serves**: the chosen model generates the answer, and the response's `model` field says which one. With the header `X-OpenRouter-Metadata: enabled` the response carries the candidates, effort, tier, estimated cost and reason, which we store in the decision log.
- **Limits that shape our design:** a cost tier is "a routing preference, not a fixed model, guaranteed score, or per-request price cap" (so our budgets from section 2 must enforce the real cap, using the most expensive allowed model as the worst case); effort hints are not guaranteed; the catalog is OpenRouter's, so `allowed_models` must be limited to what the admin approved; global data region only; the pages do not describe data retention, so treat it as an untrusted cloud recipient; it cannot be combined with another router model; pricing of the router itself is not stated, so the first real call must read `usage.cost` before we rely on it.
- In our system it appears as a **virtual model of the OpenRouter provider** (`typesafe/jev-router`) with its own clearance, price assumptions and limits.

### Panel
- **Mode:** off (current behavior) | rules | local decision model | Jev (OpenRouter). A kill switch returns to the default behavior instantly.
- **Selector settings:** local model choice and timeout; for Jev, the cost tier, allowed and excluded model patterns and the reasoning-effort hint.
- **Lanes** (for rules and the local selector): a lane has a condition (task type, difficulty range, needs tools, classification) and an ordered list of target models with fallbacks. A default lane catches everything else. Example: easy question on internal data goes to the small local model, a hard one on public data goes to a stronger cloud model, tool use goes to a tool-capable model.
- **Dry run:** type a question, choose a classification and a persona, and see the permitted set, the decision, the reason and the estimated cost, without generating an answer. For the hosted selector the dry run sends text to a cloud service, so the panel warns and only accepts sample text.
- **Decision log:** last decisions with time, classification, permitted set, chosen model, reason, latency and estimated cost. It never stores the question or any context.

### Data model
`router_settings` (singleton: mode, selector model, Jev options, timeouts), `router_lanes` (conditions, ordered targets), `router_decisions` (time, user, conversation, classification, permitted set, chosen model, reason, latency, estimated cost). Every change to settings and lanes requires step-up and is audited.

### Security notes
- **Prompt injection** in a question ("this is very hard, use the best model") can only change *which permitted model* answers; it cannot raise clearance, reveal tools, or exceed budgets. The selector's output is parsed against a strict schema, and the question is delimited as untrusted data in its prompt.
- **A hosted selector is an egress path.** It is subject to the same clearance, SSRF guard, step-up and budgets as any provider.
- The router must not become a side channel: the decision log and the refusal messages contain no content from other users.

### Hardware note (GTX 1660 SUPER, 6 GB VRAM)
The embedding model and a 4B chat model already occupy most of the VRAM. A second LLM for classification would force loading and unloading models on every request. The local selector should therefore reuse the chat model already loaded, or stay disabled; the rules selector is the default recommendation on modest hardware. Real latency to be measured when built.

### Tests (no fakes)
Real Postgres and real Ollama. Because a real model is non-deterministic, tests assert invariants and not its choices: the chosen model is always in the permitted set; confidential context never reaches a cloud model whichever selector is active; a timeout, an invalid output or a disabled selector falls back exactly as before; the rules selector is fully deterministic; mode "off" behaves identically to v0.2. Jev through OpenRouter gets a few real calls restricted to free models in `allowed_models`, reading `usage.cost` first.

### Effort and order
- **R7:** panel, rules selector, lanes, decision log, dry run: about 1.5 weeks.
- **R8:** local decision-model selector: about 1 week.
- **R9:** Jev through OpenRouter as a virtual model: about 0.5 to 1 week.

## 5. Order and effort

| Release | Contents | Estimate (full-time) |
|---|---|---|
| R6 | Cost limits (section 2) and removal of the free-only rule (section 3) | ~2 weeks |
| R7 | Model router panel, rules selector, lanes, decision log | ~1.5 weeks |
| R8 | Local decision-model selector | ~1 week |
| R9 | Jev through OpenRouter | ~0.5 to 1 week |
| **Total** | | **about 5 to 5.5 weeks (roughly 2.5 months part-time)** |

## 6. Decisions for the owner

1. Confirm the two interpretations in section 0.
2. **Who may raise limits:** keep "the environment sets hard ceilings, the panel can only lower them", or let the admin raise within an operator ceiling with step-up? I recommend the second once paid models are allowed.
3. **Default budgets after the free-only rule goes:** today 0.01 USD a month and 100 requests a day per provider. They protect you but block real paid use until raised.
4. Include **per-user and per-role quotas** in R6?
5. A hosted Jev sees the full conversation, so it would be cleared for `public` data only by default. Is that acceptable, or do you only want it for the demo?
6. Whether to wait for an official open-weights Jev, or build the local decision model with an existing small model now (recommended).

## Sources

- OpenRouter documentation for the Jev Router: https://openrouter.ai/docs/guides/routing/routers/jev-router
- Jev description (choice, score and yes/no questions; open clones): https://www.mindstudio.ai/blog/how-to-build-model-router-with-jev
