# 0018. Credential store: AES-256-GCM with a master key outside the database

**Status:** Accepted (2026-11-09), implements PLAN-v0.2 R2 and decision D4

## Context
Connectors, registered MCP servers and the OpenRouter provider need credentials. Environment variables are visible to everyone who can read the deployment configuration, cannot be changed without a restart, and are not audited. The plan asks for an encrypted store whose values are write-only through the API.

## Decision
- Table `secrets` (name, ciphertext, nonce, key id, version, created_by, updated_at, last_used_at, last_used_by). Encryption with AES-256-GCM from `cryptography` (Apache-2.0/BSD). A fresh 96-bit nonce per write; associated data `secret:<name>` so a ciphertext copied to another row fails authentication. MFA secrets use the same helper with `mfa:<user id>`.
- **Master key** from `ERAG_SECRETS_KEY` (base64, 32 bytes) or `ERAG_SECRETS_KEY_FILE`, created by `erag secrets init`. Required in `prod` (the app refuses to start without it). In dev, if neither is set, a key derived from `ERAG_SECRET_KEY` is used so the demo works with no setup; the Docker Compose file instead points `ERAG_SECRETS_KEY_FILE` at `/data/secrets.key`, which `erag bootstrap` generates on first start (mode 0600, dev only). A key id (hash prefix) is stored with each ciphertext so a wrong master key is detected instead of producing garbage.
- **Write-only API:** `PUT /api/admin/secrets` (create or replace), `DELETE`, and a list with metadata and references ("used by"). No endpoint returns a value. Changing secrets requires step-up (ADR 0019).
- **Use is audited:** internal components call `vault.reveal(name, consumer=...)`; every call writes `secret.use` with the consumer, never the value. Consumers with assigned secrets (connectors, MCP servers) go through `reveal_assigned`, which refuses names that are not assigned to them.
- **OpenRouter key:** lookup order vault (`openrouter_api_key`), then `ERAG_OPENROUTER_API_KEY` as a bootstrap fallback. The Providers screen sets key, model, on/off and ceiling; the model router is cached and rebuilt when the provider setting or the secret changes (fingerprint of `updated_at`/`version`), so no restart is needed. The free-only rule and the env ceiling from ADR 0016 stay in force: the UI ceiling can only lower `ERAG_OPENROUTER_MONTHLY_BUDGET_USD`, never raise it, and a non-`:free` model is rejected unless `ERAG_OPENROUTER_ALLOW_PAID=true`.
- **Rotation:** `erag secrets rotate-master --new-key-file F` re-encrypts all secrets and MFA secrets in one transaction.

## Consequences
- Protects against database dumps and backups leaking, **not** against compromise of the host that holds the master key (it sits next to the app). Losing the master key means losing the secrets (and the MFA enrolments); back it up separately from the database.
- The key is decrypted when the router is (re)built, not on every request; each rebuild is audited.
- Secret values given to the runner (R5) end up as container environment variables, visible to anyone with Docker access on that host (who is root-equivalent anyway).
- Tests: `tests/security/test_vault_and_mfa.py` (write-only API, audit, name binding, tampering, wrong key, no plaintext anywhere in the database, vault-before-env lookup, hot reload, rotation).
