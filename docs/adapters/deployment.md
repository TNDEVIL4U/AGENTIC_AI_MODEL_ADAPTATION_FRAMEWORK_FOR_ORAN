# Deployment adapters

A deployment adapter implements `oran_adapt.ports.DeploymentPort` for one serving system and is
selected with `DEPLOYMENT_BACKEND=<name>`. It answers two questions: how a model version is put
into service, and what version the serving system *actually* serves right now. The core finds
adapters through the `oran_adapt.deployment` entry-point group and builds the selected one at
the composition root (`bootstrap.build_deployer`). Deployment factories take
`(settings, registry)`, because some adapters serve versions of the registry itself.

Shipped adapters:

| Adapter | Module | Serving system | Verified |
|---|---|---|---|
| `registry-alias` (default) | `adapters/deployment/alias.py` | serving processes load `model://<name>@<alias>` | local |
| `webhook` | `adapters/deployment/webhook.py` | anything implementing the HTTP contract below | local stub only |
| `bentoml` | `adapters/deployment/webhook.py` | a BentoML service from `templates/bentoml-service` | local stub only, unverified against BentoML |
| `gitops` | `adapters/deployment/gitops.py` | a manifest committed to git, applied by Argo CD / Flux | real git + controller emulator, unverified against Argo CD / Flux |
| `triton` | `adapters/deployment/triton.py` | NVIDIA Triton (KServe v2 repository extension), explicit model control | local stub only, unverified against Triton |
| `kserve` | `adapters/deployment/kubernetes.py` | a KServe `InferenceService` per model | API emulator only, unverified against Kubernetes |
| `seldon` | `adapters/deployment/kubernetes.py` | a Seldon Core v2 `Model` per model | API emulator only, unverified against Kubernetes |
| `k8s` | `adapters/deployment/kubernetes.py` | an existing `Deployment` per model (version as a pod env var) | API emulator only, unverified against Kubernetes |
| `sagemaker` | `adapters/deployment/sagemaker.py` | a SageMaker real-time endpoint per model | emulator only, unverified against AWS |
| `vertex` | `adapters/deployment/vertex.py` | a Vertex AI endpoint per model (REST) | emulator only, unverified against GCP |

## How the core uses the port

`registry.deployment.Deployer` wraps the adapter. `rollout(target, previous)`:

1. calls `deploy(target)`;
2. polls `status(model)` every `DEPLOYMENT_POLL_S` until it reads `ready` at `target.version`
   (**post-deploy read-back is mandatory**: an accepted request is not a deployment);
3. if the system refuses, reports `failed`, settles at another version, or does not settle within
   `DEPLOYMENT_TIMEOUT_S`, it calls `restore(model, previous)`, reads that back too, and raises
   `DeploymentError` whose context says `restored` (true/false), `cause`, `serving` and `detail`.

Promotion and rollback (`registry.promotion`) move the live alias, roll the version out, and undo
both when either fails. A promotion is therefore all-or-nothing across the database history, the
registry alias and the serving system. Outcomes are counted in
`model_deployments_total{backend, outcome}`. `GET /api/v1/ready` calls `ping()` as the
`deployment` part of readiness.

## What the port means

| Rule | Why | Conformance check |
|---|---|---|
| `isinstance(adapter, DeploymentPort)` | the core only calls the port | `protocol` |
| `ping()` returns when the serving system (or its control plane) is reachable, and raises `DeploymentUnavailableError` otherwise | readiness probe | `ping` |
| `status` of a model never deployed has `version=None` and is not `failed` | a first rollout has nothing to restore | `fresh_status` |
| After `deploy(target)`, `status` settles at `ready=True, version=target.version`, read from what the system serves, not from what was asked | the read-back is the proof | `deploy_reads_back` |
| `deploy`/`restore` return once the request is accepted. From then on, `status` must not report the old version as settled | otherwise the read-back could pass before the rollout started | `redeploy_moves`, `failed_rollout_restored` |
| Deploying a newer version moves `status` to it | promotion | `redeploy_moves` |
| `restore(model, previous)` puts the previous version back, and `status` reads it | failed rollouts and rollback | `restore_previous` |
| `restore(model, None)` undeploys; `status` then reads `version=None` | a failed first rollout | `restore_none` |
| Deploying the version already served succeeds and still reads back | retries after a crash | `idempotent_redeploy` |
| The instance pickles, and the copy works | jobs run in worker processes | `pickle` |
| A rollout the serving system fails reads `failed=True` (or settles elsewhere, or never settles); the core restores the previous version and it reads back | no half-deployed state | `failed_rollout_restored` (needs `Context.inject_failure`) |

Error mapping: an unreachable system or control plane raises `DeploymentUnavailableError`, and a
refused request (4xx, bad role, quota) raises `DeploymentError`. `status` never raises for "this
model is not deployed": it returns `version=None`. Never let an SDK or HTTP exception escape.

Rules every shipped adapter also follows:

- Read readiness only from state the controller has observed: the Kubernetes adapters require
  `status.observedGeneration >= metadata.generation`, so a status left over from the previous
  version is never mistaken for the new one.
- Writes that lose a race fail the rollout (for example a 409 on `resourceVersion`); they are
  never overwritten blindly.
- Object and endpoint names are the model name made DNS-1123-safe (`_common.dns_name`) after a
  configurable prefix.
- Vendor SDKs (`boto3`) are imported only inside their adapter; the Kubernetes, Vertex, Triton
  and webhook adapters use plain HTTP (`httpx`).

## The webhook contract

`webhook` (base `DEPLOYMENT_WEBHOOK_URL`) and `bentoml` (base `BENTOML_URL` + `/oran`) speak
this contract. `gitops` reads its status half from `GITOPS_STATUS_URL`.

| Request | Answer |
|---|---|
| `GET {base}/health` | 2xx when the server is up |
| `GET {base}/status?model=<model>` | `{"version": str\|null, "ready": bool, "failed": bool, "detail": str}` from what is actually served. A 404 means not deployed. |
| `POST {base}/deploy` with `{"model", "version", "source"}` | 2xx once accepted (the rollout may still be running). A 4xx means refused, and the body says why. `version: null` undeploys. |

`source` is the registry version's artifact location (its `source` field). It can be
null when the version is no longer in the registry. Servers that prefer backend-independent
addressing resolve `model://<model>/<version>` through their own registry client. When `DEPLOYMENT_WEBHOOK_TOKEN` /
`BENTOML_TOKEN` is set, every request carries `Authorization: Bearer <token>`, and a server that
answers 401/403 fails the rollout.

## Traffic split (canary and A/B rollouts)

Adapters with the `traffic_split` feature also implement `TrafficSplitPort`
(`set_traffic(model, stable=, candidate=, percent=)` and `traffic(model)`), so progressive
delivery (`docs/adapters/rollout_metrics.md`) can send `percent` of a model's requests to a
candidate while the stable version keeps the rest. Like `deploy`, `set_traffic` only asks;
`Deployer.split` then polls `status` and reads `traffic()` back, and a split that does not read
back as asked raises `DeploymentError` (the rollout then re-decides from evidence on its next
tick). `percent` 0 with `candidate` null removes the split.

| Adapter | How the split is expressed | Read back from |
|---|---|---|
| `registry-alias` | the candidate gets alias `DEPLOYMENT_CANARY_ALIAS` and version tag `DEPLOYMENT_TRAFFIC_TAG=<percent>`; the stable version keeps `DEPLOYMENT_ALIAS`. Serving processes send that share to `model://<name>@<canary alias>` | the two aliases and the tag |
| `webhook` | `POST {base}/traffic` with `{"model", "stable": {"version", "source"}, "candidate": {"version", "source"} \| null, "percent"}` | `GET {base}/traffic?model=<model>` answering `{"stable", "candidate", "percent"}` from the router |
| `kserve` | `spec.predictor.canaryTrafficPercent` on the InferenceService, the candidate as the latest revision; annotation `oran.io/stable-version` names the stable one | the object's annotation and field |

`DELIVERY_STRATEGY=canary` or `ab` (or a shadow / manual rollout continuing as a canary) with a
backend lacking the feature fails at startup with a `ConfigurationError` naming the backends
that have it. `blue_green` needs no split: it switches all traffic at once with read-back.

## Configuration

Common: `DEPLOYMENT_BACKEND` (default `registry-alias`), `DEPLOYMENT_TIMEOUT_S` (600),
`DEPLOYMENT_POLL_S` (5), and `DEPLOYMENT_HTTP_TIMEOUT_S` (30, per HTTP request).

| Adapter | Required | Optional |
|---|---|---|
| `registry-alias` | — | `DEPLOYMENT_ALIAS` (unset: `LIVE_ALIAS`, the pre-deployment-port behaviour; must differ from `CANDIDATE_ALIAS`) |
| `webhook` | `DEPLOYMENT_WEBHOOK_URL` | `DEPLOYMENT_WEBHOOK_TOKEN` |
| `bentoml` | `BENTOML_URL` | `BENTOML_TOKEN` |
| `gitops` | `GITOPS_REPO_DIR` (a git checkout owned by this adapter), `GITOPS_STATUS_URL` | `GITOPS_MANIFEST_PATH` (`deployments/{name}.json`; must stay inside the checkout), `GITOPS_MANIFEST_TEMPLATE` (`$model $name $version $source`), `GITOPS_PUSH`, `GITOPS_REMOTE`, `GITOPS_BRANCH`, `GITOPS_STATUS_TOKEN`, `GITOPS_AUTHOR_NAME`/`_EMAIL`, `GITOPS_GIT_TIMEOUT_S` |
| `triton` | `TRITON_URL`, `TRITON_REPOSITORY` (a directory the server sees too) | `TRITON_BASE_CONFIG` (a `config.pbtxt` without `version_policy`) |
| `kserve` | `K8S_API_URL`, `KSERVE_STORAGE_URI_TEMPLATE` (must use `{version}`) | `KSERVE_MODEL_FORMAT` |
| `seldon` | `K8S_API_URL`, `SELDON_STORAGE_URI_TEMPLATE` (must use `{version}`) | `SELDON_REQUIREMENTS` |
| `k8s` | `K8S_API_URL` | `K8S_MODEL_ENV`, `K8S_MODEL_URI_TEMPLATE`, `K8S_CONTAINER` |
| `sagemaker` | `SAGEMAKER_REGION`, `SAGEMAKER_DEPLOY_ROLE_ARN`, and `REGISTRY_BACKEND=sagemaker` | `SAGEMAKER_INSTANCE_TYPE`, `SAGEMAKER_INSTANCE_COUNT`, `SAGEMAKER_ENDPOINT_PREFIX`, `SAGEMAKER_ENDPOINT_URL` |
| `vertex` | `VERTEX_PROJECT`, `VERTEX_LOCATION`, and `REGISTRY_BACKEND=vertex` | `VERTEX_MACHINE_TYPE`, `VERTEX_MIN_REPLICAS`, `VERTEX_MAX_REPLICAS`, `VERTEX_ENDPOINT_PREFIX` |

Kubernetes adapters share `K8S_NAMESPACE`, `K8S_TOKEN` or `K8S_TOKEN_FILE` (re-read on every
request, since projected tokens rotate), `K8S_CA_FILE` and `K8S_NAME_PREFIX`. Storage-URI
templates take `{model}`, `{name}` and `{version}`. Any other placeholder is refused at start-up.
A selected adapter with a missing required key fails at start-up with `ConfigurationError`
naming the key.

## Writing a new adapter

1. Implement the four methods (`ping`, `status`, `deploy`, `restore`) and expose an
   `AdapterSpec` whose `Capability` lists `config_keys`, `required_keys` and `distributions`.
2. Register it under `[project.entry-points."oran_adapt.deployment"]` in `pyproject.toml`.
3. Run the suite with a `provision(model)` that makes a new deployable version exist:

   ```python
   from oran_adapt.conformance.deployment import CHECKS, Context, run
   run(MyDeployment(...), Context(provision=my_provision, inject_failure=my_fail_next))
   ```

   Pass `inject_failure` whenever your test double can fail a rollout; without it the
   `failed_rollout_restored` rule is not checked.
4. Add a row to the table above. The phase 3 acceptance script fails for an installed adapter
   that is not documented here, and `test_every_installed_deployment_adapter_is_covered` fails
   for one without a conformance harness.
