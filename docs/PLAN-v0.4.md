# EnterpriseRAG v0.4: guided first-run setup for a company's administrator

> Planning document. No code yet. Builds on v0.3 (providers, quotas, model router) and the earlier plans.
> Status: draft for owner review (2026-11-09).

## 1. The problem

Installing EnterpriseRAG for a real company today means editing an `.env` file, starting Docker Compose, running a command to create the first administrator, and then discovering by trial and error which of eleven admin tabs to configure and in which order. The demo hides all of it behind seeded data. A company's administrator needs a **guided path from "installed" to "working and safe"** in about half an hour, without reading the source.

## 2. Goals

1. A **first-run wizard** in the web UI that takes a fresh installation to a working, secure system: administrator, organization, models, access structure, first documents, and a check that access control behaves.
2. **Safe by construction:** nobody but the person who installed the software can start the setup, and the wizard disappears once it is done.
3. **Honest verification:** the wizard checks the things that commonly break (database, master key, Ollama and its models, HTTPS, default secrets) and says what to fix, instead of failing later.
4. A **permanent setup checklist** after the wizard for the recommended items the administrator postponed.
5. Nothing phones home: no telemetry and no license server.

Non-goals: SSO, email invitations (no mail server is assumed), multi-tenant onboarding, editing environment-level secrets from the browser.

## 3. Two parts: an installer and a wizard

### 3.1 Installer (command line, before the first start)
Some settings must exist before the application starts and must not be editable from a web page: the session secret, the MCP context secret, the runner token, the master key of the credential store, the database password. A new command prepares them:

- `erag init` (and a thin `deploy/install.sh` wrapper) creates the environment file with **strong random secrets**, generates the **master key file** with owner-only permissions, picks the production settings (`ERAG_ENV=prod`, `ERAG_SEED_DEMO=false`), and prints where to **back up the master key** (losing it means re-entering every stored secret).
- It checks the prerequisites it can see from the host (Docker, Compose, Ollama reachable) and tells the administrator what is missing.
- It prints the **setup token** (section 4) and the URL to open.

### 3.2 Wizard (web UI, first start)
The steps below run in the browser. The wizard **verifies** the environment-level settings but never edits them.

## 4. Setup mode and the setup token

- **Setup mode** is on while no administrator exists and the setup has not been completed. In this mode the application serves only the wizard and a status endpoint; everything else answers `503 setup required`.
- A **one-time setup token** is generated at first start, written to the server log and to a file inside the data volume (mode 0600), and has to be entered on the wizard's first screen. This prevents someone who reaches the exposed port before the real administrator from taking over the installation (the same idea as Jenkins' initial admin password).
- The token is bound to the setup, rate limited, never reused, and **destroyed on completion**; after that every setup endpoint returns 404. A server administrator can re-open setup only with a command on the server (`erag setup --reset`), which requires access to the database and files, not a web request.
- The wizard recommends keeping the port on localhost or a private network until setup finishes, and the check in step 1 warns when it is exposed without HTTPS.

## 5. The wizard

Progress is stored server-side (`setup_state`), so a closed tab or a restart resumes where it was. Every step explains in two sentences why it matters, offers a safe default, and can be revisited later from **Admin > Setup assistant**.

| # | Step | What it does |
|---|---|---|
| 0 | **Welcome and license** | Shows the license in plain language (source-available, noncommercial use free, commercial use needs a license from Badia Partners, the 60-day evaluation permit) with the website link. Informational only: no key, no enforcement, no network call. |
| 1 | **Environment check** | Read-only checks with green, amber or red: database reachable and migrations current; master key present and a reminder to back it up; production mode; the three secrets are not the development defaults; HTTPS and secure cookies; storage writable; the worker is alive; Ollama reachable with the **embedding model** and at least one **chat model** present; runner reachable (optional). Each failure shows the exact fix. |
| 2 | **Administrator** | Name, email, a strong password (strength meter, 12 or more characters) and an offer to enroll **MFA** right away with recovery codes shown once. |
| 3 | **Organization** | Company name (shown in the header instead of the demo name) and the **data classification policy**: what may leave the building. The choice sets the cloud ceiling (`public` by default, `internal` if the company accepts internal data going to approved cloud providers); confidential never leaves. |
| 4 | **Models** | Detects the local Ollama and its models, lets the administrator pick the chat model and confirms the embedding model (with a warning that changing it later means re-indexing; the choice is locked after the first document is indexed). Optionally adds a cloud provider (API key goes straight to the credential store, clearance defaults to `public`) and sets the default model and the monthly budget. A **Test** button on each. Offers to download a missing Ollama model, as an explicit admin action. |
| 5 | **Access structure** | Starter templates that create **containers and roles** together: *Small company* (General, Management), *By department* (HR, Finance, Engineering, Sales, Legal) and *Blank*. Each container gets a classification (HR and Finance confidential, most others internal, public documentation public) and each role the right level (Reader, Contributor, Manager). Everything is editable and nothing is created until the administrator confirms. |
| 6 | **People** | Create users and assign roles, one by one or by pasting a CSV (email, name, role). Passwords are temporary and **must be changed at first login** (a small new feature). Optional default quotas per role from the quota screen. |
| 7 | **Knowledge** | Upload the first documents or add a folder or web connector to a container, with live ingestion progress. |
| 8 | **Does it work?** | A built-in check: ask a question **as a chosen user** and show the effective permissions, the sources used, which model answered and its classification, so the administrator sees that people with different roles get different answers before anyone else logs in. |
| 9 | **Tools and router (optional)** | A short explanation and links to the MCP registry and the Model router, both left off or empty by default. Skippable. |
| 10 | **Review and finish** | A summary of everything created, a **security checklist** (MFA on, master key backed up, HTTPS, no demo data, cloud ceiling, budget set, a second administrator exists), and an exportable **setup report** (Markdown) for the company's IT records. Finishing records `setup_completed_at`, destroys the setup token, audits the event and signs the administrator in. |

## 6. After the wizard

- **Setup checklist** card at the top of the admin area until every recommended item is done or dismissed: enable MFA, back up the master key, add a second administrator, set provider budgets, review the router, remove demo data.
- **Setup assistant** in the admin area re-opens any step for editing (idempotent) with the normal admin session; it is not the token-gated installation mode.
- **Demo data:** loading the ACME demo is a deliberate choice offered only in development mode. If demo data is detected in a production installation, the checklist flags it and offers a guarded removal.

## 7. Data model and API (sketch)

- `setup_state` (singleton): step statuses, `setup_completed_at`, token hash and expiry, who completed it.
- `users.must_change_password` (boolean) with a forced change at first login.
- Settings stored in the database (the cloud ceiling and the company name become admin-editable at runtime, with the environment value as the initial default).
- `GET /api/setup/status` (public: mode, step progress, no secrets), `POST /api/setup/token`, step endpoints under `/api/setup/*` accepting only the token or the new admin's session, all disabled after completion.
- Every step is audited, and secrets only travel to the credential store.

## 8. Security notes

- **Takeover race:** closed by the setup token and by serving nothing else in setup mode.
- **No secrets in the wizard output:** checks report booleans and fixes, never values; the setup report omits secrets.
- **Wizard as an SSRF path:** provider and connector tests use the existing destination guards; private addresses are only accepted for local or self-hosted kinds.
- **Defaults that fail safe:** a skipped step leaves the stricter setting (no cloud provider, router off, no connectors, `public` as the cloud ceiling).
- **Demo credentials:** the wizard refuses to finish in production while the demo password users exist.
- **Reset needs the server,** not the browser.

## 9. Tests (real components, no fakes)

- Against a **throw-away installation** (its own Compose project and volumes, never the owner's data): API tests for the token, setup mode answering 503 elsewhere, resume after restart, idempotent steps, completion disabling every endpoint, password policy, MFA enrollment, forced password change, template creation, CSV import with bad rows.
- **Environment checks tested against real failures:** an unreachable Ollama URL, a missing model, a default secret, missing master key, a non-writable storage path.
- An **end-to-end browser test** that installs from scratch: run `erag init`, open the wizard, complete every step, and finally log in as two users with different roles and see different answers.
- The leakage and routing suites must stay green on a wizard-created installation.

## 10. Documentation

An **Installation guide** (prerequisites, `erag init`, starting the stack, opening the wizard, what each step asks, how to back up the master key and the database, upgrading), a short **troubleshooting** page driven by the environment checks, and a link from the white paper's how-to. Screenshots of the wizard in the README.

## 11. Releases and effort

| Release | Contents | Estimate (full-time) |
|---|---|---|
| R10 | `erag init`, setup mode, setup token, the environment checks, administrator and MFA, organization | ~1.5 weeks |
| R11 | Models step, access-structure templates, people with forced password change, CSV import | ~1.5 weeks |
| R12 | Knowledge step, "Does it work?", review and report, setup checklist, setup assistant, installation guide, end-to-end test | ~1.5 weeks |
| **Total** | | **about 4.5 weeks (roughly 2 months part-time)** |

## 12. Questions for the owner

1. **Starter templates:** are *Small company*, *By department* and *Blank* the right three, and which departments should the second one create?
2. **Forced password change** for users created in the wizard: yes (recommended)? Without email, temporary passwords are handed over by the administrator.
3. **Downloading Ollama models from the wizard** (an outbound download started by the administrator): allow it, or only show the command to run?
4. **The license welcome screen:** informational as proposed, or do you want something stronger such as an acknowledgement checkbox (still no enforcement)?
5. **Language:** English only for now, with the texts kept in one place so translations can be added later?
6. Should **demo data** be removable by the wizard in production, or only by a command on the server?
