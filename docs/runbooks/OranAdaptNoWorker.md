# OranAdaptNoWorker

**Fires when** no worker has reported `worker_up == 1` for 10 minutes. A worker reports it on
its metrics port (`WORKER_METRICS_PORT`, the chart's `worker.metricsPort`), which the chart's
PodMonitor scrapes.

**Impact.** Nothing claims queued jobs, the reaper does not requeue lost ones and rollouts are
not ticked: canaries hold at their current step until a worker returns. Drift events are still
accepted and queued; nothing is lost.

**Check.**
1. Are worker pods running? `kubectl get pods -l app.kubernetes.io/component=worker`.
2. Is the port served? `WORKER_METRICS_PORT` must be set (the chart sets it from
   `worker.metricsPort`); a worker without it runs but is invisible to this alert.
3. Is it scraped? The PodMonitor (`metrics.podMonitor.enabled`) and the NetworkPolicy's
   `metricsFrom` peers must admit Prometheus to the worker port.
4. Deployments whose jobs all run on a broker's own workers (celery, rq, Kubernetes Jobs) still
   need one `oran-adapt worker run` pool for the reaper and the rollout ticks.

**Fix.** Scale a worker pool up (`workers[].replicas`), fix the crash its logs show, or open the
scrape path. Queued jobs run once a worker claims them.
