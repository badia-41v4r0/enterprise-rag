# Troubleshooting

Driven by the setup wizard's environment check (step 1, also in Admin > Setup assistant). Each item below is one
check; the wizard shows the same fix. Commands assume the Compose files of the [installation guide](installation.md);
`dc` stands for `docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.hostnet.yml`.

| Check | Symptom | Fix |
|---|---|---|
| **Database reachable and migrations current** | red: "cannot be queried" or "Database at migration X, code expects Y" | `dc ps` (is `postgres` healthy?), `dc logs postgres`. The app migrates at start: `dc restart app`. A wrong `POSTGRES_PASSWORD` after re-running `erag init` on an old volume: restore the old `deploy/.env`. |
| **Master key present** | red: "cannot be loaded" | Production needs `ERAG_SECRETS_KEY` (written by `erag init`) or `ERAG_SECRETS_KEY_FILE`. Restore it from your backup; a new key cannot decrypt existing secrets. Amber "readable by other users": `chmod 600` the key file. |
| **Production mode** | amber | `ERAG_ENV=prod` in `deploy/.env`, then `dc up -d`. |
| **Secrets not the development defaults** | red, naming `ERAG_SECRET_KEY`, `ERAG_MCP_CONTEXT_SECRET` or `ERAG_RUNNER_TOKEN` | Put strong values (`openssl rand -hex 32`) in `deploy/.env` and `dc up -d`. In production the app refuses to start with them anyway. |
| **HTTPS and secure cookies** | amber on localhost; red when reached from another machine over plain HTTP | Put a TLS reverse proxy in front of the app port, forward `X-Forwarded-Proto`, set `ERAG_COOKIE_SECURE=true`. Until then use the wizard from the server itself (an SSH tunnel: `ssh -L 8000:127.0.0.1:8000 server`). |
| **Document storage writable** | red: "Cannot write to /data/blobs" | The `appdata` volume must be writable by uid 10001 (the app user). A bind mount owned by root: `chown -R 10001 <dir>`. |
| **Ingestion worker running** | red: "never reported" or "last seen N seconds ago" | `dc up -d worker`, then `dc logs worker`. It reports every 20 seconds. |
| **Ollama reachable** | red | `ollama serve` (or `systemctl start ollama`) on the host. With Ollama on `127.0.0.1` use the `hostnet` override; with the plain Compose file Ollama must listen on `0.0.0.0` and be reachable as `host.docker.internal`. Check `ERAG_OLLAMA_BASE_URL`. |
| **Embedding model present** | red: "bge-m3 is not installed" | Step 5 > Download (after a confirmation), or `ollama pull bge-m3` on the host. The embedding model must produce 1024-dimensional vectors; change `ERAG_EMBED_MODEL` only before the first document is indexed. |
| **Local chat model present** | red | Step 5: pick an installed model or download one; or `ollama pull qwen3:4b-instruct-2507-q4_K_M`. |
| **Isolated MCP runner (optional)** | amber: "configured but not reachable" | `dc up -d runner`, or remove `ERAG_RUNNER_URL` if you do not run MCP servers in containers. |

## Other problems

- **"setup required" (503) everywhere**: the installation is in setup mode; finish the wizard. Only administrators can
  use the API meanwhile.
- **Lost or expired setup token**: `dc exec app erag setup --new-token`. The token expires 24 hours after the first
  start (`ERAG_SETUP_TOKEN_TTL_HOURS`); a restart issues a new one when it has expired.
- **"Too many attempts; wait a minute"** on the token screen: the token is rate limited (10 per minute per address).
- **The wizard says "sign in as the administrator created in step 2"**: your browser session expired; sign in on that
  screen (with your authenticator code if you enrolled MFA).
- **"An administrator has a known or temporary password"** when finishing: that administrator must log in once and
  change the password (or be deactivated). In production the setup cannot be finished before.
- **A person sees "Choose your own password"**: their password was set by an administrator; they choose their own.
- **A model download fails**: the server needs HTTPS access to `registry.ollama.ai`; check `dc logs app` and Ollama's
  own log. A cancelled download may leave partial files that Ollama resumes next time.
- **Demo data in a real installation**: Admin > Setup assistant > Installation type > Remove demo data.
- **Re-run the wizard**: `dc exec app erag setup --reset --yes`, then open the web interface with the printed token.
