# 0008. Testing without fake providers

**Status:** Accepted (2026-11-08)

## Decision
- No fake or mock LLM/embedding providers. Security tests assert on what the backend decides and sends: allowed chunk IDs, built prompts, outgoing request payloads, tool calls allowed or refused, audit entries. Never on model prose.
- Tests that need no model (permission logic, filtered vector search with explicit test vectors, ACL re-check, context building, prompt building, routing policy, MCP gateway against the real reference servers, token tampering/expiry) always run, including in CI.
- Markers:
  - `db`: needs the Postgres test DB (skips if unreachable).
  - `embeddings`: needs real `bge-m3` in Ollama (skips if unreachable). The ACME canary matrix runs end to end with real embeddings when available.
  - `provider_ollama`: real local chat model (skips if unreachable). Free, so it runs whenever Ollama is up.
  - `provider_openrouter`: skips without `ERAG_OPENROUTER_API_KEY`.
  - `provider_claude_code`: opt-in with `ERAG_RUN_CLAUDE_CODE_TESTS=1` (subscription limits).
- **Egress assertions without a cloud key.** The routing test runs the real OpenRouter provider code with an egress hook that records every request and blocks it before it leaves the machine (a network guard, not a fake model: nothing ever answers). It asserts that confidential context is never handed to the cloud provider. With a real key the same test also runs unblocked.
- Explicit vectors in vector-store tests are test data, not a provider: they exercise the SQL filter, which does not depend on embedding quality.
