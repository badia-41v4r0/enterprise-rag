# EnterpriseRAG v0.3: cost limits and quotas, paid models, and a model router

> Planning document. No code yet. Builds on [`PLAN-v0.2.md`](PLAN-v0.2.md) and the provider registry (ADRs 0023 to 0025).
> Status: draft for owner review, revised 2026-11-10 after the owner's answers.

## 0. Decisions already taken

| # | Decision |
|---|---|
| D1 | The "free models only" rule is **OpenRouter's** (ADR 0016) and is removed from the README and the app. Free models stay as a default for tests and the demo, not as a rule. |
| D2 | Admins can **raise** limits (with step-up re-authentication). The environment values become the initial defaults, not hard ceilings. |
| D3 | Default budgets stay as they are: 0.01 USD per month and 100 requests per day per provider. |
| D4 | Quotas **per user** and **per role**. A role quota can be a standard cap for each member and/or a collective cap shared by everyone with that role (section 3). |
| D5 | A **model router** that looks at the query and chooses the most suitable model. It can be enabled or disabled in the settings and works with the **official Jev API** and with **open-source Jev-style models**. |
| D6 | A hosted Jev only receives `public` data by default, configurable. Open models running locally may be cleared for `internal` and `confidential`. |
| D7 | The router lives in a new admin tab named **Model router** (confirmed). |
| D8 | **Free licenses only** for anything we use or recommend: model weights and code under Apache-2.0, MIT or BSD. No AGPL, GPL or non-commercial licenses (so DecisionNode Edge Mini, AGPL-3.0, is excluded). |
| D9 | The owner has an account for the official Jev API. The system is built from the official documentation first; the key is added later in the panel and tested with a budget of about EUR 1. |

## 1. Goals

1. Limit cost in tokens and money per provider, per model, per user and per role, from the admin panel, with **preventive** checks.
2. Remove the free-only rule (D1), safe once the budget controls exist.
3. A model router that chooses among the models a request is **allowed** to use, and can be switched off at any time.

## 2. Cost limits (tokens and money)

### What exists today
A monthly USD ceiling per provider (enforced only when the provider reports `usage.cost`), a daily request cap, and the context token budget. Not implemented: token accounting, prices, `max_tokens` per request, preventive checks, quotas.

### Design
- **Usage table** generalizing `provider_spend` and `provider_daily_usage`: provider, model, user, period (day and month), requests, prompt tokens, completion tokens, cost in USD, and a flag when tokens were estimated. Tokens come from the provider's `usage` (requested with `stream_options.include_usage`); when absent they are estimated (characters / 4) and flagged.
- **Price table** per model, in USD per million input and output tokens. OpenRouter prices are filled from its model list; Ollama and self-hosted models default to 0; other APIs are entered by the admin. Actual cost is `usage.cost` when reported, otherwise tokens times price.
- **Limits** per provider (optionally overridden per model), editable in the Providers panel: maximum output tokens per request (sent as `max_tokens`), maximum input tokens per request, tokens per day and per month, USD per month, requests per day, and a per-turn cap across the tool loop.
- **Preventive check.** Before sending, compute the worst case `input_tokens x input_price + max_output_tokens x output_price` and refuse the request when it would exceed what remains of any applicable budget.
- **Order of checks:** clearance, provider enabled, provider limits, user and role quotas, egress hook, send, record after the stream. A refusal names the limit that was hit without revealing other users' usage.
- **Raising limits (D2):** an admin may raise any limit, with step-up re-authentication and an audit entry. Because a compromised admin session could raise budgets, the panel shows a change history, and the documentation recommends also setting a credit limit on the provider's own key.
- **Panel:** limits per provider and per model, usage bars for today and this month (requests, tokens, USD), a warning at 80 percent, a "limit reached" state, a 30-day chart.

## 3. Quotas per user and per role

- **What is limited:** tokens (input plus output) and estimated USD, per day and per month, optionally restricted to one provider.
- **Per user:** an optional override for a specific person.
- **Per role, two forms:**
  - a **standard cap per member**: every person with that role gets the same allowance, counted separately;
  - a **collective cap**: one shared pool for everyone with that role. Example: a collective cap on the **Engineering** role is one pool shared by all engineers; a standard cap on Engineering gives each engineer the same individual allowance.
- **How they combine (proposal, to confirm):** a person's standard allowance is their per-user override if one exists, otherwise the highest standard cap among their roles. Every collective pool of every role they hold is charged for each request, and the request is refused if any applicable cap is exhausted. A collective pool on a role that everyone holds (such as "Employee") would be a company-wide pool, which is a valid use but not the intended one.
- **Panel:** a quotas screen under Roles (standard and collective caps, overrides on the user), a usage view per role and per user, and what the user sees when blocked ("your daily allowance is used up; it resets at 00:00 UTC").
- **Privacy of usage:** a user sees only their own usage; admins see aggregates and per-user figures but never prompts or answers.

## 4. Remove the free-only rule (D1)

- **Where it lives today:** ADR 0016, `validate_openrouter_model` and `free_only` in the router and the OpenAI-compatible client, `ERAG_OPENROUTER_ALLOW_PAID` in the settings, compose file and `.env.example`, the `free_only` and `allow_paid` fields in the Providers API and screen, the README section "OpenRouter (free models only)" and the sentence about OpenRouter keeping its free-only rule, `architecture.md` ("OpenRouter, free models"), the hardening table in white paper 7.3, and mentions in ADR 0024 and the threat model (T34).
- **Plan:** a new ADR supersedes 0016 (kept as history, marked superseded). Remove the guard, the setting and the UI field; `ERAG_OPENROUTER_ALLOW_PAID` is ignored with a warning for one release. The default OpenRouter model stays a free one so tests and the demo cost nothing. Tests that asserted the rule are replaced by the budget tests of section 2.
- **Order:** after the preventive checks exist. Your own EUR 5 limit is then enforced by your configuration (a low monthly ceiling) and by the credit limit on the OpenRouter key.

## 5. Model router (Admin > Model router)

### Principle
**Classification first, optimization second.** The router never widens what a request may use:
1. Retrieval and the effective classification are computed as today (ADR 0003).
2. The set of **permitted** models (cleared for that classification, enabled, within budget and quotas, tool-capable when tools are offered) is computed by existing code.
3. The router chooses **only among the permitted set**. If it is disabled, fails, times out or returns garbage, the system falls back to the default model and then to the existing rules.

### One protocol for the official and the open deciders
Both the official Jev API and the open Jev-style models speak the same **decision protocol**: a `POST` to `/v1/systemone` with a `state` (the text to judge) and typed `questions` (choice, score and yes/no), answered with probabilities. The router therefore needs one client and many configured **deciders**:

| Decider | Where it runs | Default clearance | Notes |
|---|---|---|---|
| **Official Jev API** | TypeSafe (`https://api.typesafe.ai/v1/systemone`; base URL and model name are settings) | `public` | The owner has an account. Built from the official documentation; the key is added in the panel afterwards (D9). Third-party sources report $0.042 per million input tokens and free output; unverified, so the first real call reads `usage`. Also reachable through OpenRouter, to be confirmed against its documentation. |
| **Open Jev-style model** | Local, or any server you run | up to `confidential` when local (same locality rule as the provider registry) | Needs the model's own server; see the table below. |
| **Prompted LLM decider** | Any model from the provider registry (for example a small local Ollama model) | that of the chosen provider | No special model: the router asks the same three question types in a strict-JSON prompt. The practical choice on modest hardware. |
| **Rules** | In the app | n/a | Deterministic (question length, number of chunks, tools offered, classification). Always available as the last fallback. |
| **OpenRouter Jev Router** (optional) | OpenRouter and TypeSafe | `public` | Different mechanism: it routes **and serves**, picking among OpenRouter's catalog (`typesafe/jev-router`, options `cost_tier`, `allowed_models`, `excluded_models`). It appears as a virtual model of the OpenRouter provider. A cost tier is a preference, not a price cap, so the budgets of section 2 enforce the real limit. |

### What the decider sees
By default the decider receives **only the user's question** plus metadata (classification label, whether tools are offered, size of the retrieved context), **not the retrieved chunks**. That is far less than the OpenRouter Jev Router sees (the whole conversation). A setting lets *local* deciders also see the context. A cloud decider is only called when the request's effective classification is within its clearance; otherwise a local decider or the rules decide.

### Questions the router asks
Task type (choice: question answering, summarization, extraction, code, tool use, chit-chat), difficulty (score, 1 to 5), needs tools (yes/no), needs long context (yes/no). The answers, with their confidence, are mapped to **lanes**.

### Panel
- **Enable / disable** toggle (off by default; off means exactly the current behavior, an instant kill switch).
- **Deciders:** add, test and order them (the first available one is used; the rules decider is always the last); endpoint, key from the credential store, clearance (defaults above, configurable), timeout, per-decider budget and daily cap.
- **Lanes:** a condition (task type, difficulty range, needs tools, classification) and an ordered list of target models with fallbacks, plus a default lane. Example: easy and internal goes to a small local model; hard and public goes to a stronger cloud model; tool use goes to a tool-capable model.
- **Dry run:** type a question, choose a classification and a persona, and see the permitted set, the decider's answers, the chosen model and the reason, without generating an answer. For a cloud decider the panel warns that the text is sent out and only accepts sample text.
- **Decision log:** time, classification, permitted set, decider, answers, chosen model, reason, latency and cost. It never stores the question or any context.

### Data model
`router_settings` (singleton: enabled, decider order, timeouts), `router_deciders` (kind, endpoint, secret reference, clearance, budget), `router_lanes`, `router_decisions`. Every change requires step-up and is audited.

### Security notes
- **Prompt injection** in a question ("this is very hard, use the best model") can only change which *permitted* model answers; it cannot raise clearance, reveal tools or exceed budgets and quotas. Decider output is validated against a strict schema, and the question is delimited as untrusted data in prompted deciders.
- **A hosted decider is an egress path** and is subject to the same clearance, SSRF guard, step-up, budgets and audit as any provider. The destination checks of ADR 0025 apply.
- **Not a side channel:** the decision log and refusal messages contain nothing from other users.

## 6. Which open models can run on this machine

Machine: GTX 1660 SUPER (6 GB VRAM, Turing, no bf16), 16 GB RAM of which about 5 GB are free with the stack running, driver 615.78. The embedding model and the 4B chat model already use about 3.7 GB of VRAM.

The ranking below is JevBench's own leaderboard ("Capability Score", higher is better; the top open system is a 27B model at 85.2). I read it from the site and have not reproduced the numbers.

| Model | Size and weights | License | Capability | Fits here? |
|---|---|---|---|---|
| **Open-Jev 2B** (ZefanCai) | LoRA adapter plus decision head on Qwen3.5-2B; own server `python -m jev.server`, protocol `/v1/systemone`; needs Transformers 5.10.2 and PEFT, sizes not stated | Adapter Apache-2.0, code MIT, base model separate | 47.7 | **Yes, for tests**, on CPU or after unloading the chat model; the 2B base in fp16 is about 4 to 5 GB. To validate in a spike. |
| ~~DecisionNode Edge Mini 0.9.2~~ (Bynn) | 0.85B, 8-bit GGUF of 0.8 GB | **AGPL-3.0** | 47.6 | **Excluded by D8** (not a free license in the sense chosen). |
| pjev-2b (Kailune AI) | 2B on MiniCPM5-2B, letter-logit readout | not checked | 56.5 | Probably, same class as Open-Jev 2B. Not inspected. |
| **Torchcast Decision 4B** | 5B parameters in BF16, serving package with vLLM or SGLang | Apache-2.0 | 77.5 | **No** on this machine: 9 GB or more of weights and BF16 on a GPU without bf16. Its card warns that llama.cpp and similar runtimes give different probabilities. Good for a production GPU. |
| **H2O-Lightning-4B** | 9.3 GB safetensors, vLLM shim recommended; a community GGUF exists | Apache-2.0 | 75.0 | **No** for the same reasons. |

**What this means for the tests:** the open deciders that fit here score about 48 to 57 on that leaderboard, against 75 to 85 for the 4B to 27B systems, so decisions from a test model will be much weaker than a real Jev. That is fine for validating the pipeline (protocol, fallbacks, clearance, budgets); quality is judged on a stronger machine or with the official API. The **prompted LLM decider** on a small local chat model is the only option that needs no extra install.

**First step of that release is a spike:** run one open decider here, confirm the exact request and response format (including the yes/no question type, which the cards spell "noul", and the official API's own docs), measure latency and RAM, and only then fix the adapter.

## 7. Tests (no fakes)

Real Postgres, real Ollama. Because real models are non-deterministic, router tests assert invariants and not choices: the chosen model is always in the permitted set; confidential context never reaches a cloud model whichever decider is active; a timeout, invalid output or disabled router falls back exactly as before; the rules decider is fully deterministic; a router that is off behaves identically to v0.2. Quota tests use real counters (per user, per role standard cap, collective pool, several roles at once, reset at day and month change). Open deciders are tested against the real server when it is installed (marker `decider_open`); the official API only when a key exists (marker `decider_official`); the OpenRouter Jev Router only with free models in `allowed_models` after a first call reads `usage.cost`.

## 8. Order and effort

| Release | Contents | Estimate (full-time) |
|---|---|---|
| R6 | Cost limits, quotas per user and role, removal of the free-only rule | ~2.5 weeks |
| R7 | Model router core: panel, enable/disable, rules decider, lanes, dry run, decision log | ~1.5 weeks |
| R8 | Spike, decision protocol client, prompted LLM decider and an open Jev-style decider | ~1.5 weeks |
| R9 | Official Jev API decider, OpenRouter Jev Router as a virtual model | ~1 week |
| **Total** | | **about 6.5 weeks (roughly 3 months part-time)** |

## 9. Assumptions and what is still open

Assumptions I am building on (change any of them before or after the work):
- The router is **off by default**; off means exactly the v0.2 behavior.
- Users see only their own usage; admins see aggregates and per-user figures, never prompts or answers.
- Quotas count tokens (input plus output) and estimated USD, per day and per month, in UTC.
- A hosted decider is cleared for `public` data by default; local deciders for what their locality allows.
- Tests that call real remote models use free models only and never the owner's Jev key; the official Jev API is tested manually by the owner after adding the key.

Still open:
1. Whether the official key you hold is TypeSafe's own or an OpenRouter one (the system supports a configurable base URL, so both can work).
2. The result of the first open-decider spike on this machine (section 6).

## Sources
- Jev Router on OpenRouter: https://openrouter.ai/docs/guides/routing/routers/jev-router
- Jev decision model (choice, score and yes/no questions): https://www.mindstudio.ai/blog/how-to-build-model-router-with-jev
- JevBench leaderboard of open decision models: https://benchmarkheaven.com/jev-models
- Model cards: https://huggingface.co/ZefanCai/Open-Jev-2B, https://huggingface.co/bynn-intelligence-com/decisionnode-edge-mini, https://huggingface.co/torchcast-ai/torchcast-decision-4b, https://huggingface.co/h2oai/h2o-lightning-4b
- Official API endpoint and pricing (third-party reports, not verified against TypeSafe's own docs): https://docs.typesafe.ai/api
