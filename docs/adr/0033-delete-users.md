# 0033. Deleting users: deactivate first, step-up, what is kept

**Status:** Accepted (2026-11-09)

## Context
Until now a user could only be deactivated. People who leave a company, and the throw-away users created by the UI smoke test, piled up in the Users list. The smoke test could not clean them up.

## Decision
- `DELETE /api/admin/users/{id}` (admin, **step-up re-authentication**, audited as `admin.user.delete` with email, display name, roles and whether they were an admin).
- **Two deliberate steps:** the user must already be **deactivated** (409 otherwise). You cannot delete yourself (400).
- **What goes with the user** (database cascades): sessions, role memberships, personal quotas, MFA recovery codes, conversations and their messages.
- **What stays:** rows that only record who created something (documents versions, connectors, providers, MCP servers, deciders, lanes, secrets, quotas) keep the record with the creator unset; the **audit log** keeps its entries (it has no foreign key to users, by design); **token usage figures** keep their user id as a number only, so spend history and monthly totals do not change (they contain no text).
- **UI:** a *delete* button appears on deactivated users in Admin > Users, with a confirmation that names what is removed.
- The UI smoke test deactivates and then deletes its test users (and the leftovers of interrupted runs), so repeated runs leave nothing behind.

## Consequences
- Deleting is irreversible. Deactivation remains the right action when history matters (conversations are kept for a deactivated user).
- Per-user usage views for a deleted user are no longer reachable from the Users tab, although their usage still counts in provider totals.
- Tests: `tests/integration/test_user_delete.py` (real Postgres).
