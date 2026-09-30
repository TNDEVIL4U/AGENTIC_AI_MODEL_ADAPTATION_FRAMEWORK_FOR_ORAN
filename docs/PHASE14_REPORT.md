# Hardening Phase 14 report: integration and documentation

Branch `phase14-production-hardening`. Everything marked "passed" below was run locally on
Windows 11 with Python 3.13.7, CPU only, on 2026-09-30. CI was not polled.

**Ran for real:**
- `oran-adapt config lint` on all nine files in `config/examples/`, seven of them the target
  stacks;
- regeneration of the capability matrix and the OpenAPI document, compared byte for byte with
  the committed files;
- the three shipped mappers on their sample payloads, over the API and the CLI;
- the clone-to-canary walkthrough in-process. It uses the real FastAPI app, pipeline, SQLite
  database, filesystem registry and `registry-alias` deployment. The input is
  `config/mappers/samples/alertmanager.json`, unchanged, and it ends with a candidate on 5 % of
  traffic and a duplicate alert refused;
- a link and heading-anchor check over every relative link in `docs/` (135 links).

**Doubles:** the `opa` adapter runs against an OPA Data API served by an httpx mock transport,
which answers, fails, times out and returns undefined decisions on demand.

**Unverified locally:**
- the walkthrough against the compose stack (`deploy/compose/walkthrough.yml`), because there
  is no Docker on the laptop;
- an OPA server running the Rego policy in `docs/adapters/auth.md`;
- each example config against its real service (KServe, SageMaker, Vertex AI, Seldon, Triton,
  BentoML). Each passes lint, and its adapters pass their conformance suites against
  emulators.

## 1. Findings closed

| Finding | What changed |
|---|---|
| Monitoring systems had to send the framework's own DriftEvent | Declarative drift mappers (`core/event_mapping.py`, ADR-0003): a TOML mapping file per source, loaded and checked at startup. `POST /api/v1/adaptation/events/from/{mapper}` and `oran-adapt event map`. Shipped: Alertmanager, Evidently, generic JSON, each with a sample payload |
| `policy` had a single adapter, against the Unknown-Stack Protocol | `adapters/opa.py`: Open Policy Agent over the Data API, failing closed, with a per-role cache. It passes `oran_adapt.conformance.policy`, and `conformance_coverage.py` covers it. No port has a single adapter now |
| Found while writing the examples: a strategy that needs a traffic split (`canary`, `ab`) with a deployment adapter that cannot split failed only when the deployer was built, and `config lint` passed it. Three drafted examples (SageMaker, Vertex, Seldon) had that mistake | `Settings._delivery_fits_deployment` checks the `traffic_split` feature, so lint and startup refuse it and name the adapters that can split. The check moved out of `bootstrap.build_deployer`, and the examples were corrected |
| No generated inventory of adapters | `scripts/capability_matrix.py` writes `docs/capability-matrix.md` from the adapters' descriptors. `--check` and `test_capability_matrix_is_current` fail when it is stale |
| The OpenAPI document was not versioned | `scripts/openapi.py` writes `docs/api/openapi.json` (36 paths). `--check` fails when it is stale |
| The defaults chosen for unknown stacks were spread through the docs | `docs/adr/`: 4 architecture records and one record per selector (15). `test_every_unknown_stack_default_has_an_adr` fails when a selector has no record or the record disagrees with `Settings` |
| Integration, authoring, operations and migration were undocumented | `docs/integration-guide.md`, `docs/adapter-authoring.md` (with the registry cookiecutter), `docs/operations/README.md`, `docs/migration-guide.md`, `docs/LIMITATIONS.md`, a policy section in `docs/adapters/auth.md`, and a finalised `docs/OPEN-QUESTIONS.md` |
| The gate took 418 s on the first run, over its 300 s budget | MLflow inferred pip requirements with a subprocess on every saved model (about 40 s each here). The walkthrough and the shared test fixture now pin `MLFLOW_PIP_REQUIREMENTS`, and `scripts/openapi.py` renders with the filesystem registry. The gate then took 201 s |

## 2. Ports and adapters

There are no new ports. There is one new adapter: `policy` → `opa` (entry point
`oran_adapt.policy:opa`). New entry points for input:

- `POST /api/v1/adaptation/events/from/{mapper}`, with the `submit` action;
- `oran-adapt event map --mapper NAME | --mapping FILE --input FILE`, with the options
  `--model-id`, `--model-version`, `--dataset` and `--submit`.

## 3. Configuration keys

| Key | Default | Meaning |
|---|---|---|
| `DRIFT_MAPPERS` | `{}` | mapper name → mapping file |
| `DRIFT_MAPPER_MAX_EVENTS` | 100 | the most DriftEvents one payload may produce |
| `POLICY_OPA_URL` | – | OPA's base URL; required with `POLICY_BACKEND=opa` |
| `POLICY_OPA_PATH` | `oran_adapt/authz/allow` | the rule asked |
| `POLICY_OPA_TOKEN` | – | a bearer token (a secret) |
| `POLICY_OPA_TIMEOUT_S` | 2.0 | the time allowed per question |
| `POLICY_OPA_CACHE_S` | 30 | how long a role's answer is cached; 0 asks every time |

There is a new validation: `DELIVERY_STRATEGY` against the selected `DEPLOYMENT_BACKEND`'s
`traffic_split` feature.

## 4. Acceptance criteria

| Criterion (spec) | Evidence | Status |
|---|---|---|
| Generated capability matrix | acceptance check 2; `test_capability_matrix_is_current` | passed |
| Integration guide with working monitoring → DriftEvent mappers | `docs/integration-guide.md` §1; acceptance check 3 (3 mappers, 3 events from the samples); 10 mapper tests in `test_phase14_integration.py` | passed |
| Promotion → serving adapters, including the webhook/GitOps escape hatch | `docs/integration-guide.md` §2; ADR-0004; the deployment conformance suite (earlier phases) | documented |
| Adapter authoring guide + cookiecutter | `docs/adapter-authoring.md`; `templates/registry-adapter`, rendered and conformance-tested by `test_the_template_renders_an_adapter_that_passes_conformance` (smoke tier) | passed |
| Ops docs | `docs/operations/README.md`, which links the images, Helm, kustomize, migrations and observability pages and the 12 runbooks | documented |
| Regenerated OpenAPI | acceptance check 2 (`docs/api/openapi.json`, 36 paths) | passed |
| ADRs, including every Unknown-Stack default | acceptance check 5 (19 ADRs, all indexed); `test_every_unknown_stack_default_has_an_adr` | passed |
| Migration guide from the pilot | `docs/migration-guide.md`: migrator, workers, gate policy, delivery, LLM, auth, storage, retired keys, and a checklist | documented |
| Example configs for MLflow+KServe, SageMaker, Vertex, Seldon, Triton, BentoML and air-gapped filesystem | acceptance check 1; `test_example_config_for_each_target_stack_lints` (7 cases) | passed |
| Finalised `OPEN-QUESTIONS.md` and `LIMITATIONS.md` | the selector table is complete, with ADR links; no single-adapter port remains; `LIMITATIONS.md` is written; acceptance check 6 | passed |
| Gate: every example config passes config-lint | acceptance check 1 | passed |
| Gate: capability matrix and OpenAPI regenerate with no diff | acceptance check 2 | passed |
| Gate: the clone-to-canary walkthrough runs against the local compose stack | acceptance check 7 runs it in-process. The compose mode is written, with an override file and the commands | in-process passed; compose unverified locally |

## 5. Hardcoding

No category count changes (A 0, B 0, C 7, D 5). New literals, and why they are not keys:

| Where | Value | Why it is not a key |
|---|---|---|
| `config/mappers/*.toml` | source field paths | configuration a site copies and edits |
| `config/examples/*.toml` | example hosts, buckets and strategies | worked examples that each pass lint |
| `scripts/walkthrough.py` | 300 synthetic rows, seeds, slopes, a 120 s local timeout, `scikit-learn` as the pinned requirement | a reproducible demonstration; `--timeout-s` sets the remote timeout |
| `scripts/acceptance/phase14.py` | the target stacks, the required docs, a 240 s walkthrough timeout | acceptance criteria |
| `adapters/opa.py` | `/v1/data/` | OPA's API contract |
| `tests/conftest.py` | `mlflow_pip_requirements=["scikit-learn"]` | a test fixture; production keeps inference unless the key is set |

## 6. Assumptions and defaults

- Mappers are data, not code. A source that needs computation needs a translator in front of the
  API.
- `opa` fails closed, and caches each role's answer for 30 s.
- The examples pick the safest strategy each adapter supports:
  - canary on KServe;
  - manual on SageMaker;
  - shadow on Vertex AI and BentoML;
  - blue/green on Seldon and Triton;
  - `registry-alias` when air-gapped.
- The walkthrough's local mode uses the inline queue, so the job runs in the request. The
  compose mode runs it in the `worker` service.
- MLflow's requirement inference is skipped in tests and the walkthrough. The unit tests no
  longer exercise it, and production behaviour is unchanged.

## 7. Unverified locally

Everything in the header's list.

## 8. Gate

`bash scripts/verify.sh 14`: **PASS in 201 s** (budget 300 s). The log is in the session's
scratchpad (`verify14.log`). An earlier run passed every check in 418 s and failed the budget;
see finding 7.

| Step | Started at | Result |
|---|---|---|
| 1 ruff, mypy | 0 s | clean (mypy: 184 files) |
| 2 import boundary | 2 s | 2 passed |
| 3 no-gaps lint | 12 s | clean |
| 4 scoped tests (`core.event_mapping`, `adapters.opa`; 1 file) | 13 s | 26 passed in 24 s |
| 4 smoke tier (files not run above) | 46 s | 320 passed in 78 s |
| 5 acceptance (`scripts/acceptance/phase14.py`) | 133 s | 7/7 passed: configs 2 s, regeneration 15 s, mappers 0 s, opa 0 s, ADRs 0 s, docs 0 s, walkthrough 50 s |
