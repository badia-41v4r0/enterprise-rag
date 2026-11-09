# 0027. Quotas per user and per role: standard caps, collective pools, overrides

**Status:** Accepted (2026-11-11), implements PLAN-v0.3 section 3 and decision D4; the combination rule "to confirm" in the plan is implemented as proposed (**Owner review**).

## Decision
- **What is limited:** tokens (input plus output) and estimated USD, per UTC day and per UTC month, for all providers or for one provider (table `quotas`: subject `user` or `role`, kind `standard` or `collective`, optional provider slug, four optional limits; a database check enforces the shape; one row per subject, kind and provider scope).
- **Standard cap per member** (role): each person with the role gets that allowance, counted on their own usage (`llm_usage`).
- **Collective pool** (role): one allowance shared by everyone holding the role, counted in `quota_pool_usage` (per quota and period, day and month rows), charged after each request with the request's real tokens and cost.
- **Personal override** (user): replaces the standard allowance.
- **How they combine**, separately for each provider scope (all providers, and the provider of the request): a person's standard allowance is their override if one exists, otherwise the **highest** standard cap among their roles, per dimension (a role that sets no value in a dimension means no limit there, so it wins). **Every collective pool of every role they hold** is charged, and the request is refused when the worst case of the request (as in ADR 0026) does not fit in **any** applicable cap. A pool on a role everyone holds is a company-wide pool (valid, not the intended use).
- **Who sees what.** A user sees only their own usage and their allowances (`GET /api/usage/me`, Account page); for a shared pool they see its limits and whether it still has room, never how much colleagues used (a pool of two people would otherwise reveal a colleague's usage). Admins see aggregates, per-user figures and pool consumption (`GET /api/admin/usage`, `GET /api/admin/users/{id}/usage`), never prompts or answers (the usage tables hold no text).
- **Blocked message:** "Your daily allowance is used up; it resets at 00:00 UTC." (or the monthly / shared-pool variant), sent as an `error` event with `code: quota_exceeded`; the chat shows it as a notice, not as a model failure. Nothing is sent to the model.
- Creating, changing or deleting a quota needs step-up and is audited; quotas are deleted with their user or role (cascade).

## Consequences
- Quotas apply to local models too (tokens), and to every provider, unless scoped to one provider.
- Pool usage is charged after the request: as with budgets, concurrent requests can overshoot a pool by their worst cases.
- A person who changes roles keeps their own usage history; pools keep what was charged to them.
- Tests: `tests/security/test_quotas_and_limits.py` (override vs highest standard cap, per-person counting, a role without a value, collective pool shared and every pool charged, several roles at once, provider scope, reset at day and month change, API step-up and audit, privacy of `/api/usage/me`) and the real-model test in `tests/providers/test_provider_registry_real.py` (exhausted allowance refuses with the user-facing message and nothing is sent).
