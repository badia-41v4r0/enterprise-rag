# 0035. License acceptance in the wizard: a record and a deliberate pause

**Status:** Accepted (2026-11-09), owner decision 4 of PLAN-v0.4 — **Owner review: the wording must be reviewed by a lawyer**

## Decision
- Step 0 shows a plain-language notice built from the README licensing section: source-available under the
  PolyForm Noncommercial License 1.0.0 (not open source), noncommercial use free, commercial use (including running
  it inside a company for business purposes) needs a commercial license from the licensor (Badia Partners,
  https://badiapartners.com), the 60-day evaluation permit and its conditions, and a pointer to the full license.
- An acceptance **checkbox** must be ticked (the API refuses `accepted: false`), with the name of the person
  accepting. The server records who (that name, and later the administrator's email), when, and the SHA-256 of the
  exact text shown; if the text changed between showing and accepting, the request is refused (409).
- It is a record and a pause, **not technical enforcement**: no license key, no license server, no network call.
  The text is kept in `app/services/setup.py` (`LICENSE_TEXT`) so the hash and the text cannot drift apart.

## Consequences
- The acceptance appears in the setup report and in Admin > Setup assistant.
- Changing the wording changes the hash; already completed installations keep the hash of what they accepted.
- **A lawyer should review the wording** (in particular "running it inside a company for business purposes" and
  the summary of the evaluation permit) before it is used with customers.
