# Threat model (MVP)

Scope: a single-tenant installation with one administrator, employees with different roles, local and optional cloud models, and MCP tools. The goal is that **every piece of data a model sees, and every answer a user receives, was already authorized for that user**.

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

## Residual risks (honest limits)

- **The model can still be wrong** or misquote what it was legitimately given. Authorization limits what it can see, not what it says about it.
- **Legitimate data can leak through the authorized user**, who can copy it anywhere. Out of scope.
- **Prompt-injection heuristics are not complete.** A cleverly phrased instruction can pass the validator; the structural controls (T1 to T10) are what bound the damage: an injected model can only use the user's own tools on the user's own data.
- **Classification is per container**, not per paragraph. Mislabelled containers lead to wrong routing.
- **Cloud egress of internal data is allowed by default** (`ERAG_CLOUD_MAX_CLASSIFICATION=internal`). Set it to `public` to keep internal data local too.
- **In-process rate limiters** reset on restart and do not work across replicas (single-instance MVP).
- **Audit log is in the same database** and writable by the app; no tamper evidence.
- **MCP servers run as subprocesses of the app** with the app's OS user; a malicious server binary would not be contained. Only the bundled reference servers are registered.
- **The credential store master key sits on the same host as the app.** It protects against database and backup leaks, not against compromise of that host. Losing it loses the secrets and MFA enrolments.
- **MFA is optional**, also for admins. Admin account compromise remains the largest risk; enable MFA for admins.
