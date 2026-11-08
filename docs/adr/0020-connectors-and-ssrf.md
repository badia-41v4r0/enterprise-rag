# 0020. Connectors: container-inherited permissions, folder roots, SSRF guard

**Status:** Accepted (2026-11-09), implements PLAN-v0.2 R3 and decision D3

## Decision
- **Interface.** A connector kind is a class with a pydantic `Config`, `list_items()`, `fetch(item, previous_hint)` and `test()`, registered in `KINDS`. The sync engine (`app/connectors/sync.py`) is kind-agnostic. Tables `connectors`, `connector_items` (external id, document, content hash, version hint) and `sync_runs` (status, counts, errors, duration), migration 0006.
- **Permissions.** Every imported document goes into the container chosen by the admin and inherits its access, classification and routing. Source ACLs are not mirrored (known boundary, PLAN-v0.2 section 6). The create form and the dry-run response carry an explicit warning.
- **Sync.** Upsert by external id through the normal versioning path (new version of the same document, only that document re-indexed); unchanged items are skipped by version hint (folder: mtime and size; web: ETag with `If-None-Match`) or SHA-256. Items that disappear are deleted from the index, but only after a complete listing (a listing cut by the item limit or an error deletes nothing). Per-item error isolation; item count, size and file-type limits. Jobs are `sync_runs` rows claimed by the worker with `FOR UPDATE SKIP LOCKED`; a scheduler in the worker queues due connectors (`schedule_minutes`). Dry run ("preview") runs the listing without importing.
- **`folder` kind.** The configured folder must resolve (real path) inside one of `ERAG_CONNECTOR_ROOTS`, which Compose mounts read-only into the app and the worker. Symlinked files and folders are never followed (reported as skipped); files are opened with `O_NOFOLLOW` and re-checked to be inside the folder.
- **`web` kind and SSRF.** A list of URLs and/or a sitemap, same-host crawling with page and depth limits and an optional URL regex. All outbound requests use `app/core/netguard.py`: http(s) only, no credentials in URLs, every resolved address must be public unless listed in `ERAG_SSRF_ALLOWLIST` (`host`, `host:port` or CIDR, set by the operator, not in the UI), the connection is pinned to the checked IP (Host header and TLS SNI kept, certificates verified), redirects are followed manually and re-checked at every hop (and `Authorization`/`Cookie` are dropped when the host changes), bodies are capped. The same guard is used for remote MCP servers (R4).
- **Secrets.** A connector lists the vault secrets assigned to it; its config can only reference those (checked at create and at sync), and it receives them through `vault.reveal_assigned` (audited as `connector:<id>`).
- **Step-up** for create and edit (ADR 0019). Test, preview, sync and delete are admin-only.

## Consequences
- Fetched content is untrusted and goes through the same parsers, delimiters and injection handling as uploads.
- Admins can delete or edit connector-imported documents from the Content screen, but the next sync recreates them; manage them at the source.
- The allowlist is global (environment), deliberately not editable from the UI: a compromised admin account cannot open internal networks to the server.
- Residual risk: DNS answers are checked and pinned per request; an address that is public at check time and internal at another time is not reachable because the connection uses the checked address.
- Tests: `tests/security/test_connectors.py` (roots, traversal, symlink escapes, update/delete/idempotent re-sync, limits, dry run, 13 SSRF destinations, allowlist precision, redirect escape to a second real server, assigned secrets only, and the permission boundary with real bge-m3).
