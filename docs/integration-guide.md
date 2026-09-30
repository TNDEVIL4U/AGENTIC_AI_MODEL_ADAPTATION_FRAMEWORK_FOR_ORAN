# Integration guide

How the framework connects to the systems around it:

- **in:** a monitoring system reports drift, and a mapper turns its payload into DriftEvents;
- **through:** a job adapts the model, the validation gate decides, and the registry records
  the new version;
- **out:** a deployment adapter puts the accepted candidate in front of traffic, directly or
  through the webhook/GitOps escape hatch.

The last section runs all three end to end, first in-process and then against the compose
stack.

Every adapter named here is listed with its features and keys in the generated
[capability matrix](capability-matrix.md). The worked configurations for common stacks are in
`config/examples/`; each one passes `oran-adapt config lint`.

| Stack | Example config | Registry | Serving | Drift source |
|---|---|---|---|---|
| MLflow + KServe | `config/examples/mlflow-kserve.toml` | `mlflow` | `kserve` (canary via `canaryTrafficPercent`, Prometheus metrics) | Alertmanager |
| SageMaker | `config/examples/sagemaker.toml` | `sagemaker` | `sagemaker` (manual approval) | generic JSON |
| Vertex AI | `config/examples/vertex.toml` | `vertex` | `vertex` (shadow, then approval) | Evidently |
| Seldon Core v2 | `config/examples/seldon.toml` | `mlflow` | `seldon` (blue/green) | Alertmanager |
| NVIDIA Triton | `config/examples/triton.toml` | `filesystem` (native formats) | `triton` (blue/green) | Alertmanager |
| BentoML | `config/examples/bentoml.toml` | `mlflow` | `bentoml` (shadow, then approval) | Evidently |
| Air-gapped | `config/examples/airgapped-filesystem.toml` | `filesystem` | `registry-alias` | Alertmanager, generic JSON |

Use one with `ORAN_CONFIG_FILE=config/examples/<name>.toml`. Keep secrets (tokens, `API_KEYS`)
in the environment or the secrets backend: a config file that contains one is refused.

## 1. Monitoring to DriftEvent: mappers

The API's native input is a DriftEvent at `POST /api/v1/adaptation/events`. A monitoring
system that cannot send one posts its own payload to a **mapper**. A mapper is a TOML mapping
file (ADR-0003), not code:

```toml
# DRIFT_MAPPERS='{"alertmanager": "config/mappers/alertmanager.toml"}'
version = "1"
records = "alerts"                   # one DriftEvent per element of this list
[where]
status = "firing"                    # keep only records where this path has this value
[fields]                             # DriftEvent field = path in the record
model_id = "labels.model_id"
drift_score = "annotations.drift_score"
detected_at = "startsAt"
[split]
affected_features = ","              # "prb_util,cqi" -> ["prb_util", "cqi"]
[value_maps.severity]
warning = "MEDIUM"
[event_id]                           # repeats of one alert are duplicates, not new drift
parts = ["fingerprint", "startsAt"]
separator = "@"
```

### Syntax

- **Paths** are dotted keys.
  - `metrics[metric=DatasetDriftMetric]` picks the first element of a list whose `metric` is
    that value.
  - A leading `/` reads from the payload's root rather than the record.
  - A missing value leaves the field unset. The DriftEvent contract then decides whether that
    is allowed; `model_id` is required.
- **Sections:**
  - `records` and `[where]` select the records;
  - `[fields]` gives each field its path;
  - `[constants]` sets fixed values;
  - `[split]` splits strings into lists;
  - `[value_maps.<field>]` translates values, case-insensitively;
  - `[event_id]` builds the idempotency id from paths or `field:<name>`;
  - `[feature_table]` reads a per-feature drift table: drifted features become
    `affected_features` and their scores become `drift_metrics`.
- **Validation:** mapping files are checked at startup, and a bad file stops the API, naming
  the file and the cause. A payload a mapping cannot turn into a valid DriftEvent is refused
  with HTTP 422 `EVENT_MAPPING_FAILED`, naming the failing record and field. At most
  `DRIFT_MAPPER_MAX_EVENTS` events are accepted per payload.

### Shipped mappers

| Mapping file | Source | Sample payload |
|---|---|---|
| `config/mappers/alertmanager.toml` | Prometheus Alertmanager webhook (v4): one event per firing alert, model and data in the alert labels | `config/mappers/samples/alertmanager.json` |
| `config/mappers/evidently.toml` | Evidently `DataDriftPreset` report: one event per report, drifted columns as features; the caller names the model | `config/mappers/samples/evidently.json` |
| `config/mappers/generic-json.toml` | any JSON document: copy it and repoint the paths | `config/mappers/samples/generic-json.json` |

### Using a mapper

- **Over the API:**
  - `POST /api/v1/adaptation/events/from/{name}` with the payload as the body.
  - Query parameters `model_id`, `model_version` and `dataset_id` override the mapped values.
    Use them when the source does not name the model, as with Evidently.
  - The answer is `{"mapper", "events", "jobs": [...]}`: 201 if any job is new, 200 if all
    were duplicates.
  - The route needs the `submit` action, the same as `/adaptation/events`.
  - Mapped events get the same validation, idempotency and stale-version rejection as
    DriftEvents posted directly.
- **From the CLI:**
  - `oran-adapt event map --mapper alertmanager --input payload.json` prints the DriftEvents
    without submitting them.
  - `--mapping FILE` tries a mapping file that is not configured.
  - `--submit` queues the jobs.
- **Alertmanager:** add a receiver with `webhook_configs: [{url:
  https://oran-adapt.example/api/v1/adaptation/events/from/alertmanager}]` and an API-key
  header (or mTLS/gateway identity; see [docs/security.md](security.md)).
- **Evidently:** post the report JSON with `?model_id=...`.

## 2. Promotion to serving: deployment adapters

After the gate accepts a candidate, `DELIVERY_STRATEGY` decides how it reaches traffic:

- `shadow` (the default) and `manual` wait for approval;
- `canary` and `ab` split traffic in steps from `DELIVERY_POLICY_FILE`;
- `blue_green` switches at once.

`DEPLOYMENT_BACKEND` decides where the change goes. Every deployment adapter passes the same
conformance suite (`oran_adapt.conformance.deployment`): a deploy is read back from what is
served before it counts, and a failed rollout restores the previous version.

| Serving system | `DEPLOYMENT_BACKEND` | Traffic split | Details |
|---|---|---|---|
| inference reads a registry alias | `registry-alias` | canary alias + traffic tag | [deployment.md](adapters/deployment.md) |
| KServe | `kserve` | `canaryTrafficPercent` | [deployment.md](adapters/deployment.md) |
| Seldon Core v2 | `seldon` | – | [deployment.md](adapters/deployment.md) |
| plain Kubernetes Deployment | `k8s` | – | [deployment.md](adapters/deployment.md) |
| NVIDIA Triton | `triton` | – | [deployment.md](adapters/deployment.md) |
| BentoML | `bentoml` | ? | `templates/bentoml-service` |
| SageMaker endpoint | `sagemaker` | ? | [deployment.md](adapters/deployment.md) |
| Vertex AI endpoint | `vertex` | ? | [deployment.md](adapters/deployment.md) |
| anything with an HTTP control API | `webhook` | `POST /traffic` | below |
| Argo CD / Flux | `gitops` | in the manifest | below |

Only adapters with the `traffic_split` feature can serve part of the traffic
([capability-matrix.md](capability-matrix.md#deployment)). A delivery that needs a split is
refused by `oran-adapt config lint` and at startup, naming the adapters that have one, when the
selected adapter lacks it. Such deliveries are `canary`, `ab`, and a shadow or approval that
continues as a canary. The other adapters take `blue_green`, `manual` or `shadow`.

### The escape hatch: webhook and GitOps

A serving system with no adapter is reached without changing the framework (ADR-0004).

- **`webhook`:** implement four endpoints behind `DEPLOYMENT_WEBHOOK_URL`, and the framework
  drives them. They are `GET /health`, `GET /status?model=`, `POST /deploy` and, for
  canaries, `POST /traffic` + `GET /traffic?model=`. The exact bodies are in
  [deployment.md](adapters/deployment.md#the-webhook-contract).
  - `/status` must report what is actually served, not what was requested: that read-back
    is the proof of deployment.
  - `DEPLOYMENT_WEBHOOK_TOKEN` is sent as a bearer token.
  - `templates/bentoml-service` is a complete receiver to copy.
- **`gitops`:** the framework commits a manifest per model into the working copy at
  `GITOPS_REPO_DIR`, rendered from `GITOPS_MANIFEST_TEMPLATE` into `GITOPS_MANIFEST_PATH`,
  and pushes it when `GITOPS_PUSH=true`. Argo CD or Flux applies it.
  - The framework learns the outcome from `GITOPS_STATUS_URL`, which answers like the
    webhook's `/status`.
  - A merge-request flow fits here too: leave `GITOPS_PUSH=false` and let the site's own
    automation raise the request.

To write a proper adapter instead, see [adapter-authoring.md](adapter-authoring.md).

## 3. Clone to canary: the walkthrough

`scripts/walkthrough.py` checks the whole path with real components. Its only input is what
Alertmanager would send:

1. A model is onboarded and LIVE: Ridge on synthetic KPIs, with a drifted data version.
2. The API is ready.
3. `config/mappers/samples/alertmanager.json` is posted unchanged to the Alertmanager mapper.
4. The mapper turns its one firing alert into one job.
5. The job runs the pipeline to `DELIVERING`: the drift is real (the relationship changed),
   so the retrained candidate passes the gate.
6. The rollout is in `CANARY` at the first step of `config/policies/delivery.toml` (5 %),
   and LIVE has not moved.
7. The deployment adapter reports the same split (local mode only).
8. The same alert again returns 200 and the same job, not a second one.

Every step checks what the system actually did, and the script exits 1 otherwise.

**Locally**, with no services:

```sh
python scripts/walkthrough.py
```

This runs the real FastAPI app in-process against a temporary SQLite database, the
filesystem registry and the `registry-alias` deployment, and leaves nothing behind. The
Phase 14 gate runs it.

**Against the compose stack.** Docker is required. The override
`deploy/compose/walkthrough.yml` mounts `config/` and `scripts/` read-only and turns on the
canary strategy and the Alertmanager mapper:

```sh
oran-adapt auth new-key --role OPERATOR     # put the printed API_KEYS entry in .env
export ORAN_API_KEY=<the printed key>
docker compose -f docker-compose.yml -f deploy/compose/walkthrough.yml up -d --build --wait
docker compose -f docker-compose.yml -f deploy/compose/walkthrough.yml exec \
    -e ORAN_API_KEY="$ORAN_API_KEY" api python scripts/walkthrough.py --base-url http://127.0.0.1:8000
```

The script runs inside the api container, so onboarding reaches PostgreSQL and MLflow on the
compose network. The job runs in the `worker` service. Step 7 is skipped, because the serving
side is not reachable from the API. Add `--skip-onboard` to run it again against a model that
is already onboarded.

**This mode has not been run on the development laptop**, which has no Docker; see
[LIMITATIONS.md](LIMITATIONS.md).

From here, either:

- advance the rollout by posting observations to `/api/v1/rollouts/{id}/observations`, or
  configure `ROLLOUT_METRICS_BACKEND=prometheus`; or
- stop it with `POST /api/v1/rollouts/{id}/reject`.

See [rollout_metrics.md](adapters/rollout_metrics.md).
