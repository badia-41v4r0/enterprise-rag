# 0006. Ollama on the host by default; bundled Ollama as a profile

**Status:** Accepted (2026-11-08) — **Owner review**

## Context
The reference machine already runs Ollama on the host (`/usr/local/bin/ollama`, models `bge-m3` and the local chat model pulled there), bound to `127.0.0.1:11434`. Docker has the NVIDIA CDI devices, but a second Ollama in a container would need its own copy of every model (several GB) and compete for 6 GB of VRAM.

## Decision
- Default: Ollama runs on the host; the app reaches it via `ERAG_OLLAMA_BASE_URL`.
- `deploy/docker-compose.hostnet.yml` puts `app` and `worker` on the host network so they reach a host Ollama bound to `127.0.0.1` (the default for a host install). Without it, the default compose file uses `host.docker.internal`, which works when Ollama listens on `0.0.0.0` (`OLLAMA_HOST=0.0.0.0`).
- A bundled `ollama` service exists behind the `ollama` compose profile (GPU through CDI `nvidia.com/gpu=all`). It is not exercised on the reference machine, to avoid duplicating models.

## Consequences
On the reference machine, `docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.hostnet.yml up -d` is the tested path. The bundled profile is documented but **unverified**.
