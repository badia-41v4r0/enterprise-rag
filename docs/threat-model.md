# Threat model

Scope: a single-tenant installation with administrators, delegated content managers, employees with different roles, local and optional cloud models, and MCP tools. The goal is that **every piece of data a model sees, and every answer a user receives, was already authorized for that user**.

## Assets

Document contents (by classification), tool data (HR cases, invoices, tickets), conversation history, credentials and sessions, the MCP context secret, cloud API keys.

## Actors

- Authenticated employees with limited roles (the main threat: curiosity, social engineering of the model).
- Authors of documents and of tool data (indirect prompt injection).
- The model itself (untrusted: it can be manipulated, it can hallucinate tool calls and arguments).
- Cloud model providers (must never receive confidential data).
- A network attacker against the browser session (CSRF, XSS, clickjacking).

## Threats and controls

| # | Threat | Control | Test evidence |
|---|---|---|---|
| T1 | User retrieves chunks from a container they cannot read | ACL filter inside the pgvector SQL; re-check after search | `test_vector_filter.py` (exact-match vector still filtered), canary matrix |
| T2 | Restricted document leaks to users with container access only | Document-level grant required in filter and re-check | `test_restricted_document_needs_document_grant`, Executive positive control |
| T3 | Revoked access still works (stale ACL in an index) | ACL resolved per request, never stored in vectors | revocation tests (vector level and through the admin API) |
| T4 | Earlier answers in a conversation leak after revocation | History re-validation withholds answers citing unreadable documents | `test_role_removal_is_immediate_and_history_is_withheld` |
| T5 | Confidential context sent to a cloud model | Router after retrieval; provider-level clearance check; cloud can never be cleared for confidential; egress audit | `test_routing.py` (egress recorded and blocked), canary matrix routing assertion |
| T6 | Tool result raises the sensitivity mid-conversation | Classification recomputed after each tool result; re-route or refuse before sending | `test_reroute_after_confidential_tool_result` |
| T7 | Model calls a tool the user may not use | Only granted tools are offered; hidden tools get the same "unknown tool" refusal | `test_model_only_sees_permitted_tools`, `test_hidden_tool_called_by_name_is_refused_like_unknown` |
| T8 | Forged arguments widen tool scope | Strict schema (`additionalProperties: false`), reserved names rejected, resource checks; servers take scope only from the signed token | `test_forged_arguments_are_refused`, `test_file_scope` |
| T9 | Forged, replayed or misdirected permission context | HS256 per-server key, audience, tool, 60 s expiry, issuer | `test_bad_permission_tokens_are_rejected` |
| T10 | Server bug or bypass of the gateway checks | Server enforces the token scope independently | `test_server_enforces_token_scope_even_if_authz_is_bypassed` |
| T11 | Indirect prompt injection in documents | Retrieved text is delimited and neutralized (cannot close our tags); system rules declare it untrusted; the model only has the user's own tools and data | `test_untrusted_text_cannot_break_delimiters`, injection document in the canary matrix |
| T12 | Indirect prompt injection in tool results | Validator drops instruction-like strings and flags them; results delimited | `test_injection_in_tool_result_is_neutralized` |
| T13 | Oversized tool results / resource exhaustion | Size cap, tool and chat rate limits, upload size limit, timeouts | `test_oversized_result_is_refused`, `test_tool_rate_limit`, `test_login_rate_limit` |
| T14 | Session theft / CSRF / clickjacking | HttpOnly SameSite=Strict cookie, hashed session IDs, CSRF header on mutations, CSP `frame-ancestors 'none'`, X-Frame-Options | `test_identity.py`, `test_security_headers` |
| T15 | Admin silently reads everything | Admin is a management privilege; data access only via audited grants | ADR 0009, citation-endpoint test |
| T16 | Weak secrets in production | App refuses to start in `prod` with dev secrets | `test_prod_refuses_dev_secrets` |
| T17 | Path traversal via uploads or tools | Filenames reduced to basenames; blob keys are hashes; `read_file` rejects `..`/absolute paths and checks the root | upload and `test_file_scope` tests |
| T18 | A content manager or contributor escalates (changes grants, classification, other containers, reads restricted content by un-restricting it or through upload deduplication) | Grants/classification/roles stay admin-only; `contribute`/`manage` scoped to one container; lifting `restricted` requires read access; dedupe ignores unreadable restricted documents (ADR 0017) | `tests/security/test_delegation.py` |
| T19 | Database dump or backup leaks credentials (OpenRouter key, connector and MCP tokens, MFA secrets) | AES-256-GCM per value, name/owner bound as associated data, master key outside the database; write-only API; every use audited with its consumer (ADR 0018) | `test_secret_api_is_write_only_audited_and_never_stored_in_clear`, `test_cipher_binds_name_and_key`, `test_master_key_rotation` |
| T20 | Stolen password; hijacked admin session used for sensitive changes | Optional TOTP with replay protection, attempt limits and single-use recovery codes; pending sessions grant nothing; step-up (password or TOTP within 5 min) for secrets, providers, admin promotion and MFA resets (ADR 0019) | `test_mfa_enrolment_two_step_login_replay_and_recovery`, `test_mfa_attempts_are_limited_and_step_up_needs_totp`, `test_sensitive_actions_need_recent_reauthentication` |
| T21 | A folder connector reads outside its folder (`..`, absolute paths, symlinks, races) | Folder must resolve inside `ERAG_CONNECTOR_ROOTS` (read-only mount); symlinks never followed; `O_NOFOLLOW` open and re-check (ADR 0020) | `test_folder_must_resolve_inside_an_allowed_root`, `test_folder_sync_skips_symlinks_and_handles_update_delete_and_resync` |
| T22 | Server-side request forgery through a web connector (cloud metadata, loopback services, private network, redirects, DNS rebinding) | Public addresses only unless operator-allowlisted; connection pinned to the checked IP; each redirect re-checked; credentials dropped across hosts; body cap (ADR 0020) | `test_ssrf_destinations_are_refused`, `test_ssrf_allowlist_is_exact`, `test_web_connector_crawls_allowlisted_server_and_blocks_redirect_escape` |
| T23 | Connector content reaches people the source system would not show it to | Documents inherit the target container's access; warning in the UI and in the dry run. **Not solved:** source ACLs are not mirrored (known boundary) | `test_imported_documents_answer_only_for_readers_of_the_container` |
| T24 | Confidential conversation data sent to a third-party MCP server as tool arguments | Per-server clearance (`max_classification`, default public): tools above the conversation's classification are not offered and calls are refused (ADR 0021) | `test_clearance_refuses_calls_above_the_server_ceiling` |
| T25 | Third-party tool output treated as low-sensitivity | Every result labeled (default confidential) and validated; routing follows the label | `test_register_approve_two_tools_for_one_role` |
| T26 | Rug pull / tool poisoning (a server changes a tool after approval, or ships instructions in descriptions) | Tools start disabled; approval pins a hash; live definition checked on every call; changes disable and flag the tool; descriptions sanitized and withheld when instruction-like | `test_rug_pull_disables_the_tool_until_re_approved` |
| T27 | Model abuses a third-party tool's arguments | Strict schema, admin constraints (enum, pattern, bounds, forced values) enforced by the gateway; per-user and per-tool rate limits; result size cap | `test_argument_constraints_are_enforced` |
| T28 | Registering an internal URL as an "MCP server" (SSRF) | Same guard as connectors; step-up to register | `test_registration_needs_a_safe_url_and_step_up` |
| T29 | A malicious or compromised MCP server attacks the infrastructure (database, app, other MCPs, internet, host) | Isolated runner: approved images pinned by ID, network `none` (Unix socket only), read-only root, no capabilities, no-new-privileges, seccomp, non-root, resource limits; the app has no Docker socket (ADR 0022) | `test_mcp_in_the_runner_is_confined_and_callable_through_the_gateway`, `test_runner_api_needs_its_token_and_allowlisted_pinned_images` |
| T30 | One MCP obtains another MCP's credentials | Secrets resolved per server from the vault (assigned only), injected only into that container, redacted from logs | same test (secret present in A, absent in B), `test_log_redaction` |
| T31 | An admin-added provider receives confidential data (admin raises the clearance, a public endpoint is declared "local", a cloud model is made the default) | Locality derived from the resolved address, never chosen; clearance capped by locality on save, at router build and in the provider (`check_clearance` before every request); new non-local providers start at `public`; the default is used only when cleared, otherwise a cleared (local) model answers (ADR 0023) | `test_create_providers_with_locality_clearance_and_defaults`, `test_tampered_clearance_is_capped_at_build_time`, `test_default_model_respects_clearance_and_cannot_be_removed`, `test_registry_cloud_default_gets_internal_but_never_confidential`, `test_cloud_default_from_the_registry_never_gets_confidential_context` |
| T32 | A provider URL used as an SSRF vector (metadata endpoint, loopback services, internal network, redirects, DNS change after save) | Address classes; private addresses only for Ollama or self-hosted providers created with step-up; metadata/unspecified/multicast/reserved never; HTTPS for public endpoints; re-checked and pinned on every request, a local provider must stay private; redirects never followed (ADR 0025) | `test_locality_comes_from_the_address`, `test_cloud_policy_provider_cannot_reach_a_private_socket`, `test_local_provider_that_resolves_public_is_refused_at_request_time`, `test_redirects_from_a_model_endpoint_are_not_followed`, `test_discovery_reports_http_errors_and_never_follows_redirects` |
| T33 | Provider API keys leak (API responses, logs, audit, database dump) | Keys stored in the credential store as `provider.<slug>.api_key`, write-only (only the key source is shown), `secret.use` audited per provider, deleted with the provider; audit records `api_key_changed`, never the value (ADR 0018, 0023) | `test_api_key_is_write_only_in_the_vault_and_audited` |
| T34 | A paid provider (or one that does not report cost) runs up an unbounded bill | Monthly ceiling per provider (env cap, UI only lowers) fed by `usage.cost`; daily request cap counted before sending, for every API provider and non-local Ollama; OpenRouter free-only rule unchanged (ADR 0016, 0024) | `test_daily_request_cap_counts_before_sending_and_resets_per_day`, `test_effective_limits_can_only_be_lowered`, `test_add_ollama_and_api_providers_discover_set_default_and_answer` |
| T35 | A hijacked admin session silently redirects chat traffic to an attacker's endpoint | Step-up for creating, deleting, changing URL/key/clearance and changing the default; everything audited; the attacker's endpoint is still bounded by clearance (public by default if not local) | `test_sensitive_provider_actions_need_step_up`, `test_sensitive_actions_need_recent_reauthentication` |

## Residual risks (honest limits)

- **The model can still be wrong** or misquote what it was legitimately given. Authorization limits what it can see, not what it says about it.
- **Legitimate data can leak through the authorized user**, who can copy it anywhere. Out of scope.
- **Prompt-injection heuristics are not complete.** A cleverly phrased instruction can pass the validator; the structural controls (T1 to T10) are what bound the damage: an injected model can only use the user's own tools on the user's own data.
- **Classification is per container**, not per paragraph. Mislabelled containers lead to wrong routing.
- **Admin-added cloud providers are trusted with what their clearance allows.** Their data handling (retention, training, logging) is not verified by the software; a provider cleared for `internal` receives internal data. Locality cannot be overridden: a public endpoint with a zero-retention contract is still "cloud".
- **Spend on providers that do not report cost** is bounded by the daily request cap, not by money; a very expensive model can still cost "cap x price per request" per day.
- **Cloud egress of internal data is allowed by default** (`ERAG_CLOUD_MAX_CLASSIFICATION=internal`). Set it to `public` to keep internal data local too.
- **In-process rate limiters** reset on restart and do not work across replicas (single-instance MVP).
- **Audit log is in the same database** and writable by the app; no tamper evidence.
- **Third-party MCP output cannot be filtered per user**: we do not know the ACLs of the system behind it. It is access-controlled per tool and role, labeled, and kept on cleared models, nothing more.
- **Container isolation is not a VM.** A kernel or engine escape from an MCP container would reach the host; the runner and its Docker socket are root-equivalent. No allowlisted egress for runner MCPs (network `none` only); Podman is untested.
- **Built-in reference MCP servers run as subprocesses of the app** with the app's OS user. Remote servers run wherever their operator runs them; containment is only provided by the isolated runner (R5).
- **The credential store master key sits on the same host as the app.** It protects against database and backup leaks, not against compromise of that host. Losing it loses the secrets and MFA enrolments.
- **Connectors do not mirror source permissions.** A folder or site whose audience is narrower than the container's leaks to every reader of that container. Point connectors only at content whose audience matches the container.
- **MFA is optional**, also for admins. Admin account compromise remains the largest risk; enable MFA for admins.
