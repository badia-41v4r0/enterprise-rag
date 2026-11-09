# 0037. Temporary passwords must be changed at the first login

**Status:** Accepted (2026-11-09), owner decision 2 of PLAN-v0.4

## Decision
- New column `users.must_change_password`. It is set for every user whose password was chosen by someone else:
  users created in the wizard (one by one or by CSV, where a random temporary password is generated and shown once),
  users created in Admin > Users, and users whose password an administrator resets (their sessions end).
- While it is set, `get_auth` refuses every endpoint with `403 password_change_required`, except `GET /api/auth/me`,
  `POST /api/auth/logout` and `POST /api/auth/change-password`; in setup mode such a session also counts as "not an
  administrator" for the gate. The UI shows only the change-password screen.
- `POST /api/auth/change-password` (current and new password; rate limited): the new password must differ, have 12
  or more characters, at least 5 different characters, not be a common or the demo password, and not contain the
  email address. Success ends every other session of the user and issues a fresh one; audited
  `auth.password.change` with `forced: true|false`, never the password.
- Not set for: the administrator created in step 2 and administrators created with `erag create-admin` (they chose
  their own password), the ACME demo personas (mock accounts with a published password; ADR 0036), and users
  created directly in the database by tests.
- An administrator with `must_change_password` counts as an administrator with a **known password**: a production
  setup cannot be finished while one exists (nor while an active administrator has the demo password).

## Consequences
- Temporary passwords are never stored or audited; the CSV import shows them once in its response.
- The UI smoke test now goes through the forced change for its test user.
- Tests: `test_temporary_passwords_must_be_changed_at_first_login`,
  `test_people_one_by_one_and_csv_with_per_row_errors`,
  `test_finish_refused_in_production_while_a_known_password_admin_exists`.
