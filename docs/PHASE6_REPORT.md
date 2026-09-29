# Hardening Phase 6 report: real execution layer

Branch `phase14-production-hardening`. Everything marked "passed" below was run locally on
Windows 11 with Python 3.13.7, CPU only, on 2026-09-29. CI was not polled. No broker or cluster
was used:
- the `database` and `inline` adapters were exercised for real, on SQLite;
- `celery` ran against a Celery app double;
- `rq` ran against a Redis double with an RQ queue double;
- `kubernetes` ran against the Kubernetes API through `httpx.MockTransport`.

**Celery, RQ and Kubernetes Jobs are unverified against real brokers and clusters. PostgreSQL
claim concurrency is unverified: every claim test ran on SQLite.**

## 1. Findings closed

| Finding | Closed by |
|---|---|
| Finding 5: `POST /adaptation/events` blocks until the job ends and the job runs under the API process | The API records the job as `QUEUED` and returns at once: **201 with status `QUEUED`** (200 for a duplicate, as before). A separate worker process (`oran-adapt worker run`, compose service `worker`) claims and runs it. No job thread or child process remains in the API, apart from the development-only `inline` backend |
| Nothing supervises a job if its runner dies | Leases: a claimed job carries `lease_owner`, `lease_token` and `lease_expires_at`. The worker renews the lease every `JOB_HEARTBEAT_S`. The reaper (every `JOB_REAP_INTERVAL_S`, and `oran-adapt jobs reap`) requeues a job whose lease has expired. A job lost in `REGISTERING`/`PROMOTING` is not re-run: it fails with `JOB_ABANDONED` and `needs_reconciliation` |
| A job could be run to an outcome twice | Every write the runner makes checks the lease token (`jobs.check_fence`). A worker that lost its lease gets `JOB_LEASE_LOST` and writes nothing more |
| Retries `time.sleep` on the request thread | A transient error requeues the job with `available_at` = now + backoff. Retries are counted as `attempt - lost_count`, so a worker crash does not use up a retry |
| Thread mode cannot stop a timed-out job; on Windows only the child died | `core/processes.kill_tree` kills the whole tree, children first, with the standard library only (TerminateProcess on Windows, SIGKILL on Linux). Process mode kills the tree on timeout, on deadline, on cancel and on drain. The sandbox runner uses the same helper |
| No deadlines | `JOB_DEADLINE_S` sets each job's `deadline_at` at submit. A queued job past it is `TIMED_OUT` by the reaper. A running job is stopped and its process tree killed |
| No cancellation | `POST /adaptation/jobs/{id}/cancel` and `oran-adapt jobs cancel`. A queued job is `CANCELLED` at once (200). A running job gets `cancel_requested_at` (202) and stops at its next checkpoint, a stage report or a supervisor tick at most `JOB_HEARTBEAT_S` apart. A job in `REGISTERING`/`PROMOTING` refuses with `JOB_NOT_CANCELLABLE` (409). New terminal state `CANCELLED` |
| No poison handling | a job whose attempts ended `JOB_POISON_THRESHOLD` times without an outcome is quarantined (`JOB_QUARANTINED`) and never claimed again |
| No graceful shutdown | SIGTERM/SIGINT (SIGBREAK on Windows) drain the worker. It stops claiming and waits `JOB_DRAIN_TIMEOUT_S`, then stops the attempt and requeues the job without charging an attempt (`JOB_DRAINED`) |
| No priorities, fairness or GPU scheduling | `priority` from `JOB_PRIORITY_BY_SEVERITY`, claimed by priority then age. `worker_class` from `JOB_CLASS_BY_FRAMEWORK`; a worker claims only its `JOB_WORKER_CLASSES`, so GPU jobs reach GPU workers. `tenant` from `JOB_TENANT_BY_PRINCIPAL`; per-tenant concurrency through `job_slot` rows (`JOB_TENANT_CONCURRENCY`, `JOB_TENANT_LIMITS`) |
| No queue listing | `GET /adaptation/jobs?status&model_id&tenant&quarantined&limit&offset` and `oran-adapt jobs list` |

Migration `0008_job_queue` adds the queue columns to `adaptation_job` and creates `job_slot`.
Its downgrade is tested (`test_migration_0008_up_and_down`).

## 2. Ports and adapters

**The database is the queue. A broker only wakes workers.**
- A worker claims a job with a conditional `UPDATE`, which succeeds for exactly one worker.
- `JobQueuePort.publish(job)` tells a broker that work exists. A publish that fails leaves the
  job `QUEUED`, and the reaper republishes it after `JOB_REPUBLISH_AFTER_S`.
- A lost or duplicated broker message therefore never loses a job and never runs one twice.

| Adapter | Wakes workers through | Needs | Test double |
|---|---|---|---|
| `database` (default) | nothing: workers poll every `JOB_POLL_INTERVAL_S` | – | real (SQLite) |
| `inline` | runs the job inside the request. **Development only**: refused in production | – | real |
| `celery` | a Celery task `JOB_QUEUE_CELERY_TASK` on queue `JOB_QUEUE_NAME_PREFIX` + class | `celery` extra | app double |
| `rq` | an RQ job on queue `JOB_QUEUE_NAME_PREFIX` + class | `rq` extra | Redis + queue doubles |
| `kubernetes` | one Kubernetes Job per attempt (`oran-job-<job_id>-a<n>`), pod spec per class from `JOB_QUEUE_K8S_CLASS_PODS` | no SDK (httpx) | `httpx.MockTransport` |

Other parts:
- `celery`, `kombu`, `redis` and `rq` are allowed only in `adapters/job_queues.py` (import-boundary test).
- A missing SDK fails with a `ConfigurationError` that names the extra.
- The worker is `orchestrator/worker.py` (claim, lease, supervise, reap, drain). Executors are in `adapters/job_executors.py`, and process helpers in `core/processes.py`.
- There is no `arq` adapter. RQ covers the Redis family (see §6).
- Guide: `docs/adapters/job_queue.md`. Template: `templates/job-queue-worker/`. Conformance suite: `oran_adapt.conformance.job_queue`, which all five adapters pass.

New metrics:
- `job_queue_depth{worker_class}`;
- `job_queue_oldest_age_seconds{worker_class}`;
- `job_requeues_total{reason}`;
- `job_quarantined_total`;
- `job_cancelled_total{where}`.

New errors:
- `JOB_NOT_FOUND` (404);
- `JOB_CANCELLED`;
- `JOB_LEASE_LOST`;
- `JOB_DRAINED`;
- `JOB_QUARANTINED`;
- `JOB_QUEUE_UNAVAILABLE` (503);
- `JOB_NOT_CANCELLABLE` (409);
- `JOB_WORKER_LOST`.

## 3. Configuration keys

Added:
- `JOB_QUEUE_BACKEND`, `JOB_HEARTBEAT_S`, `JOB_LEASE_TTL_S`, `JOB_POLL_INTERVAL_S`, `JOB_REAP_INTERVAL_S`, `JOB_REPUBLISH_AFTER_S`, `JOB_POISON_THRESHOLD`, `JOB_DRAIN_TIMEOUT_S`, `JOB_DEADLINE_S`;
- `JOB_DEFAULT_CLASS`, `JOB_CLASS_BY_FRAMEWORK`, `JOB_WORKER_CLASSES`, `JOB_PRIORITY_BY_SEVERITY`, `JOB_DEFAULT_PRIORITY`, `JOB_CLAIM_CANDIDATES`;
- `JOB_TENANT_BY_PRINCIPAL`, `JOB_DEFAULT_TENANT`, `JOB_TENANT_CONCURRENCY`, `JOB_TENANT_LIMITS`;
- `JOB_QUEUE_NAME_PREFIX`, `JOB_QUEUE_CELERY_BROKER_URL` (secret), `JOB_QUEUE_CELERY_TASK`, `JOB_QUEUE_RQ_REDIS_URL` (secret);
- `JOB_QUEUE_K8S_IMAGE`, `JOB_QUEUE_K8S_ENV_SECRET`, `JOB_QUEUE_K8S_SERVICE_ACCOUNT`, `JOB_QUEUE_K8S_CLASS_PODS`, `JOB_QUEUE_K8S_TTL_AFTER_FINISHED_S`.

Kept, with a new meaning:
- `JOB_MAX_RETRIES` now counts requeues, not in-request retries;
- `JOB_RETRY_BACKOFF_S` delays the requeued job;
- `JOB_TIMEOUT_S` limits one attempt;
- `JOB_EXECUTION_MODE` is how a worker runs an attempt;
- `JOB_KILL_GRACE_S` now applies to the whole process tree.

Each key is described in `docs/adapters/job_queue.md`, "Configuration".

## 4. Acceptance criteria

| # | Criterion | Result | Proved by |
|---|---|---|---|
| 1 | Worker killed mid-job → retried or failed cleanly, never stuck, never doubled | PASS | `phase6.py` `worker_killed_mid_job`: a worker subprocess tree is killed mid-job. After the lease TTL the reaper requeues it (`lost_count` 1), a second worker completes attempt 2, and the path shows one `COMPLETED`. Unit: `test_expired_lease_is_requeued_and_finished_once` (the stale runner's write is fenced), `test_worker_process_lost_mid_attempt_is_requeued`, `test_poison_job_is_quarantined`, `test_worker_lost_while_registering_needs_reconciliation`, `test_stale_lease_holder_cannot_write` |
| 2 | Deadline exceeded → process actually terminated | PASS | `phase6.py` `deadline_kills_the_process`: in process mode, a job whose pipeline starts a grandchild is `TIMED_OUT` 30.2 s into a 30 s deadline, and both the attempt's pid and its child pid are gone. Unit: `test_running_job_is_stopped_at_its_deadline`, `test_queued_job_past_its_deadline_times_out`, `test_kill_tree_kills_a_child_and_its_children` |
| 3 | Duplicate `event_id` across two API replicas → one job | PASS | `phase6.py` `duplicate_event_two_replicas`: two uvicorn processes on one database receive the same event at a barrier and answer 201 and 200, and one job row exists. Unit: `test_duplicate_event_from_two_replicas_makes_one_job`, `test_only_one_worker_can_claim_a_job` |
| 4 | Cancel stops work within the checkpoint interval | PASS | `phase6.py` `cancel_within_checkpoint`: a running process-mode job is cancelled over REST, and its process tree is dead 0.3 s after the request (the bound is heartbeat + kill grace + 2 s). Unit: `test_cancel_stops_a_running_job_within_the_checkpoint_interval`, `test_cancel_queued_job_is_immediate`, `test_rest_submit_list_and_cancel` |
| 5 | Unknown-Stack Protocol | PASS | `phase6.py` `boundaries_docs_conformance`: the import boundary, the guide naming every adapter, and the conformance suite on all five adapters (`test_job_queue_conformance`). Unit: `test_every_queue_adapter_is_installed_and_inline_is_development_only`, `test_missing_broker_sdk_names_the_extra`, `test_kubernetes_job_manifest_per_class` |
| – | Drain, priority, classes, tenants, republish | PASS | `test_drain_requeues_the_running_job_without_charging_it`, `test_priority_then_age_decides_the_claim_order`, `test_a_job_runs_only_on_its_worker_class`, `test_tenant_concurrency_limit_holds_across_claims`, `test_unpublished_job_stays_queued_and_the_reaper_publishes_it` |

The unit tests are all in `tests/unit/test_phase6_execution.py` (29 tests).

## 5. Hardcoding

- **No baseline item reopened.**
- **New code:** every interval, threshold, name and pod spec in the new code comes from a `JOB_*` key.
- **Kept on purpose** (see `docs/hardcoding-inventory.md`, "Hardening Phase 6 status"):
  - the Kubernetes Job name pattern `oran-job-<job_id>-a<attempt>`;
  - lease tokens and worker ids built from `uuid4().hex`;
  - the bound of 1000 on `limit` for `GET /adaptation/jobs` (the same as the other list routes).
- Inventory burn-down: C open 8 → 8. C14 (`JOB_*` thresholds as schema defaults) waits on Phase 7's policy files.

## 6. Assumptions and defaults

These are recorded in `docs/OPEN-QUESTIONS.md` ("Job queue adapters"):
- **The queue backend defaults to `database`.** Workers poll it, and no broker is needed.
- **Heartbeat 5 s, lease TTL 30 s.** A dead worker's job is requeued within about 30 s plus one reap interval.
- **A job is quarantined after 3 attempts without an outcome.**
- **Drain waits 30 s.** Compose gives the worker `stop_grace_period: 45s`.
- **No deadline and no tenant limit by default.**
- **`inline` is for development only.** The demo scripts use it, and production refuses it.
- **No `arq` adapter.** The program named "RQ/Arq". RQ was written for the Redis family, and an arq adapter would repeat it through the same port.
- **A job lost while registering or promoting is not re-run.** It needs reconciliation against the registry (Phase 7 rollback work).

## 7. Unverified locally

- **Celery, RQ and Kubernetes Jobs against real brokers and clusters.** `celery`, `rq`, `redis`
  and a cluster were not available. The doubles implement the calls as the adapters make them;
  they are not recordings of the real services. The Celery worker template
  (`templates/job-queue-worker/celery_app.py`) has never run.
- **PostgreSQL claim concurrency.** The single-claim and two-replica tests ran on SQLite. The claim
  is one conditional `UPDATE`, which PostgreSQL also runs atomically, but no PostgreSQL run was made.
- **The compose `worker` service and the polling in `scripts/ci/compose_smoke.sh`.** Docker is not
  installed. The script passed `bash -n` only.
- **SIGTERM drain in a Linux container.** Drain was exercised through `Worker.drain()`, and the
  signal handlers were installed on Windows only.
- **GPU worker classes on real GPU nodes.** Class routing was tested; no GPU exists here.
- **The Docker sandbox backend.** It remains unverified.

## 8. Gate

`scripts/verify.sh 6`: **PASS in 280 s**, run locally on 2026-09-29 (budget 300 s). It covers:
- ruff;
- mypy, with 0 errors in 146 source files;
- the import boundary (2 passed);
- the no-gaps lint;
- the scoped tests (orchestrator, api: 281 passed in 100.8 s);
- the smoke tier (206 passed in 40.0 s), with 2 workers;
- acceptance, 5/5 (worker killed 34 s, deadline 31 s, two replicas 26 s, cancel 12 s, boundaries 3 s).

Wall-clock time varies a lot on this laptop: the same scoped tests took 1200 s once in a
background run while other work was going on.
