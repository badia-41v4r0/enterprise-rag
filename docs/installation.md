# Installation guide

This guide installs EnterpriseRAG for a company on one Linux server with Docker Compose, from an empty machine to a
working, secure system, in about half an hour. It uses the installer command `erag init` and the setup wizard in the
web interface. Problems that the wizard's environment check reports are explained in
[troubleshooting](troubleshooting.md).

EnterpriseRAG is source-available under the PolyForm Noncommercial License 1.0.0. Running it inside a company for
business purposes needs a commercial license from Badia Partners (https://badiapartners.com); a 60-day evaluation
permit covers testing. See the [README](../README.md#license).

## 1. Prerequisites

- Linux with **Docker Engine** and the **Compose v2** plugin (`docker compose version`).
- **Ollama** on the host (https://ollama.com), listening on `127.0.0.1:11434` (the default). The models can be
  downloaded from the wizard later (nothing is downloaded without your confirmation), or now:

  ```bash
  ollama pull bge-m3                           # embeddings, 1.2 GB, MIT
  ollama pull qwen3:4b-instruct-2507-q4_K_M    # local chat model, 2.5 GB, Apache-2.0
  ```
- **uv** (https://docs.astral.sh/uv/) to run `erag init` (it installs Python 3.12 and the dependencies itself).
- About 8 GB of RAM for the containers and the two models; a GPU with 6 GB or more makes answers much faster.
- The source tree: `git clone https://github.com/badia-41v4r0/enterprise-rag && cd enterprise-rag`.

## 2. `erag init`

```bash
deploy/install.sh                 # same as: uv run erag init
# options: --app-port 8080  --https  --public-url https://rag.example.com  --ollama-url http://10.0.0.5:11434
```

It checks Docker, Compose, Ollama, the two models and the port, then creates:

- `deploy/.env` (mode 0600): `ERAG_ENV=prod`, `ERAG_SEED_DEMO=false`, strong random values for the session secret,
  the MCP context secret, the runner token and the database password, the credential store **master key**
  (`ERAG_SECRETS_KEY`), the strictest cloud ceiling (`public`) and the SHA-256 of the setup token.
- `deploy/master.key` (mode 0600): a copy of the master key **to back up** (see section 6).

It prints the **setup token** (once; only its hash is stored), the Compose command and the URL. It never overwrites an
existing `deploy/.env` or master key.

## 3. Start

```bash
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.hostnet.yml up -d --build
```

The `hostnet` override lets the containers reach an Ollama bound to `127.0.0.1`. If Ollama listens on all interfaces
(`OLLAMA_HOST=0.0.0.0`), `docker compose up -d --build` from the repository root is enough. The first start creates
the database, enters **setup mode** and logs:

```
EnterpriseRAG is in SETUP MODE: open the web interface to run the setup wizard.
```

In setup mode only `/health` and the wizard answer; every other API answers `503 setup required`. Keep the port on
localhost or a private network until the setup is finished.

## 4. The setup wizard

Open the URL and enter the setup token. Lost it? `docker compose ... exec app erag setup --new-token` prints a new one
(the old one stops working). Without `erag init`, a token is generated at the first start and written to the app log
(`docker compose ... logs app | grep "Setup token"`) and to `/data/setup-token` inside the app container.

Progress is saved on the server: you can close the tab and come back (enter the token again and sign in).

| # | Step | What you do |
|---|---|---|
| 0 | Welcome and license | Read the plain-language license notice, tick the acceptance box, enter your name. Recorded (who, when, a hash of the text); nothing is sent anywhere. |
| 1 | Environment check | Green, amber or red for: database and migrations, master key, production mode, the three secrets, HTTPS, storage, the worker, Ollama with the embedding and a chat model, the runner (optional). Each problem shows the exact fix. The wizard never edits the environment file. |
| 2 | Administrator | Email, name and a strong password (12 or more characters). Then enroll MFA right away (recommended): scan the QR code, confirm a code, store the recovery codes. |
| 3 | Installation type | **Empty** (default): nothing is created. **Demo with mock data**: the fictional ACME Corp (people with a published demo password, roles, containers, documents, tools); your account stays the only administrator. Removable later. |
| 4 | Organization | Company name (shown in the header) and what may leave the building: `public` only (default) or also `internal` data to approved cloud providers. Confidential never leaves. |
| 5 | Models | The local Ollama and its models; pick the chat model, confirm the embedding model (locked once documents are indexed), Test both. Download a model only by pressing Download: a confirmation shows its name, size and that it comes from the Ollama registry, with progress and a cancel button. Optionally add a cloud provider (key to the credential store, cleared for public data, monthly budget). |
| 6 | Access structure | Create containers (each with a classification), roles, and grants in the matrix, with an effective-permissions preview. No templates: you build what your company needs. |
| 7 | People | Add people one by one or paste a CSV (`email,name,role`, roles separated by `;`); bad rows are reported by line. Each gets a temporary password (shown once) and must change it at the first login. |
| 8 | Knowledge | Upload documents into a container, or add a folder or web connector; ingestion progress is live. |
| 9 | Does it work? | Ask a question as a chosen user: their permissions, the sources, the model and the classification (and, during setup, the answer). |
| 10 | Tools and router | Optional; both stay off or empty. Set them up later in Admin > MCP registry and Admin > Model router. |
| 11 | Review and finish | Summary, a security checklist, the setup report (Markdown, no secrets) for your IT records, then **Finish**: setup mode ends, the token is destroyed, and you are signed in. A production setup cannot be finished while an administrator has a known or temporary password. |

Skipped steps leave the stricter setting (no cloud provider, router off, no connectors, `public` cloud ceiling).

## 5. After the wizard

- The **setup checklist** card at the top of the admin area lists what is still recommended: MFA, the master key
  backup, a second administrator, provider budgets, reviewing the router, removing demo data.
- **Admin > Setup assistant** re-opens any step with your normal admin session (changes that matter for security,
  such as the cloud ceiling, ask you to confirm your password or code again).
- For HTTPS, put a reverse proxy with TLS in front of the app port, re-run with `ERAG_COOKIE_SECURE=true` in
  `deploy/.env`, and restart.

## 6. Backups

- **Master key**: copy `deploy/master.key` to a password manager or an offline medium, then delete it from the server
  (the key also lives in `deploy/.env`). Without it, stored provider keys, connector secrets and MFA enrolments cannot
  be decrypted after a restore.
- **Database** (users, permissions, documents' text and vectors, audit log):

  ```bash
  docker compose -f deploy/docker-compose.yml exec -T postgres pg_dump -U erag erag | gzip > erag-$(date +%F).sql.gz
  ```
- **Uploaded files**: the `appdata` volume (`/data/blobs` in the app container), for example
  `docker run --rm -v enterprise-rag_appdata:/data -v "$PWD":/out alpine tar czf /out/appdata.tgz /data`.
- Keep `deploy/.env` with the backups (it holds the database password and the secrets), protected like the key.

## 7. Upgrading

```bash
git pull
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.hostnet.yml up -d --build
```

The app runs the database migrations at start. An existing installation is never put into setup mode by an upgrade:
if the database already has users, the setup is recorded as completed.

## 8. Server-side commands

| Command (inside the app container: `docker compose ... exec app ...`) | Purpose |
|---|---|
| `erag setup --status` | Is setup running or completed? |
| `erag setup --new-token` | Issue a new setup token while setup runs (the old one stops working). |
| `erag setup --reset --yes` | Re-open the wizard (only administrators can use the app until it is finished again). |
| `erag create-admin --email ...` | Create an administrator on the server; on a pending setup this completes it (headless installation). |
| `erag reset-mfa --email ...` | Remove a user's second factor (lost device). |
| `erag secrets rotate-master --new-key-file F` | Re-encrypt every stored secret with a new master key. |

## Demo for development

`ERAG_SEED_DEMO=true docker compose ... up -d --build` (not in production) seeds the full ACME demo including
`admin@acme.test` and skips the wizard, for development and `scripts/demo_scenarios.py`.
