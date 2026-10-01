# OranAdaptJobQueueStalled

**Fires when** the oldest queued job of a worker class has waited more than 30 minutes
(`job_queue_oldest_age_seconds`), for 10 minutes.

**Impact.** Adaptations of that class are not running; drifted models stay in service.

**Check.**
1. Is there a worker pool for the class? Each `workers[].classes` entry in the Helm values is
   a class; a job whose class no pool serves waits forever.
2. Are the pool's pods running and live? `oran-adapt worker health` is the liveness probe; a pod
   killed by it has stopped turning its loop.
3. GPU pools: pending pods mean no node matches the pool's `nodeSelector`/`tolerations`.
4. `oran-adapt jobs list --status QUEUED` shows what waits; `job_requeues_total` by reason
   shows jobs bouncing (lost, lease_expired).

**Fix.** Scale the pool (`workers[].replicas`), add a pool for the class, or fix the node
capacity. Jobs are durable: nothing is lost while they wait.
