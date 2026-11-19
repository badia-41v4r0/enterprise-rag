# 0036. Installation type: empty by default, the ACME demo on request, and removing it

**Status:** Accepted (2026-11-09), owner decision 1 and the plan's assumptions (PLAN-v0.4 sections 5 and 6)

## Decision
- **Step 3** offers an **empty installation** (the default and what a skipped step means: nothing is created) or
  **Demo with mock data**: the fictional ACME Corp (its personas, roles, containers with their documents queued for
  the worker, the reference MCP tools and their grants, the folder connector when its folder is mounted, and the
  demo remote MCP server as *pending* when `ERAG_DEMO_OFFICE_MCP_URL` is set). There are **no starter templates**:
  the demo is the only pre-built structure.
- The wizard's demo **does not create `admin@acme.test`**: the administrator is the person who ran the wizard, so no
  administrator account has the published password. The persona accounts do have it (they are mock accounts for
  exploring and are not forced to change it, ADR 0037).
- Demo presence is detected live (ACME container slugs or persona emails), not from a flag: the interface shows a
  persistent **"Demo installation, not for real data"** banner while it is present, the company name defaults to
  "ACME Corp", the review step warns to keep the instance on localhost or a private network, and the checklist keeps
  flagging it.
- **Remove demo data** (wizard step 3 or Admin > Setup assistant; admin, step-up, audited `setup.demo.remove`): a
  confirmation lists what will be deleted (persona accounts, roles, containers with their document count,
  connectors, MCP servers); deletion removes those, their documents, versions, chunks, jobs, blobs no longer used,
  role quotas, and every grant that points at a removed container, document or tool. The acting administrator is
  never deleted; the audit log is kept. Installing the demo again is only offered in the wizard.

## Consequences
- An empty installation has exactly one user (the administrator) and nothing else (tested).
- Removing the demo also removes the reference `files` and `tickets` MCP servers registered by the demo; an
  administrator can register them again with Admin > Tools > Sync.
- Tests: `test_demo_installation_without_a_known_password_admin_and_its_removal`,
  `test_empty_installation_creates_nothing_beyond_the_administrator`.
