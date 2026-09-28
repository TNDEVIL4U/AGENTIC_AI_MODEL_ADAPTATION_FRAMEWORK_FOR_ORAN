# Writing a model registry adapter

A registry adapter implements `oran_adapt.ports.ModelRegistryPort` for one backend and is
selected with `REGISTRY_BACKEND=<name>`. The core never names a backend. It finds adapters
through the `oran_adapt.registry` entry-point group, builds the selected one once at the
composition root (`oran_adapt.bootstrap`), and calls only the port.

Shipped adapters:

| Adapter | Module | Backend | Verified |
|---|---|---|---|
| `mlflow` (default, reference) | `adapters/registry/mlflow/` | MLflow tracking server / sqlite | local stack |
| `filesystem` | `adapters/registry/filesystem.py` | JSON metadata + any `artifact_store` adapter | local |
| `mirror` | `adapters/registry/mirror.py` | a primary registry, writes copied to a replica | local |
| `sagemaker` | `adapters/registry/sagemaker.py` | SageMaker Model Registry + S3 | emulator only, unverified against AWS |
| `vertex` | `adapters/registry/vertex.py` | Vertex AI Model Registry + GCS (REST) | emulator only, unverified against GCP |

## Quick start

```sh
pip install cookiecutter
cookiecutter templates/registry-adapter        # answers: name, adapter_name, env_prefix, ...
cd <project_slug>
pip install -e ".[test]"
pytest                                          # the conformance suite, against your adapter
```

The generated adapter is a working single-writer registry over a local directory. Port it to
your backend by replacing its four storage primitives (`_load`, `_save`, `_put_artifact` and
`_get_artifact`), and re-run `pytest` after each change. The phase 2 test
`test_the_template_renders_an_adapter_that_passes_conformance` renders the template and runs
the suite on the result, so the template cannot rot.

## What the port means

A registry stores **opaque artifact directories** and the metadata around them. It never builds,
saves or loads a model object: that is the model handler port's job (`ModelHandlerPort`,
`MODEL_FORMAT`). `registry.publishing.publish_model` has the handler write a directory, then
hands that directory to `create_version`.

| Rule | Why | Conformance check |
|---|---|---|
| `isinstance(adapter, ModelRegistryPort)`, and `artifact_policy` is an `ArtifactPolicy` built with `ArtifactPolicy.from_settings(settings)` | callers apply one size limit, hash chunk and tag names | `protocol` |
| `ping()` returns when the backend is reachable and raises `RegistryUnavailableError` otherwise | readiness probe | `ping` |
| A missing model, version or alias raises `ModelNotFoundError`. That applies to every read, to `set_alias`, to `set_version_tags` and to `download_artifacts` | the API maps it to 404 `MODEL_NOT_FOUND` | `missing_model`, `versions`, `tag_merge`, `aliases` |
| Versions are `"1"`, `"2"`, ... per model in creation order, and `list_versions` is oldest first | `model://<name>/<version>` must mean the same on every backend; stale-version rejection compares them | `versions` |
| `get_version_metrics` returns exactly the metrics given to `create_version`, and `{}` when there were none | the promotion gate reads the training-time baseline | `versions` |
| `create_version` returns a version whose status is `READY` | callers use the version at once | `versions` |
| `download_artifacts` returns a local directory that is byte-identical to what was stored | the checksum tag is verified after download | `download_roundtrip` |
| `set_version_tags` merges: existing keys are overwritten and other keys are kept | checksum and status tags are written at different times | `tag_merge` |
| `set_alias` moves an alias that is already set. `delete_alias` of an unset alias is a no-op. `get_registered_model().aliases` lists every alias | promotion and rollback move `live`/`candidate` | `aliases` |
| `model://name@alias` and `model://name/version` resolve through the adapter | backend-independent addressing (`core/model_uri.py`) | `aliases` |
| The instance pickles, and the copy reads the same data | the process job executor sends the registry to a worker | `pickle` |

Error mapping: never let an SDK exception escape. Map "not found" to `ModelNotFoundError`, and
map outages, throttling and permission failures to `RegistryUnavailableError` (the REST error
code stays `MLFLOW_UNAVAILABLE` for contract compatibility). Map a write that loses a race to
`ConflictError`. Any retry budget comes from settings. The Vertex adapter retries a tag write
that raced another writer at most `VERTEX_TAG_UPDATE_ATTEMPTS` times, then raises
`ConflictError`. The SageMaker adapter currently relies on botocore's own retry defaults, and
no key configures them yet: a recorded gap, not a pattern to copy.

Names: the conformance suite uses lower-case names with hyphens, and plain-word aliases,
because every backend accepts that subset. If your backend's naming is narrower than
`core.model_uri`'s grammar (1-128 characters of `[A-Za-z0-9_.-]`, starting alphanumeric), map
names deterministically and store the original: see `sagemaker.group_name` and
`vertex.model_id`. If it cannot store something (tag count or length limits), raise
`ConflictError` or `ArtifactError` naming the limit. Never truncate silently.

## Configuration

`Capability` fields, as used by the core:

| Field | Meaning |
|---|---|
| `features` | flags the core may test (`aliases`, `version_tags`, `version_metrics`, `lineage_inputs`) instead of branching on the adapter name |
| `config_keys` | the settings the adapter reads, shown by `GET /api/v1/capabilities` and `oran-adapt config effective` |
| `required_keys` | core `Settings` fields that must be non-empty when the adapter is selected. Startup fails naming the key |
| `production_keys` | core `Settings` fields whose default is a local development path. `ENVIRONMENT=production` refuses to start until they are set explicitly |
| `distributions` | optional Python packages the adapter needs |

`required_keys` and `production_keys` may only name fields of `oran_adapt.core.config.Settings`.
An adapter shipped outside this repository keeps its own keys in its own `pydantic-settings`
class under an env prefix, and validates them in its factory, which runs at startup. The
template does exactly this. That is an extension-path limitation: its keys do not appear in the
core's config lint. It is recorded in `docs/OPEN-QUESTIONS.md`.

## Import rules

- The spec module (the entry-point target) is imported every time adapters are listed. It must
  not import the backend SDK: the factory imports the implementation lazily. See
  `adapters/registry/mlflow/__init__.py`, which lets the MLflow spec load where MLflow is not
  installed.
- In this repository, an SDK may be imported only from its home in `SDK_HOMES`
  (`tests/unit/test_import_boundary.py`). A new first-party adapter adds its SDK there.
- A first-party adapter adds its entry point to `pyproject.toml` under
  `[project.entry-points."oran_adapt.registry"]`, then runs `pip install -e . --no-deps` so the
  entry point is registered.

## Testing a new adapter

- Parametrize a test over `oran_adapt.conformance.registry.CHECKS`, or call `run(adapter,
  Context(tmp_path))`. Each check uses its own model names, so one adapter instance can serve
  every check.
- A cloud backend is tested against an emulator or recorded fixtures in the default tier
  (`tests/unit/registry_emulators.py` has SageMaker/S3 and Vertex/GCS emulators). The run against
  the real service is marked `heavy` and skips when the adapter's `required_keys` are unset
  (`test_live_cloud_conformance`).
- Say plainly what was verified: an adapter that has passed only against an emulator is
  "unverified against" the real service in reports and in `docs/OPEN-QUESTIONS.md`.
