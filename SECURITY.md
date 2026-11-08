# Security policy

EnterpriseRAG is a reference implementation of a permission-aware RAG + MCP stack. Security reports are welcome.

## Reporting a vulnerability

Please do not open a public issue for a vulnerability. Use GitHub's private vulnerability reporting ("Report a vulnerability" in the Security tab of the repository), or the contact form at https://badiapartners.com. Include steps to reproduce and the affected commit.

## Scope

In scope: anything that lets a user (or a document, or a tool result) cause data to reach a person or a model that is not authorized for it; authentication, session and CSRF flaws; permission-context forgery; classification-routing bypasses.

Known limits (documented, not vulnerabilities): see [`docs/threat-model.md`](docs/threat-model.md), section "Residual risks". In short, a model can still misstate or leak what it was legitimately given, and prompt-injection heuristics reduce but do not remove injection risk.

## Supported versions

Only the latest commit on `main` is supported while the project is pre-1.0.
