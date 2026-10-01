# OranAdaptApiDown

**Fires when** Prometheus has not scraped an oran-adapt API target for 5 minutes (`up == 0`).

**Impact.** Drift events cannot be submitted and job status cannot be read. Running workers
keep working; queued jobs wait.

**Check.**
1. `kubectl get pods -l app.kubernetes.io/component=api` (or `docker compose ps api`).
2. A pod stuck in `Init`: the `wait-for-schema` init container is waiting for the migration
   hook. Read its log; `oran-adapt db status` shows `behind`, `ahead` or `unreachable`.
3. A pod restarting: `kubectl logs --previous`. A `CONFIGURATION_ERROR` names the key at fault.
4. Running but not ready: `GET /api/v1/ready` lists the failing component (database, registry,
   deployer).
5. Pods healthy but the scrape fails: the NetworkPolicy (`networkPolicy.metricsFrom`) or
   `METRICS_PUBLIC=false` without scrape credentials.

**Fix.** Correct the named key or the dependency, then let the Deployment roll. Never run
migrations by hand from an API pod; use `oran-adapt db upgrade` in the migrator image
(`docs/operations/migrations.md`).
