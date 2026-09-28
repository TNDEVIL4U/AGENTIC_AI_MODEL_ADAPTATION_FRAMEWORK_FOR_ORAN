# Hardening Phase 1 report: tiers, ports, configuration, hardcoding burn-down

Branch `phase14-production-hardening`. Commits: `fca0cb1` (test tiers, `scripts/verify.sh`),
`80b4134` (ports and adapters), `3891b6d` (configuration system), `350193d` (hardcoding
burn-down), `911edc1` (acceptance script). All pushed. CI was not polled; everything below that
says "passed" was run locally on Windows 11, Python 3.13.7, CPU only.

## 1. Findings closed

| Finding | Closed by |
|---|---|
| No fast local gate; every check ran the whole suite | pytest tiers `smoke` / unmarked / `heavy`, `scripts/verify.sh <n>` and `make verify`, 300 s budget, xdist capped at 2 workers |
| Vendor SDKs imported across the domain code (MLflow, Kafka, Anthropic, Gemini, httpx webhooks) | 12 ports in `oran_adapt.ports`; vendor code only under `oran_adapt.adapters`, resolved once in `bootstrap.py` through entry points (`oran_adapt.<port>`); `tests/unit/test_import_boundary.py` enforces it |
| Configuration was environment variables with silent defaults | layered, schema-validated `Settings` (init > env > `.env` > secrets backend > TOML file > defaults); unknown keys and secrets in files are errors; startup fails naming the key; `oran-adapt config lint`; redacted `GET /api/v1/config/effective` (ADMIN) and `oran-adapt config effective`; `GET /api/v1/capabilities` |
| Hardcoding inventory A1-A24, B1-B7 | all closed except A17 and A24, kept with reasons (section 5) |
| Inventory C1, C3, C8, C9 | production profile requires storage locations; C8 derived from the framework table; `KAFKA_BOOTSTRAP_SERVERS` has no default and is required with `CDC_MODE=kafka` |
| Framework dispatch repeated in 6 files (A23) | one table, `core/frameworks.py` |
| Dead registry client and loaders | `registry/client.py` and `adaptation/loaders.py` deleted (with approval) |

## 2. Ports and adapters

Generated from the installed entry points (`plugins.adapters`), the same data
`GET /api/v1/capabilities` serves.

| Port | Protocol | Selector | Adapter | Features | Required keys |

|---|---|---|---|---|---|

| `registry` | `ModelRegistryPort` | `REGISTRY_BACKEND` | `mlflow` | lineage_inputs, version_tags, run_metrics, aliases | `MLFLOW_TRACKING_URI` |

| `artifact_store` | `ArtifactStorePort` | – | (none yet) | – | – |

| `model_handler` | `ModelHandlerPort` | by framework | `mlflow-flavors` | pytorch, load, xgboost, sklearn, torch | – |

| `deployment` | `DeploymentPort` | – | (none yet) | – | – |

| `dataset` | `DatasetPort` | – | (none yet) | – | – |

| `cdc_source` | `CdcSourcePort` | `CDC_MODE` | `kafka` | external_offsets, network, at_least_once | `KAFKA_BOOTSTRAP_SERVERS`, `CDC_KAFKA_TOPIC` |

| `cdc_source` | `CdcSourcePort` | `CDC_MODE` | `polling` | exactly_once_offsets, offline | – |

| `job_executor` | `JobExecutorPort` | `JOB_EXECUTION_MODE` | `process` | isolated, hard_timeout | – |

| `job_executor` | `JobExecutorPort` | `JOB_EXECUTION_MODE` | `thread` | in_process | – |

| `notification` | `NotificationPort` | `NOTIFICATION_BACKEND` | `log` | offline | – |

| `notification` | `NotificationPort` | `NOTIFICATION_BACKEND` | `webhook` | network | `NOTIFICATION_WEBHOOK_URL` |

| `llm` | `LLMPort` | `LLM_PROVIDER` | `anthropic` | network, system_prompt, completion | `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` |

| `llm` | `LLMPort` | `LLM_PROVIDER` | `gemini` | network, system_prompt, completion | `GEMINI_API_KEY`, `GEMINI_MODEL` |

| `auth` | `AuthPort` | `AUTH_BACKEND` | `api-key` | static_keys, roles | – |

| `policy` | `PolicyPort` | `POLICY_BACKEND` | `static-rbac` | deny_by_default, roles | – |

| `secrets` | `SecretsPort` | `SECRETS_BACKEND` | `env` | read | – |

| `secrets` | `SecretsPort` | `SECRETS_BACKEND` | `file` | read, rotation_on_restart | `SECRETS_DIR` |


Ports with a single adapter (`registry`, `model_handler`, `auth`, `policy`) and ports with none
yet (`artifact_store`, `deployment`, `dataset`) are recorded with their open question and
schedule in `docs/OPEN-QUESTIONS.md`. The Unknown-Stack Protocol's "at least two adapters" is
met for `cdc_source`, `job_executor`, `notification`, `llm` and `secrets` only.

## 3. Configuration keys

All 93 keys of `oran_adapt.core.config.Settings`, generated from the schema. "Required" means
startup fails naming the key when it is unset in that situation: in the production profile
(`ENVIRONMENT=production`), or when the named adapter is selected. Secret keys are never read
from a file.

| Key (env) | Type | Default | Required |
|---|---|---|---|
| `ENVIRONMENT` | `Literal['development', 'production']` | `'development'` | no |
| `DATABASE_URL` | `str` | `'sqlite:///./data/oran_adapt.db'` | in production |
| `MLFLOW_TRACKING_URI` | `str` | `'sqlite:///./data/mlflow.db'` | in production; when registry=mlflow |
| `MLFLOW_REGISTRY_URI` | `str \| None` | `None` | no |
| `ARTIFACT_WORKDIR` | `str` | `'./data/artifacts'` | in production |
| `REGISTRY_BACKEND` | `str` | `'mlflow'` | no |
| `MLFLOW_HTTP_MAX_RETRIES` | `int` | `1` | no |
| `MLFLOW_HTTP_BACKOFF_FACTOR` | `float` | `0.0` | no |
| `MLFLOW_HTTP_TIMEOUT_S` | `float` | `10.0` | no |
| `REGISTRY_TAGS_CHECKSUM` | `str` | `'artifact.sha256'` | no |
| `REGISTRY_TAGS_STATUS` | `str` | `'oran.status'` | no |
| `LLM_PROVIDER` | `str` | `'none'` | no |
| `ANTHROPIC_API_KEY` | `SecretStr \| None` | `(secret, unset)` | when llm=anthropic |
| `ANTHROPIC_MODEL` | `str` | `'claude-sonnet-5'` | when llm=anthropic |
| `GEMINI_API_KEY` | `SecretStr \| None` | `(secret, unset)` | when llm=gemini |
| `GEMINI_MODEL` | `str` | `'gemini-3.6-flash'` | when llm=gemini |
| `LLM_TIMEOUT_S` | `float` | `60.0` | no |
| `LLM_MAX_OUTPUT_TOKENS` | `int` | `2048` | no |
| `LLM_MAX_RETRIES` | `int` | `0` | no |
| `SANDBOX_BACKEND` | `Literal['docker', 'subprocess']` | `'subprocess'` | no |
| `SANDBOX_TIMEOUT_S` | `int` | `120` | no |
| `SANDBOX_MEMORY_MB` | `int` | `1024` | no |
| `SANDBOX_DOCKER_IMAGE` | `str` | `'oran-adapt-sandbox:latest'` | no |
| `SANDBOX_DOCKER_PIDS_LIMIT` | `int` | `128` | no |
| `SANDBOX_DOCKER_CPUS` | `float` | `1.0` | no |
| `SANDBOX_DOCKER_TMPFS_MB` | `int` | `64` | no |
| `SANDBOX_DOCKER_CLEANUP_TIMEOUT_S` | `float` | `60.0` | no |
| `SANDBOX_MANIFEST_MAX_BYTES` | `int` | `4096` | no |
| `LIVE_ALIAS` | `str` | `'live'` | no |
| `CANDIDATE_ALIAS` | `str` | `'candidate'` | no |
| `LOG_LEVEL` | `str` | `'INFO'` | no |
| `LOG_JSON` | `bool` | `True` | no |
| `JOB_MAX_RETRIES` | `int` | `2` | no |
| `JOB_RETRY_BACKOFF_S` | `float` | `1.0` | no |
| `JOB_TIMEOUT_S` | `float` | `600.0` | no |
| `JOB_EXECUTION_MODE` | `str` | `'process'` | no |
| `JOB_KILL_GRACE_S` | `float` | `5.0` | no |
| `ANALYSIS_PSI_REUSE_THRESHOLD` | `float` | `0.1` | no |
| `ANALYSIS_KS_PVALUE_REUSE_THRESHOLD` | `float` | `0.05` | no |
| `ANALYSIS_DRIFT_SCORE_REUSE_THRESHOLD` | `float` | `0.3` | no |
| `ANALYSIS_MIN_PSI_ROWS` | `int` | `30` | no |
| `ANALYSIS_PERFORMANCE_HISTORY_LIMIT` | `int` | `10` | no |
| `REUSE_ENABLED` | `bool` | `True` | no |
| `REUSE_MAX_VERSIONS` | `int` | `10` | no |
| `REUSE_MIN_ACCURACY_GAIN` | `float` | `0.02` | no |
| `REUSE_MIN_RMSE_REDUCTION_RATIO` | `float` | `0.05` | no |
| `REUSE_MAX_MODEL_AGE_DAYS` | `float \| None` | `None` | no |
| `REUSE_CONFIDENCE_ROWS` | `int` | `100` | no |
| `MODEL_LOCK_TTL_S` | `float` | `3600.0` | no |
| `DECISION_MIN_DRIFTED_ROWS` | `int` | `10` | no |
| `DECISION_FULL_RETRAIN_PSI_THRESHOLD` | `float` | `0.5` | no |
| `DECISION_SUPPORTED_FRAMEWORKS` | `list[str]` | `['pytorch', 'sklearn', 'torch', 'xgboost']` | no |
| `DECISION_MEMORY_COPIES` | `int` | `3` | no |
| `DECISION_DEFAULT_CONFIDENCE` | `float` | `0.5` | no |
| `VALIDATION_MIN_ROWS` | `int` | `5` | no |
| `VALIDATION_HOLDOUT_FRACTION` | `float` | `0.2` | no |
| `VALIDATION_ACCURACY_TOLERANCE` | `float` | `0.02` | no |
| `VALIDATION_RMSE_TOLERANCE_RATIO` | `float` | `0.05` | no |
| `LEAKAGE_CHECKS_ENABLED` | `bool` | `True` | no |
| `LEAKAGE_ALLOW_FUTURE_ROWS` | `bool` | `False` | no |
| `LEAKAGE_TARGET_CORRELATION_MAX` | `float \| None` | `None` | no |
| `MLFLOW_SKOPS_TRUSTED_TYPES` | `list[str]` | `['sklearn.tree._tree.Tree', 'sklearn.ensemble._hist_gradi...` | no |
| `ARTIFACT_MAX_BYTES` | `int` | `2147483648` | no |
| `ARTIFACT_HASH_CHUNK_BYTES` | `int` | `1048576` | no |
| `TORCH_FINE_TUNE_EPOCHS` | `int` | `5` | no |
| `TORCH_FULL_RETRAIN_EPOCHS` | `int` | `300` | no |
| `TORCH_LEARNING_RATE` | `float` | `0.01` | no |
| `API_MAX_REQUEST_BYTES` | `int` | `10485760` | no |
| `API_PAGINATION_DEFAULT_LIMIT` | `int` | `50` | no |
| `API_CORRELATION_HEADER` | `str` | `'X-Correlation-ID'` | no |
| `AUTH_ENABLED` | `bool` | `True` | no |
| `AUTH_BACKEND` | `str` | `'api-key'` | no |
| `AUTH_API_KEY_HEADER` | `str` | `'X-API-Key'` | no |
| `API_KEYS` | `dict[str, str]` | `{}` | no |
| `METRICS_PUBLIC` | `bool` | `True` | no |
| `POLICY_BACKEND` | `str` | `'static-rbac'` | no |
| `POLICY_ROLES` | `dict[str, list[str]]` | `{'read': ['ADMIN', 'OPERATOR', 'ML_ENGINEER', 'READ_ONLY'...` | no |
| `NOTIFICATION_BACKEND` | `str` | `'log'` | no |
| `NOTIFICATION_WEBHOOK_URL` | `str \| None` | `None` | when notification=webhook |
| `NOTIFICATION_WEBHOOK_TIMEOUT_S` | `float` | `5.0` | no |
| `SECRETS_BACKEND` | `str` | `'env'` | no |
| `SECRETS_DIR` | `str \| None` | `None` | when secrets=file |
| `CDC_MODE` | `str` | `'disabled'` | no |
| `CDC_BATCH_SIZE` | `int` | `500` | no |
| `CDC_POLLING_TABLE` | `str` | `'kpi_sample'` | no |
| `CDC_SCHEMA_REF` | `str` | `'kpi_sample/1'` | no |
| `CDC_IDLE_POLL_S` | `float` | `1.0` | no |
| `CDC_MAX_TX_IDS_PER_VERSION` | `int` | `1000` | no |
| `KAFKA_BOOTSTRAP_SERVERS` | `str \| None` | `None` | when cdc_source=kafka |
| `CDC_KAFKA_TOPIC` | `str` | `'oran.public.kpi_sample'` | when cdc_source=kafka |
| `CDC_CONSUMER_GROUP` | `str` | `'oran-adapt-cdc'` | no |
| `CDC_KAFKA_POLL_TIMEOUT_S` | `float` | `1.0` | no |
| `CDC_KAFKA_AUTO_OFFSET_RESET` | `Literal['earliest', 'latest']` | `'earliest'` | no |

## 4. Acceptance criteria

| # | Criterion | Result | Proved by |
|---|---|---|---|
| 1 | Import-boundary test passes | PASS | `tests/unit/test_import_boundary.py` (verify step 2) |
| 2 | No-gaps lint passes | PASS | `scripts/lint_no_gaps.py` (verify step 3) |
| 3 | A missing required key fails startup, naming the key | PASS | `test_config_system.py::test_a_selected_adapter_without_its_required_key_fails_naming_it`, `::test_production_refuses_defaulted_storage_locations`, `::test_a_bad_value_fails_naming_the_key`; `scripts/acceptance/phase1.py` runs the real CLI with `CDC_MODE=kafka` and with `ENVIRONMENT=production` |
| 4 | Config-lint passes on every example config | PASS | `test_config_system.py::test_every_example_config_passes_lint`, `::test_lint_reports_each_bad_file_by_name`; acceptance check 3 (`config/examples/development.toml`, `production.toml`) |
| 5 | `GET /api/v1/capabilities` lists the registered adapters | PASS | `test_config_system.py::test_capabilities_lists_every_registered_adapter`; acceptance check 4 compares the served list with every installed entry point, port by port |
| – | Effective config is visible and redacted | PASS | `test_config_system.py::test_effective_config_redacts_secrets_and_url_passwords`, `::test_the_effective_config_endpoint_is_redacted`; ADMIN-only per `test_phase15_api_security.py` |
| – | Configured values reach the code (not only the schema) | PASS | `test_phase15_sandbox.py::test_the_docker_backend_takes_its_limits_from_settings`; `test_phase6_torch.py::test_run_engine_passes_torch_learning_rate`; `test_phase11_docker_sandbox.py::test_adapt_via_llm_sandbox_backend_is_required_and_the_schema_default_is_subprocess` |
| – | Secrets come from the selected secrets backend, never from a file | PASS | `test_config_system.py::test_the_file_secrets_backend_supplies_secret_keys`, `::test_a_file_holding_a_secret_is_refused` |
| – | Compatibility: REST contract, event_id idempotency, stale-version rejection | PASS | unchanged tests in the default tier (`test_phase10_hardening.py`, `test_phase14_*`, `test_phase15_*`) |

## 5. Hardcoding

**Removed this phase** (details and keys in `docs/hardcoding-inventory.md`, "Hardening Phase 1
status"):

- Sandbox Docker limits and the manifest cap: `SandboxLimits.from_settings`
  (`SANDBOX_DOCKER_PIDS_LIMIT`, `_CPUS`, `_TMPFS_MB`, `_CLEANUP_TIMEOUT_S`,
  `SANDBOX_MANIFEST_MAX_BYTES`).
- Artifact size cap, hash chunk and registry tag names: `ArtifactPolicy`
  (`ARTIFACT_MAX_BYTES`, `ARTIFACT_HASH_CHUNK_BYTES`, `REGISTRY_TAGS_CHECKSUM`,
  `REGISTRY_TAGS_STATUS`), held by the registry adapter.
- CDC: `CDC_POLLING_TABLE`, `CDC_SCHEMA_REF`, `CDC_MAX_TX_IDS_PER_VERSION`, `CDC_IDLE_POLL_S`.
- LLM output cap and retries, job kill grace, decision memory copies and default confidence,
  performance-history limit, pagination limit, correlation and API-key header names.
- Framework dispatch: `core/frameworks.py`; `DECISION_SUPPORTED_FRAMEWORKS` defaults to it.
- Function-level defaults that shadowed settings: torch epochs and learning rate (`TorchBudget`),
  `min_psi_rows`, `live_alias`, `sandbox_backend`, `sandbox_docker_image`, artifact max bytes.
- The validation gate's metric is explicit per task (`validation.metrics.PRIMARY_METRIC`)
  instead of "the first key of a dict".

**Kept on purpose:**

- A17, Prometheus histogram buckets: metrics register at import, before settings exist, and
  fixed buckets keep histograms from different replicas aggregatable.
- A24, `"observed_at"`: the internal KPI table's time column, part of the DB schema and changed
  by a migration. A dataset's own time column is already a parameter (`timestamp_column`).

**Remaining:**

- The decision engine's significance factors (0.7 and 0.4, floor 0.01) and the validation
  tolerances (C13, C14): these move to policy files in Hardening Phase 7.
- The other `oran.*` run tags in `orchestrator/pipeline.py`.
- The framework names in `sandbox/runner.py` and `sandbox/security.py`. These are the sandbox's
  serialization formats and import allow-list, not dispatch.
- Inventory C2, C4-C7, C10-C12 (adapter and deployment settings), and all of section D
  (infrastructure files, Hardening Phase 9).

## 6. Assumptions and defaults

| Assumption / default | Key that changes it |
|---|---|
| Development profile; storage defaults under `./data` | `ENVIRONMENT`, `DATABASE_URL`, `MLFLOW_TRACKING_URI`, `ARTIFACT_WORKDIR` |
| No LLM; the deterministic decision and adaptation path runs | `LLM_PROVIDER` |
| CDC off | `CDC_MODE` (`polling`, `kafka`) |
| Jobs run in a child process with a hard timeout | `JOB_EXECUTION_MODE`, `JOB_TIMEOUT_S` |
| Notifications go to the log | `NOTIFICATION_BACKEND`, `NOTIFICATION_WEBHOOK_URL` |
| Secrets come from the environment | `SECRETS_BACKEND`, `SECRETS_DIR` |
| MLflow is the registry, with artifacts through `--serve-artifacts` (no MinIO) | `REGISTRY_BACKEND`, `MLFLOW_TRACKING_URI` |
| API-key auth with static RBAC | `AUTH_BACKEND`, `POLICY_BACKEND`, `POLICY_ROLES`, `AUTH_API_KEY_HEADER` |
| Sandbox: subprocess with a soft resident-memory watchdog; Docker is the hard-limit option | `SANDBOX_BACKEND`, `SANDBOX_MEMORY_MB`, `SANDBOX_DOCKER_*` |
| "Deployed" means the registry's LIVE alias | `LIVE_ALIAS` (a deployment port is scheduled, see `docs/OPEN-QUESTIONS.md`) |
| Frameworks the decision engine accepts: every framework with a built-in engine | `DECISION_SUPPORTED_FRAMEWORKS` |
| Torch budgets: 5 fine-tune epochs, 300 retrain epochs, Adam 1e-2 | `TORCH_FINE_TUNE_EPOCHS`, `TORCH_FULL_RETRAIN_EPOCHS`, `TORCH_LEARNING_RATE` |

Known limitation: `config effective` reports `env` as the source of a key that is set both by
keyword argument and in the environment (the init layer wins, but provenance cannot tell them
apart). The notification adapter is built at startup, but no code path sends a notification yet.

## 7. Unverified locally

- **The Docker sandbox backend has not been confirmed running.** Docker is not installed on this
  machine. With a faked `subprocess.run`, the unit tests check that non-default `SandboxLimits`
  values reach the `docker run` command line and the `docker rm` cleanup timeout
  (`test_phase15_sandbox.py::test_the_docker_backend_takes_its_limits_from_settings`). Nothing
  has executed that command. The two Docker integration tests skip here.
- The Kafka CDC adapter against a real broker and Debezium: covered only by unit tests on
  captured Debezium payloads.
- The webhook notifier against a real endpoint, and the Anthropic and Gemini adapters against
  the real APIs. No LLM call was made; tests inject fakes.
- PostgreSQL: `TEST_DATABASE_URL` is unset, so the suite ran on SQLite.
- The Linux CI path: pushed, not polled.
- The `file` secrets backend against a real Kubernetes or Docker secret mount. Tested through
  the full settings load with a temporary directory in the same layout
  (`test_config_system.py::test_the_file_secrets_backend_supplies_secret_keys`,
  `::test_the_file_secrets_backend_needs_an_existing_directory`).

## 8. Gate result

`scripts/verify.sh 1`: **PASS**, run twice locally.

| Run | Wall clock | Scoped `not heavy` tests | Smoke tier | Acceptance |
|---|---|---|---|---|
| after the burn-down commit | 186 s | passed (count not captured) | passed | 4/4 |
| final, with the two tests added for this report | **282 s** | 282 passed, 4 skipped (167 s) | 163 passed (49 s) | 4/4 |

Both runs had ruff and mypy clean (102 source files), the import-boundary test (2 passed) and the
no-gaps lint. The final run is 18 s under the 300 s budget. The margin is thin because Phase 1's
scope includes `core`, which every test file imports, so the scoped step is effectively the whole
default tier. The run time also varies on this laptop (186 s to 282 s for the same tests). If
later phases that scope `core` exceed the budget, the fix is to move slow tests to `heavy`, not
to raise the budget.

Before the gate, the full default tier (`-m "not heavy" -n 2`) ran 293 passed, 1 failed,
5 skipped in 109 s (117 s wall). The failure was
`test_phase11_docker_sandbox.py::test_adapt_via_llm_sandbox_backend_defaults_to_subprocess`,
which pinned the function-level default that B7 removes. It was replaced by a test that checks
the parameter is required and the schema default is still `subprocess`. That file then passed
4/4, and the gate above re-ran it. The 5 skips are the Docker tests (no Docker CLI here).
