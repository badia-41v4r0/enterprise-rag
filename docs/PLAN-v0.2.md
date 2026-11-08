# EnterpriseRAG after the MVP: administration, connectors and MCP extensibility

> Planning document. No code yet. Builds on the MVP in [`PLAN.md`](PLAN.md) and the ADRs in [`adr/`](adr/README.md).
> Status: draft for owner review (2026-11-08).

## 1. Goals

Let an administrator extend the system from the web UI, without losing the properties that define it (the backend decides permissions, data is authorized before a model or tool sees it, every action is audited):

1. Create and manage **roles**, with **delegated management** of containers (a department head can upload their own content if the admin allows it).
2. Upload files from the web UI with a proper workflow (status, versions, retry, delete).
3. Add **information sources (connectors)** that sync into a container.
4. Register **MCP servers** (any MCP, run in its own limited container, configurable by the admin) and decide which roles may use which tools.
5. An **encrypted credential store** for connector and MCP secrets, and for the OpenRouter key.
6. Optional **MFA (TOTP)** for every user, and re-authentication for sensitive admin actions.

## 2. Decisions taken in discussion

| # | Decision |
|---|---|
| D1 | Any MCP may be registered, but each one runs isolated (own container, limited, admin-configurable) and is only reachable through the gateway. |
| D2 | Data sent to an MCP is limited by a **clearance** per MCP (section 4), the same idea as the LLM router. |
| D3 | Connector documents **inherit the permissions of the container** the admin picks. Source-system ACLs are not mirrored. |
| D4 | A credential store (encrypted at rest) holds connector/MCP secrets and the OpenRouter key. |
| D5 | Users with `manage` on a container may upload and delete documents there when the admin grants it. They cannot change who has access or the classification. |
| D6 | MFA (TOTP) is optional for all users, including admins. Sensitive admin actions require recent re-authentication. |
| D7 | New MCPs, tools and connectors are invisible until the admin approves them and assigns roles and a classification. |

## 3. Why "filter first, then send to the MCP" works in one direction only

Two data flows exist and they need different answers:

- **Into the MCP (arguments the model sends).** Everything the model holds in its context was already authorized for this user (retrieval ACL, history re-validation). So what can flow into an MCP is bounded by what the user may see. What remains is *where it goes*: an MCP is an external recipient, exactly like a cloud model. We therefore give every MCP a **clearance** (`max_classification`) and refuse the call when the conversation's effective classification is higher. This is the filter-before-send step.
- **Out of the MCP (what it returns).** A tool usually fetches data itself from a system we do not control (an ERP, Drive, a ticket system) using its own credentials. We do not know that system's ACLs, so we cannot filter its output by user. For our reference servers this is solved by cooperation (they receive a signed permission context and scope their own queries). For third-party MCPs we can only: restrict **who** may call **which tool** with **which arguments**, limit **what the container can reach**, and **label everything it returns** with a classification chosen by the admin (default `confidential`, so the rest of the conversation stays on local models until the admin deliberately lowers it).

This limit is the same root cause as the connector ACL issue in section 6. It is documented as a known boundary in the threat model and the white paper.

## 4. MCP security model (target for releases R4 and R5)

- **States:** `pending` (registered) → `approved` (admin reviewed) → `disabled`. Nothing in `pending` is ever offered to a model.
- **Tools are discovered, not trusted.** On registration we call `tools/list` and store each tool's name, description and JSON schema with a content hash. Every tool starts `disabled`. The admin enables tools one by one, can set a per-tool classification, and can add **argument constraints** (enum, regex, bounds, or a forced value such as `department`).
- **Rug-pull / tool-poisoning defence.** If a server's tool list or any description/schema hash changes, the affected tools are disabled automatically and flagged for re-approval. Tool descriptions are untrusted text: bounded in size and sanitized before they reach the model.
- **Clearance and result label.** `max_classification` (highest data that may be sent as arguments) and `result_classification` (label put on everything it returns).
- **Gateway** (existing, extended): transport abstraction (stdio for built-in servers, streamable HTTP for registered ones), per-user and per-tool rate limits, timeouts, size limits, result validator, audit of every decision.
- **Admin-only and step-up:** registering or editing an MCP, connector or secret requires recent re-authentication.
- **Isolation (R5):** own container per MCP; see R5.

## 5. Releases

Estimates are for focused full-time work by one developer; double them for part-time. Each release ends in a tagged version, updated ADRs/threat model, and passing CI. Tests keep the project rule: no fake providers; real Postgres; real servers and real folders instead of mocks.

### R1. Roles, delegated management and uploads (about 1.5 weeks)

- **Roles:** create, rename, clone, delete (blocked while users are assigned unless reassigning), role templates (Reader, Contributor, Manager) as presets for grants, bulk user assignment.
- **Grants matrix UI:** role × resource (container, document, tool) × action, with an effective-permissions preview for a given user.
- **Delegated management:** a `manage` grant on a container lets that role's members upload, replace and delete documents and toggle `restricted` on documents there. It does **not** allow changing grants, classification, or anything outside that container. The admin decides who gets `manage`.
- **Uploads UI:** multi-file drag and drop, per-file status (queued, processing, ready, failed with the error), version history, replace, delete, retry failed. Managers see only their containers.
- **Data model:** no new tables expected; new authorization checks and API surface.
- **Tests:** a manager cannot escalate (change grants, classification, reach other containers, read documents without a read grant); deleting a role in use; revocation of `manage` is immediate; every action audited.
- **Exit:** a department head uploads and replaces a document in their own container, and cannot see or touch anything else.

### R2. Credential store and MFA (about 2 weeks)

**Credential store**
- Table `secrets` (name, ciphertext, nonce, version, created_by, updated_at, last_used_at). AES-256-GCM through the `cryptography` package (permissive license), with the secret's name as associated data so ciphertexts cannot be swapped between names.
- **Master key** from `ERAG_SECRETS_KEY` or `ERAG_SECRETS_KEY_FILE`, generated by `erag secrets init`, required in production. Honest limit: it protects against database dumps and backups leaking, not against compromise of the host that holds the master key. Losing the master key means losing the secrets.
- Write-only API and UI: a secret can be created, replaced or deleted but never displayed again. The UI shows name, last used and what references it. Every use is audited (who, which consumer, never the value). `erag secrets rotate-master` re-encrypts everything.
- **OpenRouter key:** lookup order is vault, then the `ERAG_OPENROUTER_API_KEY` environment variable as a bootstrap fallback. A Providers screen sets the key, model and spend ceiling without a restart (providers are rebuilt when settings change). The free-only guard and spend ceiling from ADR 0016 stay in force.
- Other components never read secret values directly: connectors and the MCP runner receive them through the vault service for exactly the secrets assigned to them.

**MFA (TOTP, optional)**
- Standard TOTP (RFC 6238, `pyotp`) enrolled from the user's profile with a QR code and a manual key, plus 10 single-use recovery codes (stored hashed). The TOTP secret is stored encrypted with the same crypto helper.
- Two-step login only for users who enabled it. Attempts are rate limited and a used time step cannot be replayed.
- An admin can reset a user's MFA (audited). An admin who loses their device uses a CLI reset on the server (`erag reset-mfa`).
- **Step-up:** actions that create code-execution or data-access paths (secrets, connectors, MCP registration, promoting a user to admin, resetting someone's MFA) require re-entering the password, or a TOTP code if MFA is on, within the last 5 minutes.
- **Tests:** real TOTP codes computed from the stored secret; replay rejected; recovery code works once; rate limit; step-up expiry; the database contains no plaintext secret (checked by scanning the stored bytes).
- **Exit:** the OpenRouter key lives only in the vault, and a user can log in with and without MFA.

### R3. Connectors (about 2.5 weeks)

- **Interface:** a `Connector` kind has a config schema, `list_items(cursor)`, `fetch(item)` and change detection (etag, mtime or hash). New kinds plug in without touching the core.
- **Tables:** `connectors` (kind, name, container, config, secret references, schedule, enabled, status), `connector_items` (external id, document, last seen hash), `sync_runs` (counts, errors, duration).
- **Sync:** a worker job `sync_connector`, run manually or on an interval. Items are upserted by external id and hash through the existing versioning, so unchanged content is skipped. Items that disappear from the source are removed from the index. Limits on item count and size, allowed file types, and error isolation per item.
- **Permissions:** every document inherits the container the admin chose when creating the connector. No per-file ACLs.
- **First two kinds:**
  - `folder`: a directory below an admin-configured allowed root (`ERAG_CONNECTOR_ROOTS`, mounted read-only into the worker). Path traversal and symlink escapes are refused.
  - `web`: a list of URLs or a sitemap with page and depth limits. Server-side request forgery protection: loopback, link-local and private ranges are blocked unless the admin allowlists them; redirects are re-checked.
- **UI:** list, create, edit, test connection, dry-run preview of what would be imported, "sync now", run history with errors.
- **Security notes:** fetched content is untrusted and takes the normal parse and prompt-delimiter path; creating a connector requires step-up auth; the form warns that everyone with access to the container will see everything the connector imports.
- **Tests:** real temporary directories (traversal, symlink escape, update, delete, idempotent re-sync); a real local HTTP server for the web kind; SSRF attempts against loopback and private addresses; the sync leaves the leakage and canary suites green.
- **Later connectors** (not in this plan): Google Drive, SharePoint, S3, Git, Confluence. They need OAuth and bring the ACL mismatch described in section 6.
- **Exit:** point a folder connector at a directory, assign it to a container, and see its documents answer questions only for the roles that read that container.

### R4. MCP registry for remote servers and the clearance model (about 2.5 weeks)

- **Registration:** name, transport (`http` or `builtin`), URL, authentication through a vault secret, `max_classification`, `result_classification` (default `confidential`), then tool discovery as in section 4.
- **Review screen:** diff of discovered tools, enable per tool, per-tool classification, argument constraints, and the role grants (`execute`) per tool.
- **Gateway changes:** an HTTP transport next to stdio; clearance check before each call; tool-change detection; the model is told a tool is unavailable when refused; all decisions audited.
- **Network safety:** TLS verification, SSRF rules for the URL, admin allowlist for private addresses.
- **Reference servers:** gain an HTTP transport so the same tests can run over both.
- **Tests:** real reference servers over HTTP; rug-pull simulation (a real server changes a description, tools become disabled); clearance refusal when the conversation is confidential; argument constraints enforced; unapproved tools never offered; SSRF cases.
- **Exit:** an admin registers a remote MCP, approves two of its tools for one role, and only users of that role see and can call them.

### R5. Isolated runner for MCP containers (about 3 weeks)

- **Separate runner service.** The app never receives the Docker socket (it is equivalent to root on the host). A small `runner` service exposes a narrow, authenticated internal API: start, stop, status, logs for an MCP.
- **Only approved images.** The runner has an image allowlist pinned by digest; an admin cannot make it run an arbitrary image.
- **Per-MCP limits, configured by the admin:** CPU and memory, process limit, timeout, idle shutdown, environment variables, secrets (from the vault, only those assigned), host paths mounted read-only from an allowlisted set, and network mode.
- **Hardening defaults:** non-root, read-only root filesystem with a small tmpfs, all Linux capabilities dropped, `no-new-privileges`, default seccomp profile, a dedicated internal network reachable only by the gateway, no access to the app, database or other MCPs.
- **Network:** default `none`. Allowlisted egress is delivered through a per-MCP forward proxy container that only permits the listed domains; this is the hardest part and may ship after the rest of R5 if it takes too long.
- **Operations:** lifecycle controls and a redacted log viewer in the admin UI; health checks.
- **Tests (marker `docker`, need a real Docker daemon):** a container with network `none` really cannot connect; it cannot write outside tmpfs; it cannot reach Postgres, the app or another MCP; limits are applied; secrets are visible only to the MCP they were assigned to; an image outside the allowlist is refused.
- **Open question:** Docker versus rootless Podman. The runner interface is kept engine-neutral so Podman can be added.
- **Exit:** register an MCP packaged as an approved image, configure it from the UI, and call its tool through the gateway while it is provably confined.

## 6. Known boundaries (to be documented, not hidden)

- **Source ACLs are not mirrored.** If a Drive or SharePoint folder contains files restricted to some people in the source system, importing it into a container shows those files to everyone with access to that container. Connectors should only point at folders whose audience matches the container's audience. Mirroring source ACLs is a separate, larger feature.
- **Third-party MCP output cannot be filtered per user** because we do not know the ACLs of the system behind it; it is labeled and access-controlled at tool level (section 3).
- **The master key sits on the same host as the app.** The credential store protects against database leaks, not host compromise.
- **Compromise of an admin account** remains the largest risk. Mitigations: optional MFA, step-up for sensitive actions, audit, isolation of MCP containers.

## 7. Cross-cutting work in every release

- Alembic migrations (starting at 0004), ADRs for each decision above (suggested 0017 onwards).
- Update `threat-model.md`, `architecture.md` (and the English diagram), `README.md`, and the white paper (new section: extensibility without losing control; known boundaries).
- Keep the canary matrix, revocation, routing and tool suites green and extend them to roles, managers, connectors and MCP clearance.
- Audit entry for every new administrative action.

## 8. Order and effort

| Release | Depends on | Estimate (full-time) |
|---|---|---|
| R1 Roles, delegation, uploads | MVP | ~1.5 weeks |
| R2 Credential store and MFA | R1 | ~2 weeks |
| R3 Connectors | R2 (secrets, step-up) | ~2.5 weeks |
| R4 MCP registry (remote) | R2 | ~2.5 weeks |
| R5 Isolated runner | R2, R4 | ~3 weeks |
| **Total** | | **~11 to 12 weeks (about 5 to 6 months part-time)** |

R3 and R4 are independent of each other and can swap order. R5 is the riskiest and can be cut into "network none" first and "allowlisted egress" later.

## 9. Open items for the owner

1. Confirm the clearance model in section 3, and the default `result_classification = confidential` for registered MCPs.
2. Confirm that managers (D5) cannot change grants or classification, only content.
3. Docker or rootless Podman for the runner.
4. Release hygiene: review the white paper, enable GitHub private vulnerability reporting, merge the MVP branch and tag `v0.1.0` before starting R1 (recommended), then work on `feat/*` branches.
