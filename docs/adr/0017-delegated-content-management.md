# 0017. Delegated content management: `contribute` and `manage` on containers

**Status:** Accepted (2026-11-09), implements PLAN-v0.2 R1 and decision D5

## Context
Department heads should be able to maintain their own container's documents without becoming administrators. The plan asks for role templates *Reader*, *Contributor* and *Manager*, and says that a manager may upload, replace and delete documents and toggle `restricted`, but never change grants, classification or anything outside the container. The MVP only had `read` and `manage` grant actions.

## Decision
1. **Three container levels, each implying the previous one:** `read` < `contribute` < `manage`. A new enum value `contribute` (migration 0004) gives the *Contributor* template a meaning:
   - `contribute`: upload new documents and new versions (replace) of documents the user can read; retry failed ingestion.
   - `manage`: contribute, plus delete documents, upload restricted documents and set the `restricted` flag.
   - Neither level grants anything on grants, roles, users, classification or other containers: those routes stay `require_admin`.
2. **Lifting `restricted` requires read access to the document.** A manager may restrict any document in their container, but may only un-restrict one they can already read (document grant) or the admin does it. Otherwise un-restricting would give the manager, and every reader of the container, access the admin never granted. Consequence: a manager without a document grant who restricts a document cannot undo it.
3. **Managers see restricted documents' metadata, not their content.** The document list shows the title and status (needed to manage them) with `can_read: false`; chunks, citations and retrieval still require the document grant.
4. **Deduplication is not a read oracle.** Upload deduplication (same SHA-256 in the container) ignores restricted documents the uploader cannot read; otherwise a "duplicate" answer would confirm a guess of their content.
5. **Replacing by name follows the same rule as an explicit replace.** Uploading a file whose name matches a restricted document the uploader cannot read is refused with 403 (this reveals that the name exists, which we accept: names are metadata).
6. **Role templates are presets, not a new concept.** Creating a role with a template (or applying one) writes ordinary container grants. The grants matrix sets one level per role and container (`PUT /api/admin/roles/{id}/containers/{cid}` replaces whatever was there).
7. **Deleting a role in use is refused** unless its users are reassigned to another role in the same call (`DELETE /api/admin/roles/{id}?reassign_to=...`).

## Consequences
- Revocation stays immediate: permissions are resolved per request, so removing a `manage` grant stops the next upload.
- Every content action (`document.upload|update|delete|retry`) and every role/grant change (`admin.role.*`, `admin.grant.*`) is audited.
- Tests: `tests/security/test_delegation.py`; the ACME demo gets an *HR Contributor* persona (`hr.assistant@acme.test`), which the canary matrix checks automatically (it must never see the restricted HR case).
