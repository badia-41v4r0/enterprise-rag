# Contributing

Thank you for your interest. **External pull requests are not accepted yet.** EnterpriseRAG is source-available under the PolyForm Noncommercial License 1.0.0 and is also licensed commercially by its author; accepting outside code requires a contributor license agreement (CLA), which does not exist yet.

What you can do today:

- Open an issue for bugs, questions or ideas (use the templates).
- Report vulnerabilities privately, see [`SECURITY.md`](SECURITY.md).

## Development notes (for the maintainer)

- Python 3.12 with `uv`; `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest`.
- Frontend: `cd frontend && npm ci && npm run build`.
- Dependencies must be permissive (MIT/BSD/Apache/ISC/MPL); `uv run python scripts/check_licenses.py` fails on copyleft.
- No fake or mock LLM providers in tests: assert on retrieved chunk IDs, built prompts, outgoing requests and tool decisions (ADR 0008).
- Record non-obvious decisions as ADRs in `docs/adr/`.
