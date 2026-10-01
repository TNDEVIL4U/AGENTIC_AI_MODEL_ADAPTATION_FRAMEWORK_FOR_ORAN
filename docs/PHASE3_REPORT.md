# Hardening Phase 3 report: deployment and serving propagation

Branch `phase14-production-hardening`. Everything below marked "passed" was run locally on
Windows 11, Python 3.13.7, CPU only, on 2026-09-29. CI was not polled. No serving system was
run: webhook, BentoML and Triton were tested against a local stdlib stub, GitOps against a real
git repository with a controller emulator, and Kubernetes, SageMaker and Vertex against API
emulators. **All nine non-default adapters are unverified against the real systems.**

## 1. Findings closed

| Finding | Closed by |
|---|---|
| "Deployed" meant "the registry's LIVE alias moved". Nothing checked that anything served the new version | `DeploymentPort` (`ports/runtime.py`: `ping`, `status`, `deploy`, `restore`). `registry/deployment.Deployer.rollout` deploys, then **reads the serving system's own state back** until it reports the requested version ready. It restores the previous version when the system refuses, fails, settles elsewhere or times out |
| A promotion could leave LIVE, the history and the serving system disagreeing | `registry/promotion.promote_version` moves the alias, rolls out, and undoes both when either fails. A failed rollout leaves LIVE and serving on the previous version, with no `ModelPromotion` row. Rollback goes through the same path |
| One serving mechanism, hardwired | ten adapters behind `oran_adapt.deployment` entry points: `registry-alias` (default), `webhook`, `bentoml`, `gitops`, `triton`, `kserve`, `seldon`, `k8s`, `sagemaker`, `vertex` |
| No definition of "a correct deployment adapter" | conformance suite `oran_adapt.conformance.deployment`: 9 checks, plus `failed_rollout_restored` when the test double can inject a failure |
| Readiness ignored the serving system | `GET /api/v1/ready` includes `deployment` (the adapter's `ping`). `DEPLOYMENT_UNAVAILABLE` maps to 503 |
| Inventory C7 (`LIVE_ALIAS` as serving semantics) | serving is `DEPLOYMENT_BACKEND`. The default adapter's alias is `DEPLOYMENT_ALIAS` (falls back to `LIVE_ALIAS`). See `docs/hardcoding-inventory.md`, "Hardening Phase 3 status" |
| Extension path | authoring guide `docs/adapters/deployment.md` (rules, webhook contract, per-adapter keys); `templates/bentoml-service/`; defaults in `docs/OPEN-QUESTIONS.md` ("Deployment adapters") |

## 2. Ports and adapters

| Adapter | Serving system | Required keys | Test double |
|---|---|---|---|
| `registry-alias` (default) | serving loads `model://<name>@<alias>` | none | the filesystem registry (plus a flaky-alias wrapper for failure injection) |
| `webhook` | the HTTP deployment contract | `DEPLOYMENT_WEBHOOK_URL` | `tests/unit/serving_stub.py` |
| `bentoml` | the same contract under `/oran`, from `templates/bentoml-service` | `BENTOML_URL` | serving stub |
| `gitops` | a manifest committed (and optionally pushed) to git; status from the serving system | `GITOPS_REPO_DIR`, `GITOPS_STATUS_URL` | real `git` repository + `GitOpsController` emulator |
| `triton` | Triton repository API, explicit model control; artifacts staged into the model repository | `TRITON_URL`, `TRITON_REPOSITORY` | serving stub (v2 repository API) |
| `kserve` | `InferenceService` per model (REST, no SDK) | `K8S_API_URL`, `KSERVE_STORAGE_URI_TEMPLATE` | Kubernetes API emulator |
| `seldon` | Seldon Core v2 `Model` per model | `K8S_API_URL`, `SELDON_STORAGE_URI_TEMPLATE` | Kubernetes API emulator |
| `k8s` | an existing `Deployment`; version as a pod env var | `K8S_API_URL` | Kubernetes API emulator |
| `sagemaker` | real-time endpoint per model, from sagemaker registry packages (needs `boto3`) | `SAGEMAKER_REGION`, `SAGEMAKER_DEPLOY_ROLE_ARN`, `REGISTRY_BACKEND=sagemaker` | SageMaker endpoint emulator |
| `vertex` | Vertex AI endpoint per model (REST) | `VERTEX_PROJECT`, `VERTEX_LOCATION`, `REGISTRY_BACKEND=vertex` | Vertex endpoint emulator |

`AdapterSpec.factory` is now `Callable[..., T]`: deployment factories take `(settings, registry)`.
`boto3` is allowed in `adapters/deployment/sagemaker.py` as well as the registry adapter
(import-boundary test).

## 3. Configuration keys added in Phase 3

Common: `DEPLOYMENT_BACKEND` (`registry-alias`), `DEPLOYMENT_TIMEOUT_S` (600),
`DEPLOYMENT_POLL_S` (5), `DEPLOYMENT_HTTP_TIMEOUT_S` (30).

Per adapter: `DEPLOYMENT_ALIAS`; `DEPLOYMENT_WEBHOOK_URL`, `DEPLOYMENT_WEBHOOK_TOKEN`;
`BENTOML_URL`, `BENTOML_TOKEN`; `GITOPS_REPO_DIR`, `GITOPS_MANIFEST_PATH`,
`GITOPS_MANIFEST_TEMPLATE`, `GITOPS_PUSH`, `GITOPS_REMOTE`, `GITOPS_BRANCH`,
`GITOPS_STATUS_URL`, `GITOPS_STATUS_TOKEN`, `GITOPS_AUTHOR_NAME`, `GITOPS_AUTHOR_EMAIL`,
`GITOPS_GIT_TIMEOUT_S`; `K8S_API_URL`, `K8S_NAMESPACE`, `K8S_TOKEN`, `K8S_TOKEN_FILE`,
`K8S_CA_FILE`, `K8S_NAME_PREFIX`; `KSERVE_STORAGE_URI_TEMPLATE`, `KSERVE_MODEL_FORMAT`;
`SELDON_STORAGE_URI_TEMPLATE`, `SELDON_REQUIREMENTS`; `K8S_MODEL_ENV`,
`K8S_MODEL_URI_TEMPLATE`, `K8S_CONTAINER`; `TRITON_URL`, `TRITON_REPOSITORY`,
`TRITON_BASE_CONFIG`; `SAGEMAKER_DEPLOY_ROLE_ARN`, `SAGEMAKER_INSTANCE_TYPE`,
`SAGEMAKER_INSTANCE_COUNT`, `SAGEMAKER_ENDPOINT_PREFIX`; `VERTEX_MACHINE_TYPE`,
`VERTEX_MIN_REPLICAS`, `VERTEX_MAX_REPLICAS`, `VERTEX_ENDPOINT_PREFIX`.

Refused at start-up: a missing required key; `DEPLOYMENT_ALIAS` equal to `CANDIDATE_ALIAS`;
`sagemaker`/`vertex` without their registry; a KServe/Seldon storage template without
`{version}` or with an unknown placeholder; a Triton base config that sets `version_policy`;
a `GITOPS_REPO_DIR` that is not a git checkout; a manifest path that escapes the checkout.
With `DEPLOYMENT_BACKEND` unset, behaviour is unchanged from Phase 2.

## 4. Acceptance criteria

| # | Criterion | Result | Proved by |
|---|---|---|---|
| 1 | Every deployment adapter passes the conformance suite, including the failed-rollout check | PASS (stub/emulators) | `test_phase3_deployment.py::test_conformance[<backend>-<check>]` (10 × 10 cases); `test_every_installed_deployment_adapter_is_covered`; `scripts/acceptance/phase3.py` check 1 |
| 2 | Promotion reads the version back from the serving system; a failed rollout leaves LIVE and serving on the previous version and records no move | PASS | `test_promotion_rolls_out_and_reads_back`, `test_failed_rollout_restores_live_and_serving`; acceptance check 2 (migrated sqlite, filesystem registry, webhook stub) |
| 3 | Pre-Phase-3 behaviour is the default | PASS | `test_default_backend_serves_the_live_alias`; acceptance check 3; the older promotion and rollback tests pass with a `registry-alias` deployer |
| 4 | Vendor SDKs only inside adapters; readiness covers the serving system; every adapter documented | PASS | `test_import_boundary.py`; the readiness 503 test; acceptance check 4 |
| – | The suite and `Deployer` catch broken adapters | PASS | `_Memory` fake-port tests: ignored restore, timeout with restore, settling at another version, a restore that does not hold, and the success metric |
| – | Live runs against real systems | not run | `test_live_conformance` is `@pytest.mark.heavy` and skips without configuration (`test_live_runs_skip_without_config`) |
| – | Migrations have a tested rollback | n/a | Phase 3 added no database migration |

## 5. Hardcoding

**Removed:** C7 (section 1). Every timeout, location, prefix and template above is a `Settings`
key.

**Kept on purpose:** facts of the serving systems, not choices. These are the Kubernetes API
paths and group versions, the annotation `oran.io/model-version`, and the SageMaker tags
`oran:model-version` / `oran:model-name` (persisted data formats). Also kept: SageMaker's
63-character name limit and status names, Triton's `READY` state, and the webhook contract's
paths.

**Remaining:**
- One deployment target per installation. Fan-out to several serving systems would be a new
  adapter.
- Transient-error retries inside a rollout are not configurable. A transient failure fails the
  rollout and restores the previous version; the caller may promote again.
- Inventory burn-down: C open 9 → 8.

## 6. Assumptions and defaults

Recorded in `docs/OPEN-QUESTIONS.md` ("Deployment adapters"): the `registry-alias` default;
the 600 s / 5 s read-back window; settling at another version counts as failure; a restore that
does not hold is reported, not retried; GitOps status comes from the serving system, never from
git; Triton needs a shared repository volume and models already in a Triton layout; the
Kubernetes adapters trust readiness only once `observedGeneration` has caught up.

## 7. Unverified locally

- **Every non-default adapter against its real system.** No BentoML, Triton, Kubernetes
  (KServe, Seldon), Argo CD / Flux, AWS or GCP was available. The stub and emulators implement
  the documented APIs as the adapters use them; they are not recordings of the real services.
  `boto3` is not installed. The heavy live conformance test was not run.
- `templates/bentoml-service` has never run under BentoML. Only the contract it implements is
  tested.
- `gitops` with `GITOPS_PUSH=true` against a real remote. Commits were made to a local
  repository only.
- Kubernetes TLS with `K8S_CA_FILE` and rotating `K8S_TOKEN_FILE` tokens against a real API
  server.

## 8. Gate result

`scripts/verify.sh 3`: **PASS in 173 s** (budget 300 s).

| Step | Result |
|---|---|
| ruff; mypy | clean; no issues in 127 source files |
| import boundary | 2 passed |
| no-gaps lint | clean |
| scoped tests (`registry`, `orchestrator`, `adapters`, `ports`; `not heavy`, `-n 2`) | 282 passed (91 s) |
| smoke tier | 173 passed (27 s) |
| `scripts/acceptance/phase3.py` | 4/4 |

Two earlier runs did not count. The first stopped at ruff: an unused variable in the GitOps
emulator, now fixed. The second passed every check but reported 4605 s. Its acceptance step used
about 20 s of CPU over roughly 70 minutes of wall time, which is consistent with the laptop being
suspended during the run. Steps 1–4 of that run had already taken 325 s, under system load. The
clean rerun above is the recorded result. `test_phase3_deployment.py` alone: 117 passed.
