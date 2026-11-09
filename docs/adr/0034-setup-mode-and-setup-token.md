# 0034. Setup mode, the one-time setup token and `erag init`

**Status:** Accepted (2026-11-09), implements PLAN-v0.4 sections 3, 4 and 7 — **Owner review** (master key storage)

## Context
A fresh installation has no administrator. Whoever reaches the port first could create one (the "takeover race").
The plan asks for an installer command, a setup mode that serves only the wizard, and a one-time token that only
the person who installed the software has, like Jenkins' initial admin password.

## Decision
- **`setup_state`** (migration 0012): a singleton row (id 1, check constraint) with the step record (JSON), the
  SHA-256 of the setup token and its expiry, the SHA-256 of the setup browser session and its CSRF token, the
  license acceptance (ADR 0035), the installation type (ADR 0036), `completed_at/by/via` and the dismissed checklist
  items. The token itself is never stored.
- **Setup mode = a row whose `completed_at` is NULL.** No row means "never bootstrapped as a fresh installation"
  (development and test databases): no setup mode.
- **Upgrade invariant.** Migration 0012 writes a completed row (`completed_via = "upgrade"`, `install_type = "demo"`
  when `@acme.test` users exist) whenever the database already has users. `erag bootstrap` (every start) does the
  same if it finds users and no row. Only a database with **no user at all** enters setup mode. An existing
  installation is therefore never put into setup mode by an upgrade.
- **The token.** 24 characters from a 32-symbol alphabet without look-alikes, in six groups (120 random bits).
  `erag init` prints it and writes only `ERAG_SETUP_TOKEN_SHA256` to the environment file. Without `erag init`, the
  first start generates one, prints it to the log and writes it to `/data/setup-token` (mode 0600, in the data
  volume). It expires after `ERAG_SETUP_TOKEN_TTL_HOURS` (24) from the first start; an expired token is replaced at
  the next start (new log line and file). `erag setup --new-token` issues a new one on the server; the old one dies.
- **Exchange.** `POST /api/setup/token` (10 attempts per minute per client address and 30 in total, failures audited
  without the value) sets an HttpOnly, SameSite=Strict cookie limited to the path `/api/setup` and returns a CSRF
  token for the `X-Setup-CSRF` header. One setup session at a time (a new exchange ends the previous one), 4 hours.
- **The gate.** In setup mode every router except `/health`, `/api/info`, `/api/auth/*` and `/api/setup/*` answers
  `503 setup required`, except for an active administrator's session (not MFA-pending, not forced to change the
  password): from step 3 on, the administrator created in step 2 configures the rest through the normal admin
  screens. Steps 0 to 2 need the setup session; steps 3 to 11 need the setup session **and** that admin session.
- **Completion** (step 11, `erag create-admin` on a pending setup, or `ERAG_SEED_DEMO=true`, ADR 0040) records
  `completed_at`, removes the token hash, the setup session and the token file, and audits `setup.complete`. From
  then on every `/api/setup/*` endpoint answers 404 and the token can never be used again. The "not in setup mode"
  answer is cached for 30 seconds per process.
- **Reset needs the server.** `erag setup --reset --yes` re-opens setup (steps and the license record cleared, a new
  token printed). It needs database access; there is no web endpoint for it.
- **`erag init`** (and `deploy/install.sh`, a thin wrapper): refuses to overwrite an existing environment file or
  master key; writes the environment file with mode 0600: `ERAG_ENV=prod`, `ERAG_SEED_DEMO=false`, random
  `ERAG_SECRET_KEY`, `ERAG_MCP_CONTEXT_SECRET`, `ERAG_RUNNER_TOKEN`, `POSTGRES_PASSWORD`, the master key
  (`ERAG_SECRETS_KEY`), `ERAG_CLOUD_MAX_CLASSIFICATION=public`, secure cookies with `--https`, the token hash; checks
  Docker, Compose, Ollama, the two models and the port; prints the token, the URL, the Compose command and where
  to back up the master key. It needs no database and makes no network call besides the local Ollama check.
- **Organization settings in the database.** The company name and the cloud ceiling live in `app_settings`
  (`organization`); the environment value of the ceiling is the initial default. The provider registry reads the
  stored value (cached per process, refreshed on every router load, part of the router fingerprint). Changing the
  ceiling needs step-up. A skipped or never visited organization step stores `public` (the stricter setting).

## Owner review: where the master key lives
The containers run as uid 10001 and cannot read a 0600 file owned by the host user, so `erag init` puts the master key
into the (owner-only) environment file as `ERAG_SECRETS_KEY` and also writes an owner-only copy, `master.key`, next to
it, to be copied off the machine and then deleted. Alternatives: a Docker secret owned by uid 10001 (needs root), or a
key manager (roadmap).

## Consequences
- A person who reaches the port before the administrator cannot do anything without the token; the token is useless
  after completion; nothing but `/health`, `/api/info` and the wizard answers while setup runs.
- Administrators keep working during a reset; other users get 503 until the wizard is finished again.
- Tests: `tests/setup/test_setup_mode.py` (no-row, upgrade at bootstrap, migration 0012 on a database with users,
  token file mode and hash-only storage, 503 elsewhere, CSRF, wrong and expired tokens, rate limit, `erag init` hash,
  resume after a restart, idempotent steps, completion and 404 afterwards, CLI create-admin and reset).
