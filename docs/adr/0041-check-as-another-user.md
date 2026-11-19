# 0041. "Does it work?": asking as another user, and what the administrator sees

**Status:** Accepted (2026-11-09), implements PLAN-v0.4 step 9 — **Owner review**

## Context
Step 9 must show that people with different roles get different answers before anyone logs in. That means running a
question as a chosen user, which gives the administrator a view of data the administrator may not be granted:
ADR 0009 says an administrator is a manager, not a data reader.

## Decision
- `POST .../check {user_id, question}` runs the real pipeline as that user (effective permissions, filtered
  retrieval, classification routing, the chosen model; without tools) and returns the user's effective permissions,
  the sources (title, container, classification), the model and the classification, then **deletes the test
  conversation**. Usage counts against that user's quota like any question (a few hundred tokens).
- **During setup** (installation mode) the answer text is shown as well: the administrator has just uploaded the
  documents, and there is nobody else's data yet.
- **After setup** (Setup assistant) the answer text is **not** returned (`answer_hidden: true`), and the call needs
  step-up re-authentication. Sources, model and classification are still shown: titles are visible to
  administrators elsewhere (document lists), contents are not.
- Every call is audited (`setup.check_as_user`) with the target user, the number of sources, the model, the
  classification and whether the answer was shown; never the question.

## Owner review
If even titles should not be shown after setup, restrict the assistant's check to the effective-permissions preview.
