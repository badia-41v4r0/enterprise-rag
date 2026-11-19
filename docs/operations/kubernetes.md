# Kubernetes

The chart in `deploy/helm/enterprise-rag` deploys the app (with a horizontal autoscaler, a disruption budget and probes), the workers, PgBouncer, an internal metrics service and a migration job. Postgres comes from CloudNativePG (`deploy/helm/cloudnativepg-cluster.yaml`) or a managed service with pgvector 0.8 or newer. Model servers (Ollama, vLLM) are deployed separately; register them in Admin > Providers with an endpoint pool (ADR 0037). The MCP runner needs a container engine socket and runs outside the cluster (on a dedicated VM), as in ADR 0022.

## Install

1. Postgres: `kubectl apply -f deploy/helm/cloudnativepg-cluster.yaml` (after installing the operator), or a managed service.
2. A Secret named `enterprise-rag` with: `ERAG_DATABASE_URL` (through PgBouncer: `postgresql+asyncpg://erag:...@<release>-enterprise-rag-pgbouncer:5432/erag`), `ERAG_DB_DIRECT_URL` (direct to Postgres, for LISTEN and migrations), `PGBOUNCER_DATABASE_URL` (`postgres://erag:...@erag-db-rw:5432/erag`), `ERAG_SECRET_KEY`, `ERAG_MCP_CONTEXT_SECRET`, `ERAG_SECRETS_KEY` (back it up apart from the database), and the S3 credentials if blobs are in S3. An external secret operator can create it.
3. `helm install erag deploy/helm/enterprise-rag -f my-values.yaml` (image, Ollama URL, S3 bucket, ingress host).
4. Create the first administrator: `kubectl exec deploy/erag-enterprise-rag-app -- erag create-admin --email you@example.com`.
5. Point Prometheus at `<release>-enterprise-rag-metrics` (ports 9464 and 9465) and load `deploy/monitoring/alerts.yml`.

## Upgrades

- The migration job runs before the new pods start (Helm `pre-upgrade` hook), against the direct database URL.
- Migrations are expand/contract (tested by `tests/unit/test_migrations_expand_contract.py`), so the old pods keep serving during the rollout. Pods roll one at a time (`maxUnavailable: 0`) and only receive traffic once `/health/ready` passes.
- **Exception:** a migration that declares `REQUIRES_DOWNTIME` cannot run under the previous release. In v0.5 that is 0018 (`halfvec`). Scale the app and workers to 0, run the upgrade, scale back:
  ```
  kubectl scale deploy/erag-enterprise-rag-app deploy/erag-enterprise-rag-worker --replicas=0
  helm upgrade erag deploy/helm/enterprise-rag -f my-values.yaml
  ```
  On a large corpus the index rebuild takes time; raise `maintenance_work_mem` beforehand.
- After upgrading, run `kubectl exec deploy/erag-enterprise-rag-app -- erag backup verify`.

## What was checked

`helm lint` passes and the rendered manifests (11 resources with the ingress on) validate with `kubeconform -strict` against the Kubernetes schemas. The chart has not been installed on a real cluster in this release; do a staging install first.
