# 0022. Isolated runner for MCP containers (network `none`, Unix-socket transport)

**Status:** Accepted (2026-11-09), implements PLAN-v0.2 R5 (owner decision: Docker, engine-neutral interface). Allowlisted egress is **not** implemented.

## Context
Any MCP server may be registered (D1), but a third-party server is code we did not write. It must not be able to reach the database, the app, other MCPs or the internet, and the app must not hold the Docker socket (root-equivalent).

## Decision
- **Separate `runner` service** (`python -m app.runner.service`), the only component with the container engine socket. Narrow HTTP API authenticated with `ERAG_RUNNER_TOKEN` (constant-time compare; required to be strong in prod): `ensure` (start or reuse), `stop`, `status`, `logs`, `images`. Not published outside the Compose network (127.0.0.1 with the host-network override).
- **Engine-neutral interface** (`app/runner/engine.py`): the Docker Engine API v1.41 over its Unix socket with `httpx`. Podman serves a compatible API, so pointing `ERAG_RUNNER_DOCKER_SOCKET` at a Podman socket should work, but this is **not verified**.
- **Only approved images.** `ERAG_RUNNER_IMAGES` lists `name=sha256:<image id>`. The container is created from the pinned image ID (re-tagging another image with the same name changes nothing). No command override, entrypoint, privileged flag or extra capability can be requested: the API does not have those fields.
- **Hardening defaults:** user `10001:10001`, read-only root filesystem, 16 MB `noexec,nosuid,nodev` tmpfs on `/tmp`, `CapDrop: ALL`, `no-new-privileges`, the engine's default seccomp profile (never disabled), `--init`, CPU/memory (swap = memory)/PID limits set by the admin within runner maximums, restart policy `no`.
- **Network `none`.** The container has only a loopback interface. The gateway talks to the server with streamable HTTP over a **Unix socket** in a per-MCP directory, which is the only writable mount; admin-approved host paths (below `ERAG_RUNNER_MOUNT_ROOTS`) can be mounted read-only under `/data`. Each MCP sees only its own socket, so MCPs cannot reach each other; nothing in the container can reach Postgres, the app or the internet. This is stronger than a dedicated internal Docker network (which still leaves the host's bridge address reachable).
- **Secrets:** the app resolves exactly the vault secrets assigned to the server (audited as `mcp:<id>`) and sends them to the runner for that container only, as environment variables; the runner redacts their values (and generic `token=`/`Bearer` patterns) from the logs it returns.
- **Operations:** `ensure` reuses a running container when its configuration hash is unchanged; idle containers are stopped and removed by a reaper (per-server `idle_minutes`, default `ERAG_RUNNER_IDLE_MINUTES`=15) and restarted transparently on the next call. A runner only reaps containers it created (label with an instance id derived from its socket directory). Admin UI: start, stop, status (network mode, read-only root, capabilities, user, memory) and redacted logs.
- **Allowlisted egress (cut):** the per-MCP forward proxy permitting listed domains was the first item on the cut list and is **not implemented**. An MCP that needs the network today must be run elsewhere and registered as a remote HTTP server (ADR 0021), where only clearance, labels and the SSRF guard apply.

## Consequences
- MCP images must serve streamable HTTP on `/run/mcp/mcp.sock` (the reference servers do with `--uds`); arbitrary third-party images need a small wrapper image.
- Secret values in container environment variables are visible to anyone with Docker access on that host (root-equivalent anyway) and to the MCP itself, by design.
- Rootless Docker/Podman, user namespaces and gVisor-style sandboxes are not configured; they would further reduce the impact of a container escape.
- Tests (`-m docker`, real daemon): `tests/security/test_runner.py` probes from inside the container (no interfaces besides `lo`, no route to the internet, the host bridge or Postgres; read-only root; `CapEff` 0, `NoNewPrivs` 1, `Seccomp` 2; limits; only its own socket; secrets only where assigned; image allowlist and pinning; idle reaping) and calls the tool through the normal gateway path.
