# 0019. Optional TOTP MFA and step-up re-authentication

**Status:** Accepted (2026-11-09), implements PLAN-v0.2 R2 and decision D6

## Decision
- **TOTP (RFC 6238)** with `pyotp` (MIT), optional for every user including admins. Enrolment from the Account page: a new secret is stored encrypted (ADR 0018) but inactive; the QR code (SVG made server-side with `segno`, BSD) and the manual key are shown once; a valid code activates MFA and returns 10 single-use recovery codes (40 bits each, stored as HMAC-SHA256 with the app secret, shown once).
- **Two-step login:** for users with MFA, a correct password creates a *pending* session (5 minutes) that every endpoint except `POST /api/auth/mfa/verify` rejects. A correct TOTP or recovery code replaces it with a new session (new cookie, no fixation). At most `ERAG_MFA_MAX_ATTEMPTS` (5) codes per pending session, plus a per-user rate limit; then the pending session is deleted.
- **No replay:** the highest accepted time step is stored per user; a code for that step or an earlier one is refused (window of one step either side for clock drift).
- **Step-up:** sessions carry `reauth_at`, set at login (after the second factor) and by `POST /api/auth/step-up` (password, or a TOTP/recovery code when MFA is on; the password alone is not accepted then). Actions that create credential, data-access or code-execution paths require `reauth_at` within `ERAG_STEP_UP_SECONDS` (300): secrets, provider settings, creating or promoting an admin, resetting someone's MFA, and (later releases) connectors and MCP registration. The API answers `403 step_up_required`; the UI then asks for the password or code and retries.
- **Recovery:** an admin can reset another user's MFA (step-up, audited, ends their sessions); an admin who lost their own device uses `erag reset-mfa --email ...` on the server.

## Consequences
- A fresh login counts as recent re-authentication, so the first five minutes after login need no extra prompt.
- Recovery codes and TOTP secrets are useless in a database dump without the app secret / master key.
- Disabling MFA requires a current code (or a recovery code).
- Tests: `tests/security/test_vault_and_mfa.py` (codes computed from the secret decrypted from the database, replay, recovery code once, attempt limit, step-up expiry and TOTP-only step-up, admin and CLI reset).
