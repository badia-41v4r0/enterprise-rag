# 0042. Permission cache by epoch, and the search plan for selective users

**Status:** Accepted (2026-11-18).

## Context
Effective permissions were computed from Postgres on every request: a few indexed queries, fine for one replica and tens of users, wasteful for thousands of users and several replicas. A time-based cache would delay revocation, which the project never accepts (ADR 0002). Separately, the R13 benchmark showed the vector search ran as a sequential scan, and that users who can read 1% of the corpus make a filtered HNSW scan walk much of the graph.

## Decision
- **Cache keyed by (user, epoch).** `authz_state.epoch` is increased **in the same transaction** as any change that can alter what someone may read or do: grants, roles, memberships, containers, a user's active/admin flags, a document's container or restricted flag. SQLAlchemy events (`before_flush`, `do_orm_execute`) do it for ORM changes and bulk UPDATE/DELETE, so services cannot forget. A request reads the epoch (one row) and recomputes only when it changed: revocation is immediate on every replica. Changes made with raw SQL outside the app must bump the epoch themselves.
- **The index drives the search.** The inner query filters on `chunks` columns only, so an ordered HNSW scan with `iterative_scan` feeds the top k; documents are joined afterwards and rejected candidates are compensated by over-fetching (`docs/benchmarks/r16-index-drives-search.md`).
- **Selective users get an exact scan.** From the planner's estimate of the user's readable chunks (cached per container set, no counting), below 20,000 rows and 10% of the corpus the search disables index scans for that statement only (bitmap scan on the container index plus an exact sort), and switches them back before the transaction continues. The filter is identical either way; only the plan changes.
- **ACL id lists are one array parameter** (`= ANY(:ids)`), not one bind parameter per id, so thousands of restricted-document grants do not change the plan. The ids still come from `app.authz.policy`, the single place where permissions are decided; the policy is not re-expressed in SQL.

## Consequences
- One extra indexed read per request; no recomputation in the common case.
- The epoch is global: any permission change invalidates every cached entry. At the expected rate of admin changes this is cheap; finer keys can come later without changing callers.
- The thresholds are defaults from this machine; the synthetic benchmark (R20) refines them.
