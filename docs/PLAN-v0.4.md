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
| 0 | **Welcome and license** | Shows the license in plain language (source-available, noncommercial use free, commercial use needs a license from Badia Partners, the 60-day evaluation permit) with the website link, and an **acceptance checkbox** the administrator must tick to continue. The acceptance (who, when, and a hash of the license text shown) is recorded in `setup_state`. It is a record and a deliberate pause, not technical enforcement: no key, no license server, no network call. The wording should be reviewed by a lawyer. |
| 1 | **Environment check** | Read-only checks with green, amber or red: database reachable and migrations current; master key present and a reminder to back it up; production mode; the three secrets are not the development defaults; HTTPS and secure cookies; storage writable; the worker is alive; Ollama reachable with the **embedding model** and at least one **chat model** present; runner reachable (optional). Each failure shows the exact fix. |
| 2 | **Administrator** | Name, email, a strong password (strength meter, 12 or more characters) and an offer to enroll **MFA** right away with recovery codes shown once. |
| 3 | **Installation type** | **Empty installation** (default): nothing is created, the administrator configures everything by hand. **Demo with mock data**: installs the fictional ACME Corp (its users, roles, containers, documents, tools and a sample structure) so the product can be explored immediately. The demo is clearly labeled, see section 6. |
| 4 | **Organization** | Company name (shown in the header; "ACME Corp" for the demo) and the **data classification policy**: what may leave the building. The choice sets the cloud ceiling (`public` by default, `internal` if the company accepts internal data going to approved cloud providers); confidential never leaves. |
| 5 | **Models** | Detects the local Ollama and its models, lets the administrator pick the chat model and confirms the embedding model (changing it later means re-indexing; the choice is locked after the first document is indexed). Optionally adds a cloud provider (API key goes straight to the credential store, clearance defaults to `public`) and sets the default model and the monthly budget. A **Test** button on each. **Ollama models can be downloaded from here, but only when the administrator presses Download** on a specific model, after a confirmation that shows the model name, its size and that it comes from the Ollama registry; with progress and a cancel button. Nothing is ever downloaded automatically. |
| 6 | **Access structure** | **Manual, no starter templates.** The administrator creates the containers (with a classification each), the roles, and the grants between them, using the same screens as the rest of the admin area, with inline explanations of what each choice means and an effective-permissions preview. The step shows what is still missing (at least one container and one role) and never creates anything on its own. For a demo installation this step is already filled in by the mock data. |
| 7 | **People** | Create users and assign roles, one by one or by pasting a CSV (email, name, role). Passwords are temporary and the user **must change them at first login** (a small new feature). Optional default quotas per role from the quota screen. Skipped for a demo installation. |
| 8 | **Knowledge** | Upload the first documents or add a folder or web connector to a container, with live ingestion progress. Skipped for a demo installation. |
| 9 | **Does it work?** | A built-in check: ask a question **as a chosen user** and show the effective permissions, the sources used, which model answered and its classification, so the administrator sees that people with different roles get different answers before anyone else logs in. |
| 10 | **Tools and router (optional)** | A short explanation and links to the MCP registry and the Model router, both left off or empty by default. Skippable. |
| 11 | **Review and finish** | A summary of everything created, a **security checklist** (MFA on, master key backed up, HTTPS, demo data present or not, cloud ceiling, budget set, a second administrator exists), and an exportable **setup report** (Markdown) for the company's IT records. Finishing records `setup_completed_at`, destroys the setup token, audits the event and signs the administrator in. |

## 6. After the wizard

- **Setup checklist** card at the top of the admin area until every recommended item is done or dismissed: enable MFA, back up the master key, add a second administrator, set provider budgets, review the router, remove demo data.
- **Setup assistant** in the admin area re-opens any step for editing (idempotent) with the normal admin session; it is not the token-gated installation mode.
- **Demo installation (mock data).** Chosen explicitly in step 3, never by default. It installs ACME Corp with its personas, roles, containers, documents, tools and sample providers. Because the demo users share a **published password**, the demo is safe only on a private network, so: the persona accounts are created with the demo password but **the administrator is the one created in step 2** (the demo's own `admin@acme.test` is not created, so there is no known-password admin); a persistent **"Demo installation, not for real data"** banner is shown in the interface; the review step warns to keep the instance on localhost or a private network; and the checklist offers **Remove demo data** from the admin area, with a confirmation that lists what will be deleted, to turn the installation into an empty one.

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
- **Demo credentials:** only an explicit demo installation creates users with the published password, there is no known-password administrator, the banner and the review warning make the state visible, and the checklist keeps flagging it until the demo data is removed.
- **Reset needs the server,** not the browser.

## 9. Tests (real components, no fakes)

- Against a **throw-away installation** (its own Compose project and volumes, never the owner's data): API tests for the token, setup mode answering 503 elsewhere, resume after restart, idempotent steps, completion disabling every endpoint, password policy, MFA enrollment, forced password change, CSV import with bad rows.
- **Environment checks tested against real failures:** an unreachable Ollama URL, a missing model, a default secret, missing master key, a non-writable storage path.
- **License acceptance** is required to leave step 0 and is recorded; an **empty installation** creates no users, roles, containers or documents beyond the administrator; a **demo installation** creates ACME without `admin@acme.test`; **Remove demo data** leaves an empty installation and the administrator; **no model is downloaded** unless the Download action is called (checked against the real Ollama; a real download is exercised only with a small model the owner approves).
- An **end-to-end browser test** that installs from scratch: run `erag init`, open the wizard, complete every step, and finally log in as two users with different roles and see different answers.
- The leakage and routing suites must stay green on a wizard-created installation.

## 10. Documentation

An **Installation guide** (prerequisites, `erag init`, starting the stack, opening the wizard, what each step asks, how to back up the master key and the database, upgrading), a short **troubleshooting** page driven by the environment checks, and a link from the white paper's how-to. Screenshots of the wizard in the README.

## 11. Releases and effort

| Release | Contents | Estimate (full-time) |
|---|---|---|
| R10 | `erag init`, setup mode, setup token, license acceptance, the environment checks, administrator and MFA, organization | ~1.5 weeks |
| R11 | Installation type and demo install/removal, models step with explicit Ollama download, manual access-structure guidance, people with forced password change, CSV import | ~1.5 weeks |
| R12 | Knowledge step, "Does it work?", review and report, setup checklist, setup assistant, installation guide, end-to-end test | ~1.5 weeks |
| **Total** | | **about 4.5 weeks (roughly 2 months part-time)** |

## 12. Decisions taken (2026-11-09)

1. **No starter templates.** The administrator builds the structure by hand. The only pre-built structure is the ACME Corp demo, installed only if "Demo with mock data" is chosen (users, roles, containers, documents and tools).
2. **Forced password change** at first login for users created in the wizard (and for any user created with a temporary password).
3. **Ollama models can be downloaded from the app, on explicit administrator action only**; nothing downloads automatically.
4. **The license screen has an acceptance checkbox**, recorded but not enforced technically.

Assumptions I am making (tell me if you disagree):
- **Language:** English only for now, with interface texts kept in one place so translations can be added later.
- **Demo data can be removed from the app** by the administrator, with a confirmation, since the demo is opt-in.
- The demo does not create `admin@acme.test`; the administrator is the person who ran the wizard.
