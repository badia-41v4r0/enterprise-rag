#!/bin/sh
# Thin wrapper around `erag init` (PLAN-v0.4 section 3.1): prepares deploy/.env with strong random secrets, the
# production settings, a master key (with an owner-only backup copy) and a one-time setup token, after checking
# Docker, Compose, Ollama and the port. Arguments are passed through (see `uv run erag init --help`).
#
#   deploy/install.sh                     # then: docker compose ... up -d --build (printed at the end)
#   deploy/install.sh --app-port 8080 --https
set -eu
cd "$(dirname "$0")/.."
if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required (https://docs.astral.sh/uv/): it installs Python 3.12 and the dependencies for erag." >&2
  exit 1
fi
umask 077
exec uv run erag init "$@"
