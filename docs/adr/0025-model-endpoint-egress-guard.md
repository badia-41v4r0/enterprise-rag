# 0025. Egress guard for model endpoints: address classes, request-time re-validation, no redirects

**Status:** Accepted (2026-11-10)

## Context
A provider base URL entered by an admin is an outbound data path that carries prompts with retrieved context. Unlike web connectors and MCP servers (ADR 0020), private addresses are legitimate for models (a LAN Ollama, a self-hosted vLLM), and the address also decides the locality and so the clearance (ADR 0023). A check at save time alone is not enough: DNS can change, and a database row can be edited.

## Decision
- **Address classes** (`netguard.classify_ip`): *public*; *private* = loopback, RFC 1918, IPv6 ULA, link-local, CGNAT `100.64.0.0/10` (IPv4-mapped IPv6 is unwrapped first); *never* = unspecified (`0.0.0.0`, `::`, `0.0.0.0/8`), multicast, reserved ranges (`240.0.0.0/4` and similar) and cloud metadata endpoints (`169.254.169.254`, `fd00:ec2::254`), even though the latter is link-local.
- **Who may use private addresses:** only `ollama` providers and `api` providers explicitly marked **self-hosted**, both created or edited with step-up. Everything else must resolve to public addresses only. The operator SSRF allowlist (`ERAG_SSRF_ALLOWLIST`) is not used for model providers: the "self-hosted" flag plus step-up is the explicit decision.
- **HTTPS for public endpoints:** a non-built-in provider whose address is public must use `https` (prompts and keys would otherwise cross the internet in clear). Built-in URLs come from the environment and are not checked for this.
- **Request-time re-validation.** Every chat request of `OpenAICompatProvider` (built-in or not) and every test/discovery request goes through `PinnedTransport` with a provider checker: the host is resolved again, every address is classified, the policy above is applied, and a provider that was cleared as *local* must still resolve to private addresses only (otherwise the request is refused before connecting, so confidential context can never reach a public address through a DNS change). The connection is pinned to the checked addresses (tried in resolver order, so `localhost` works with `::1` and `127.0.0.1`), with the original `Host` header and TLS SNI, and certificates verified.
- **No redirects.** Model and discovery requests never follow redirects; a 3xx is an error. Discovery responses are capped at 2 MB.
- Refusals surface as `EgressBlocked` (a `ProviderError`), so the chat shows a model error and nothing is sent. Network errors are wrapped in `ProviderError` as well (before, a connection error ended the chat stream with an internal error).

## Consequences
- The `emit_egress` hook and `check_clearance` still run first for every request, then the destination guard; the egress audit (`llm.request`) is unchanged.
- An Ollama reachable only through a public hostname is a cloud provider, including the built-in one if `ERAG_OLLAMA_BASE_URL` points at a public address (previously it was always treated as local). This is a deliberate behaviour change.
- Tests: `test_locality_comes_from_the_address` (24 URLs), `test_cloud_policy_provider_cannot_reach_a_private_socket` and the redirect tests (real listening sockets, zero connections reach the target), `test_local_provider_that_resolves_public_is_refused_at_request_time`, `test_tampered_clearance_is_capped_at_build_time`.
