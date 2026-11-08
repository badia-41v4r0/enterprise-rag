# 0014. Frontend: small React SPA with vendored shadcn-style components

**Status:** Accepted (2026-11-08)

## Decision
- React 19 + TypeScript + Vite, Tailwind v4, TanStack Query. No router library: two views (Chat, Admin) switched in state.
- shadcn/ui is a copy-the-source library; the handful of components needed (button, input/select/textarea, card/badge/table) are vendored in `frontend/src/components/ui/` without Radix dependencies. Native `<select>` and checkboxes keep the bundle small (~98 kB gzip JS).
- Chat streams via `fetch` + SSE parsing (POST with the CSRF header; `EventSource` cannot send headers or POST). Citations are clickable and resolved through `/api/chunks/{id}`, which re-checks read access, so a revoked source shows "no longer accessible".
- The UI shows, per answer, the model used, the effective classification, tool calls with their result classification, and refusals; the sidebar shows exactly the tools the model may use for the current user (`/api/tools`).
- Built in the `frontend` stage of the Dockerfile and served by FastAPI from the same origin (cookie auth, no CORS).
- UI smoke test: `scripts/ui_smoke.mjs` (playwright-core against a system Chromium; not part of CI).
