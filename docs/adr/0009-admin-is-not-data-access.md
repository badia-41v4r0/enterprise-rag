# 0009. Admin is a management privilege, not a data-access privilege

**Status:** Accepted (2026-11-08) — **Owner review**

## Context
PLAN.md: "admin bypasses via explicit admin checks, never via the model". It does not say whether the admin can read every container in chat.

## Decision
`is_admin` grants the management API (users, roles, containers, grants, uploads, tools, audit). It does **not** add containers or tools to `effective_permissions`. In chat and retrieval the admin is a normal user whose access comes from grants; an admin who needs to read a container grants it to one of their roles, which is audited.

## Consequences
Least privilege by default, and admin data access is visible in the grants table. The ACME "Executive" persona has read on every container; "Admin" manages but sees only what its role grants.
