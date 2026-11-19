# 0053. Helm and zero-downtime upgrades

**Status:** Accepted (2026-11-19).

## Decision
- **Helm chart** (`deploy/helm/enterprise-rag`): the app Deployment with an HPA on CPU, a PodDisruptionBudget, readiness on `/health/ready` and liveness on `/health/live`, a short `preStop` so proxies drop the pod before it stops accepting, and a grace period above uvicorn's 60 s stream drain; the worker Deployment with a 130 s grace period (it finishes the jobs in hand); PgBouncer in transaction mode; a headless metrics Service; an optional Ingress with SSE-friendly timeouts. Pods run as non-root with a read-only root filesystem and no capabilities.
- **Secrets only by reference** (`secrets.existingSecret`): the chart never takes a secret value in `values.yaml`, so an external secret store can supply them.
- **Reranker files**: an init container runs `erag models install` (pinned by hash) into an emptyDir, or a prepared volume is mounted for offline clusters.
- **Postgres** is not in the chart: CloudNativePG (Apache-2.0; an example `Cluster` with a replica, failover and Barman backups to S3 with PITR) or a managed service. The app reconnects after a failover (`pool_pre_ping`).
- **Migrations as a `pre-install,pre-upgrade` hook Job**, using the direct database URL.
- **Expand/contract** is enforced by a test on the migration files: `upgrade()` may not drop, rename, retype or tighten anything unless the migration declares `CONTRACT` (the contract step, a release after the code stopped reading the object) or `REQUIRES_DOWNTIME` (no compatible path; the upgrade notes say to scale to zero). In v0.5 only 0019 (`halfvec`) declares downtime.
- **Compose stays** the single-host path, with the `scale` profile for two replicas, two workers and PgBouncer.

## Consequences
- Rolling upgrades without downtime for every expand-only migration; an explicit, tested exception otherwise.
- The chart is validated (`helm lint`, `kubeconform -strict`) but has not been installed on a real cluster in this release.
- "A test runs the previous release's suite against the new schema" (PLAN-v0.5 5.6) is approximated by the static expand/contract check; running the previous suite is left for when there is a tagged previous release image in CI.
