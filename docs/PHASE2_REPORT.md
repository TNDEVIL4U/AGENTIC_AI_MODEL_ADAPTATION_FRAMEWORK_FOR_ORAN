# Hardening Phase 2 report: model registry abstraction

Branch `phase14-production-hardening`. Everything below marked "passed" was run locally on
Windows 11, Python 3.13.7, CPU only, on 2026-09-29. CI was not polled. SageMaker and Vertex
were exercised only against local API emulators: **they are unverified against AWS and GCP.**

## 1. Findings closed

| Finding | Closed by |
|---|---|
| 1. The model registry was MLflow: the domain called the MLflow client, and "registry" also meant "load a model" | `ModelRegistryPort` (`ports/registry.py`) carries opaque artifact directories and metadata only. Five adapters: `mlflow` (reference), `filesystem`, `mirror`, `sagemaker`, `vertex`. Loading and saving model objects moved to `ModelHandlerPort` (`native`, `mlflow-flavors`), combined by `registry/handlers.ModelHandlers`. `registry/publishing.publish_model` joins them |
| Model versions addressed by backend-specific handles | `model://<name>/<version>` and `model://<name>@<alias>` (`core/model_uri.py`), resolved through any registry adapter |
| No definition of "a correct registry adapter" | conformance suite `oran_adapt.conformance.registry`: 8 checks (protocol, ping, missing_model, versions, download_roundtrip, tag_merge, aliases, pickle) |
| The MLflow SDK was loaded on every plugin discovery | `adapters/registry/mlflow/__init__.py` holds only the spec and imports `registry.py` lazily. The import-boundary test allows `mlflow` only under `adapters/registry/mlflow/` |
| Inventory C2 (`MLFLOW_TRACKING_URI` assumes MLflow) | now adapter config: required, and demanded by `ENVIRONMENT=production`, only when `REGISTRY_BACKEND=mlflow` (new `Capability.production_keys`) |
| Extension path | authoring guide `docs/adapters/registry.md`; cookiecutter `templates/registry-adapter/` (renders to a working adapter that passes the suite, and a test keeps it that way); defaults recorded in `docs/OPEN-QUESTIONS.md` ("Registry adapters") |

## 2. Ports and adapters

| Port | Adapter | Features / capabilities | Required keys | Production keys |
|---|---|---|---|---|
| `registry` | `mlflow` (default) | aliases, version_tags, version_metrics, lineage_inputs | `MLFLOW_TRACKING_URI` | `MLFLOW_TRACKING_URI` |
| `registry` | `filesystem` | aliases, version_tags, version_metrics, lineage_inputs. Per-model lock file; atomic metadata writes; artifacts via `artifact_store` | none | `REGISTRY_FS_ROOT` |
| `registry` | `mirror` | the primary's features; every write is copied to a replica; `sync` repairs the replica | `REGISTRY_MIRROR_PRIMARY`, `REGISTRY_MIRROR_REPLICA` | none (see section 6) |
| `registry` | `sagemaker` | model package groups, S3 `model.tar.gz`, aliases as group tags (needs `boto3`) | `SAGEMAKER_REGION`, `SAGEMAKER_S3_BUCKET`, `SAGEMAKER_INFERENCE_IMAGE` | none |
| `registry` | `vertex` | Vertex Model Registry over REST, GCS sidecar metadata, generation-matched tag writes (needs `google-auth`) | `VERTEX_PROJECT`, `VERTEX_LOCATION`, `VERTEX_GCS_BUCKET`, `VERTEX_SERVING_IMAGE` | none |
| `artifact_store` | `filesystem` (default) | atomic_put | none | `ARTIFACT_STORE_ROOT` (when the filesystem registry uses it) |
| `artifact_store` | `fsspec` | any fsspec URL (memory, s3, gcs, ...), refuses to overwrite | `ARTIFACT_STORE_URL` | none |
| `model_handler` | `mlflow-flavors` (default save format) | sklearn, xgboost, torch through MLflow flavors | none | none |
| `model_handler` | `native` | skops / XGBoost UBJSON / torch, with a manifest; no MLflow needed | none | none |

Loading picks whichever installed handler's `detect` recognises the artifact, so versions saved
in either format keep loading after `MODEL_FORMAT` changes.

## 3. Configuration keys added or changed in Phase 2

| Key | Type | Default | Required |
|---|---|---|---|
| `REGISTRY_BACKEND` | str (adapter name) | `mlflow` | no |
| `MODEL_FORMAT` | str (handler name) | `mlflow-flavors` | no |
| `ARTIFACT_STORE_BACKEND` | str (adapter name) | `filesystem` | no |
| `ARTIFACT_STORE_ROOT` | path | `./data/artifact-store` | in production, with the filesystem registry and store |
| `ARTIFACT_STORE_URL` | fsspec URL | none | with `ARTIFACT_STORE_BACKEND=fsspec` |
| `REGISTRY_FS_ROOT` | path | `./data/registry` | in production, with `REGISTRY_BACKEND=filesystem` |
| `REGISTRY_FS_LOCK_TIMEOUT_S` | float > 0 | `30.0` | no |
| `REGISTRY_MIRROR_PRIMARY` / `REGISTRY_MIRROR_REPLICA` | registry adapter name | none | with `REGISTRY_BACKEND=mirror` |
| `REGISTRY_MIRROR_ON_REPLICA_ERROR` | `fail` \| `log` | `fail` | no |
| `SAGEMAKER_REGION` | str | none | with `sagemaker` |
| `SAGEMAKER_S3_BUCKET` | str | none | with `sagemaker` |
| `SAGEMAKER_S3_PREFIX` | str | `oran-models` | no |
| `SAGEMAKER_INFERENCE_IMAGE` | str | none | with `sagemaker` |
| `SAGEMAKER_GROUP_PREFIX` | str | `""` | no |
| `SAGEMAKER_CONTENT_TYPES` | list[str] | `["application/json", "text/csv"]` | no |
| `SAGEMAKER_ENDPOINT_URL` | URL | none (regional endpoint) | no |
| `VERTEX_PROJECT` / `VERTEX_LOCATION` / `VERTEX_GCS_BUCKET` / `VERTEX_SERVING_IMAGE` | str | none | with `vertex` |
| `VERTEX_GCS_PREFIX` | str | `oran-models` | no |
| `VERTEX_API_ENDPOINT` | URL | none (`https://<location>-aiplatform.googleapis.com`) | no |
| `VERTEX_STORAGE_ENDPOINT` | URL | `https://storage.googleapis.com` | no |
| `VERTEX_HTTP_TIMEOUT_S` / `VERTEX_OPERATION_TIMEOUT_S` / `VERTEX_OPERATION_POLL_S` | float > 0 | `30` / `600` / `5` | no |
| `VERTEX_TAG_UPDATE_ATTEMPTS` | int ≥ 1 | `5` | no |
| `MLFLOW_TRACKING_URI` | URI | `sqlite:///./data/mlflow.db` | with `mlflow`. In production, only with `mlflow` (changed; it was always required) |

`Capability` gained `production_keys`, which `GET /api/v1/capabilities` also lists.
`PRODUCTION_REQUIRED` is now `DATABASE_URL`, `ARTIFACT_WORKDIR`. With the default adapters,
production still requires the same three keys as in Phase 1; only the order of the reported list
changed (`DATABASE_URL`, `ARTIFACT_WORKDIR`, `MLFLOW_TRACKING_URI`). `scripts/acceptance/phase1.py`
was updated to match.

## 4. Acceptance criteria

| # | Criterion | Result | Proved by |
|---|---|---|---|
| 1 | Conformance suite green for MLflow and filesystem against the local stack | PASS | `test_phase2_registry.py::test_conformance[filesystem\|fsspec\|mlflow\|mirror-*]` (32 cases); `scripts/acceptance/phase2.py` check 1 (filesystem+filesystem store, filesystem+fsspec memory, MLflow on sqlite: 8 checks each) |
| 2 | Cloud adapters conformance-tested against emulators; live run marked heavy | PASS (emulator) | `test_conformance[sagemaker-emulator\|vertex-emulator-*]` (16 cases); acceptance check 2; `test_live_cloud_conformance[sagemaker\|vertex]` is `@pytest.mark.heavy` and skips without the required keys (`test_live_runs_skip_without_config`). The live run was **not** executed |
| 3 | No mlflow import outside `adapters/registry/mlflow/` | PASS | `test_import_boundary.py` (`SDK_HOMES`); acceptance check 3 also runs publish → `model://kpi@live` → download → load in a child process where `import mlflow` raises |
| 4 | Model loading moved out of the registry into the handler port | PASS | acceptance check 4 (the port has no `load_model`, and no registry adapter defines `load_model`/`log_model`); `test_handler_roundtrip_and_detection[native\|mlflow-flavors]`, `test_handler_refuses_an_unknown_artifact` |
| – | The suite catches a broken adapter | PASS | `test_the_suite_catches_a_broken_adapter` |
| – | Every installed registry adapter is covered | PASS | `test_every_installed_registry_adapter_is_covered` |
| – | Extension path works | PASS | `test_the_template_renders_an_adapter_that_passes_conformance`. The rendered template was also checked by hand: pytest 10/10, ruff and mypy clean, under two sets of names |
| – | Production requires the selected registry's storage, not MLflow's | PASS | `test_config_system.py::test_production_requires_the_selected_registrys_storage_not_mlflows`, `::test_production_refuses_defaulted_storage_locations`; phase 1 acceptance 4/4 |
| – | Compatibility: REST contract, event_id idempotency, stale-version rejection | PASS | the migrated tests `test_phase10_hardening.py`, `test_phase13_versioning.py`, `test_phase14_*`, `test_phase9_orchestrator.py`, `test_phase4_adaptation.py`, `test_phase1_foundation.py`: 103 passed (together with `test_config_system.py` and the import-boundary test) |
| – | Migrations have a tested rollback | n/a | Phase 2 added no database migration. MLflow data is read and written as before |

## 5. Hardcoding

**Removed:** C2 (see section 1). The registry and handler selection, every backend location,
limit and timeout above are `Settings` keys. The MLflow status and error names live in the
MLflow adapter only.

**Kept on purpose:** backend facts, not choices. These are SageMaker's name and metadata limits
(`_GROUP_MAX`, `_PROP_VALUE_MAX = 256`, `_PROP_COUNT_MAX = 50`), Vertex's reserved `default`
alias, alias syntax and ID length, the OAuth scope, and the `model://` grammar. The adapters'
own tag and file names (`oran.mirror.source_version`, `oran:model-name`, `oran-model.json`) are
persisted data formats: changing one needs a migration, not a config key.

**Remaining:**
- SageMaker transient retries use botocore's defaults. No key sets the retry mode or attempts.
- `filesystem._LOCK_POLL_S = 0.02`, the lock polling interval (the timeout is configurable).
- `native.py` has its own framework alias map (`pytorch` → `torch`) beside `core/frameworks.py`.
- `MLFLOW_SKOPS_TRUSTED_TYPES` (C12) is read by non-MLflow code under an MLflow name. A rename
  with a deprecation alias is scheduled for Phase 12.
- `RegistryUnavailableError.code` is still `MLFLOW_UNAVAILABLE`. It is part of the REST contract,
  so it is kept for compatibility.
- Inventory burn-down: C open 10 → 9 (`docs/hardcoding-inventory.md`, "Hardening Phase 2 status").

## 6. Assumptions and defaults

| Assumption / default | Key that changes it |
|---|---|
| MLflow stays the default registry, and `mlflow-flavors` the default save format | `REGISTRY_BACKEND`, `MODEL_FORMAT` |
| The filesystem registry root is a volume shared by every replica; a crashed writer's lock is reported after the timeout and removed by hand | `REGISTRY_FS_ROOT`, `REGISTRY_FS_LOCK_TIMEOUT_S` |
| A failed mirror replica write fails the call | `REGISTRY_MIRROR_ON_REPLICA_ERROR` |
| A mirror's primary and replica storage keys are not expanded into the production check | set them explicitly |
| SageMaker: new packages are `PendingManualApproval`; model names are mapped to group names and the original kept in a tag | `SAGEMAKER_GROUP_PREFIX`, `SAGEMAKER_INFERENCE_IMAGE` |
| Vertex: metadata lives in GCS sidecars; moving an alias is not atomic (remove, then add) | `VERTEX_GCS_BUCKET`, `VERTEX_GCS_PREFIX`, `VERTEX_TAG_UPDATE_ATTEMPTS` |
| A `native` torch artifact is a pickle; load only from a trusted registry | `MODEL_FORMAT` |
| A third-party adapter validates its own prefixed keys at startup; the core's config lint does not see them | the adapter's env prefix |

All of these are recorded in `docs/OPEN-QUESTIONS.md`.

## 7. Unverified locally

- **SageMaker and Vertex against AWS/GCP.** They were tested only against the emulators in
  `tests/unit/registry_emulators.py`, which implement the calls the adapters make as documented.
  They are not recordings of the real services. `boto3` and `google-cloud` packages were not
  installed. The heavy live test was not run.
- The fsspec store with a cloud URL (`s3://`, `gs://`). Only `memory://` was run.
- The filesystem registry on a real shared network volume. Only a local disk was used.
- The MLflow adapter against a remote tracking server with `--serve-artifacts`. Only local sqlite
  was used. No Docker locally.
- The cookiecutter CLI itself. The template was rendered with jinja2, the engine cookiecutter
  uses, not with `cookiecutter`, which is not installed.

## 8. Gate result

`scripts/verify.sh 2`: **PASS in 167 s** (budget 300 s), run once.

| Step | Result |
|---|---|
| ruff; mypy | clean; no issues in 116 source files |
| import boundary | 2 passed |
| no-gaps lint | clean |
| scoped tests (`registry`, `adapters`, `ports`, `bootstrap`; `not heavy`, `-n 2`) | 158 passed (69 s) |
| smoke tier | 171 passed (28 s) |
| `scripts/acceptance/phase2.py` | 4/4 |

The template test was added before the gate run and is included in the scoped count. Separately:
the migrated older test files listed in section 4 (103 passed, 64 s), `test_config_system.py`
(14 passed before the new production test; included in the 103 run afterwards), and
`scripts/acceptance/phase1.py` (4/4) after the production-keys change.

## Files moved

Two renames with `git mv`, required by the spec ("no mlflow import outside
adapters/registry/mlflow/"):
- `adapters/mlflow_registry.py` → `adapters/registry/mlflow/__init__.py` (its implementation is
  now in `registry.py`; `__init__` holds the spec only).
- `adapters/mlflow_models.py` → `adapters/registry/mlflow/flavors.py`.
