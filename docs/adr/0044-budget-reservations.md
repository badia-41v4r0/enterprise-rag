# 0044. Budget reservations

**Status:** Accepted (2026-11-18). Closes the open item of ADR 0026 ("budgets are checked, not reserved").

## Context
ADR 0026 checks the worst case of a request against provider, model and user budgets before sending it. Two requests in flight at once, in one replica or several, can both see room for one: the budget could be overspent by up to the number of concurrent requests. And a stream that failed half-way recorded no usage at all.

## Decision
- **Reserve, then settle.** In a short transaction of its own, a request takes a transaction-level advisory lock per budget scope (provider, model, user, every collective pool that applies), **always in sorted order** so two requests can never deadlock; re-runs every check with other requests' active reservations counted as spent; inserts its own reservation rows (worst-case tokens and dollars) and commits.
- After the model answers, the real usage is recorded **and the reservation deleted in the same transaction**: never counted twice, never zero times. The user lock is per user, not per user and provider, because a user's allowance may span providers.
- A stream that fails mid-way records what was sent (estimated) and settles the same way.
- A request that dies leaves rows that **expire after 10 minutes**; they stop counting at once and the maintenance task deletes them.
- `budget_reservations` is a normal (logged) table: it is money.
- `preflight` (check only) remains for the model router's dry runs and counts reservations too.

## Consequences
- Proven by a test: six concurrent requests against a budget with room for two get exactly two through.
- Two short transactions more per model call, and serialization per scope while reserving (milliseconds). Generation itself is not serialized.
