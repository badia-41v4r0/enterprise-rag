"""`erag init`: prepare a company installation before the first start (PLAN-v0.4 section 3.1, ADR 0034).

Creates the environment file Docker Compose reads (owner-only, mode 0600) with strong random secrets and the
production settings, a separate owner-only copy of the credential store master key for backup, and a one-time
setup token (printed once; only its SHA-256 goes into the environment file). Checks what it can see from the host
(Docker, Compose, Ollama and its models, the port) and says what is missing. Nothing is sent anywhere.

It never overwrites an existing environment file or master key: losing a master key means losing every stored
secret.
"""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import socket
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import REPO_ROOT

DEFAULT_ENV = REPO_ROOT / "deploy" / ".env"
DEFAULT_CHAT_MODEL = "qwen3:4b-instruct-2507-q4_K_M"
EMBED_MODEL = "bge-m3"


def add_init_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--env-file", default=str(DEFAULT_ENV), help=f"environment file to create (default {DEFAULT_ENV})")
    p.add_argument("--master-key-file", help="owner-only backup copy of the master key (default: next to the env file)")
    p.add_argument("--app-port", type=int, default=8000, help="port the web interface listens on (127.0.0.1)")
    p.add_argument("--postgres-port", type=int, default=55432, help="host port of PostgreSQL (127.0.0.1)")
    p.add_argument("--ollama-url", default="http://127.0.0.1:11434", help="Ollama as seen from this host")
    p.add_argument("--chat-model", default=DEFAULT_CHAT_MODEL, help="local chat model expected in Ollama")
    p.add_argument("--https", action="store_true", help="the app will be behind HTTPS (sets secure cookies)")
    p.add_argument("--public-url", help="address users will open (shown in the summary)")
    p.add_argument("--skip-checks", action="store_true", help="do not check Docker, Compose, Ollama and the port")


def _secret(n: int = 48) -> str:
    return secrets.token_urlsafe(n)


def _write_private(path: Path, text: str) -> None:
    """Create a file only its owner can read and write. Refuses to overwrite."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)


def _run(cmd: list[str]) -> bool:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=20, check=False).returncode == 0  # noqa: S603
    except (OSError, subprocess.SubprocessError):
        return False


def prerequisite_checks(ollama_url: str, chat_model: str, app_port: int) -> list[tuple[bool, str, str]]:
    """(ok, what, how to fix) for what is visible from the host."""
    import httpx

    out: list[tuple[bool, str, str]] = []
    docker = shutil.which("docker")
    out.append(
        (
            bool(docker) and _run(["docker", "info"]),
            "Docker installed and running",
            "Install Docker Engine (https://docs.docker.com/engine/install/) and start it.",
        )
    )
    out.append(
        (
            bool(docker) and _run(["docker", "compose", "version"]),
            "Docker Compose v2",
            "Install the Docker Compose plugin (`docker compose version` must work).",
        )
    )
    names: set[str] = set()
    try:
        r = httpx.get(f"{ollama_url.rstrip('/')}/api/tags", timeout=3)
        r.raise_for_status()
        names = {str(m.get("name")) for m in r.json().get("models", [])}
        reachable = True
    except (httpx.HTTPError, ValueError):
        reachable = False
    out.append((reachable, f"Ollama reachable at {ollama_url}", "Install Ollama (https://ollama.com) and start it."))
    for model in (EMBED_MODEL, chat_model):
        ok = reachable and (model in names or f"{model}:latest" in names)
        out.append(
            (
                ok,
                f"Ollama model {model}",
                f"`ollama pull {model}`, or download it later from the setup wizard "
                "(step 5; nothing is downloaded without your confirmation).",
            )
        )
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", app_port))
            free = True
        except OSError:
            free = False
    out.append((free, f"Port {app_port} free on 127.0.0.1", "Choose another port with --app-port."))
    return out


def run_init(args: argparse.Namespace) -> int:
    from app.core.crypto import generate_key
    from app.services.setup import generate_token, token_hash

    env_file = Path(args.env_file).expanduser().resolve()
    key_file = (
        Path(args.master_key_file).expanduser().resolve() if args.master_key_file else (env_file.parent / "master.key")
    )
    for existing in (env_file, key_file):
        if existing.exists():
            print(
                f"{existing} already exists: refusing to overwrite it (an existing master key protects stored "
                "secrets). Move it away yourself if you really want a new installation."
            )
            return 1

    if not args.skip_checks:
        print("Checking prerequisites visible from this host:")
        problems = 0
        for ok, what, fix in prerequisite_checks(args.ollama_url, args.chat_model, args.app_port):
            print(f"  [{'ok' if ok else 'missing'}] {what}")
            if not ok:
                problems += 1
                print(f"            fix: {fix}")
        if problems:
            print(f"  {problems} item(s) to fix; the files are created anyway. The setup wizard checks again.\n")

    master_key = generate_key()
    token = generate_token()
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    hostnet = args.ollama_url.startswith(("http://127.0.0.1", "http://localhost"))
    lines = [
        f"# EnterpriseRAG environment, generated by `erag init` on {now}.",
        "# Owner-only (mode 0600). Never commit, print or share this file. Back up ERAG_SECRETS_KEY separately.",
        "ERAG_ENV=prod",
        "ERAG_SEED_DEMO=false",
        f"ERAG_SECRET_KEY={_secret()}",
        f"ERAG_MCP_CONTEXT_SECRET={_secret()}",
        f"ERAG_RUNNER_TOKEN={_secret()}",
        "# Credential store master key (ADR 0018). Losing it means re-entering every stored secret and MFA.",
        f"ERAG_SECRETS_KEY={master_key}",
        "POSTGRES_USER=erag",
        f"POSTGRES_PASSWORD={secrets.token_hex(24)}",
        "POSTGRES_DB=erag",
        f"POSTGRES_HOST_PORT={args.postgres_port}",
        f"APP_PORT={args.app_port}",
        f"ERAG_COOKIE_SECURE={'true' if args.https else 'false'}",
        "# Strictest default: only public data may reach cloud models (the wizard's organization step can change it).",
        "ERAG_CLOUD_MAX_CLASSIFICATION=public",
        f"ERAG_LOCAL_CHAT_MODEL={args.chat_model}",
        "# SHA-256 of the one-time setup token printed by `erag init` (the token itself is not stored).",
        f"ERAG_SETUP_TOKEN_SHA256={token_hash(token)}",
    ]
    if not hostnet:
        lines.append(f"ERAG_OLLAMA_BASE_URL={args.ollama_url}")
    _write_private(env_file, "\n".join(lines) + "\n")
    _write_private(key_file, master_key + "\n")
    # Sanity check: both files are owner-only.
    for f in (env_file, key_file):
        os.chmod(f, 0o600)

    compose = "docker compose -f deploy/docker-compose.yml"
    if hostnet:
        compose += " -f deploy/docker-compose.hostnet.yml"
    if env_file != DEFAULT_ENV:
        compose += f" --env-file {env_file}"
    url = args.public_url or f"http://127.0.0.1:{args.app_port}/"
    print(f"Created {env_file} (mode 0600): production settings and strong random secrets.")
    print(f"Created {key_file} (mode 0600): a copy of the credential store master key.")
    print()
    print("BACK UP THE MASTER KEY NOW: copy " + str(key_file) + " to a safe place outside this machine (a password")
    print("manager or an offline medium). Without it, stored provider keys, connector secrets and MFA enrolments")
    print("cannot be decrypted after a restore. Then you may delete the copy on this machine.")
    print()
    print("Start EnterpriseRAG:")
    print(f"  {compose} up -d --build")
    print()
    print(f"Then open {url} and enter the setup token:")
    print()
    print(f"    {token}")
    print()
    print("It works once, for 24 hours after the first start, and stops working when the setup is finished.")
    print("Keep the port on localhost or a private network until the setup is finished.")
    return 0
