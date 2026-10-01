# Writing an adapter

Every external system the framework talks to sits behind a **port**: a `typing.Protocol` in
`oran_adapt.ports`. An **adapter** implements a port for one system. When a site's stack is not
covered, the answer is an adapter, installed as its own package, never an edit to the core
(ADR-0001). This guide covers what every adapter has in common. Each port's own contract is in
its `docs/adapters/<port>.md`.

## 1. The pieces

| Piece | What it is | Where |
|---|---|---|
| Port | the protocol the core calls | `src/oran_adapt/ports/` |
| `Capability` | the adapter's descriptor: `port`, `adapter` (the name a selector key picks), `description`, `features`, `config_keys`, `required_keys`, `production_keys`, `distributions` | `oran_adapt.ports.Capability` |
| `AdapterSpec` | `AdapterSpec(capability=..., factory=...)`; the factory takes the validated `Settings` (deployment adapters also take the model registry) and returns the adapter | `oran_adapt.ports.AdapterSpec` |
| Entry point | registers the spec under the group `oran_adapt.<port>`; its name is the selector value | the adapter package's `pyproject.toml` |
| Conformance suite | the checks every adapter of the port must pass | `oran_adapt.conformance.<port>` |

The selector key for each port, its default and the adapters shipped are in
[capability-matrix.md](capability-matrix.md) (generated from the descriptors) and in the ADRs
under [adr/](adr/README.md).

### Rules

- **Imports.** Import the backend's SDK only inside the adapter module, never in the package
  `__init__`. Listing adapters (`GET /api/v1/capabilities`, `oran-adapt config effective`) loads
  every entry point, including where the SDK is not installed. First-party adapters live under
  `oran_adapt/adapters`, and the import-boundary test fails the build if core code imports an
  SDK.
- **No hardcoding.** Hosts, paths, names, timeouts and limits come from configuration.
  - A first-party adapter adds typed fields to `Settings` and lists them in `config_keys`.
  - A third-party package reads its own prefixed environment variables, as the cookiecutter
    shows.
- **Fail fast, name the key.** Put every key the adapter cannot work without in
  `required_keys`: startup and `oran-adapt config lint` then refuse a configuration that
  lacks one, naming it. Keys whose default points at a local development path go in
  `production_keys`; `ENVIRONMENT=production` requires them to be set explicitly.
- **Features, not names.** The core tests `features` (e.g. `traffic_split`, `aliases`) and
  never the adapter's name. Declare only what the adapter really does: a feature it lacks makes
  the configuration fail validation, not a rollout fail at run time. Mark test doubles with the
  `development_only` feature; production configurations refuse them.
- **Typed errors.** Raise the framework's errors (`RegistryUnavailableError`,
  `DeploymentError`, `PermissionDeniedError`, ...), which the core maps to retries and HTTP
  status codes. No bare `except`, and no silent fallback.
- **Outbound HTTP.** Go through `OutboundPolicy.from_settings(settings).client(...)`, so SSRF
  protection, TLS rules and allowlists apply (see [security.md](security.md)).

## 2. Start from a template

| Port | Template | Kind |
|---|---|---|
| `registry` | `templates/registry-adapter` | **cookiecutter** project: package, entry point, README and a conformance test; renders to a working adapter |
| `auth`, `secrets` | `templates/auth-adapter` | adapter modules |
| `llm` | `templates/llm-adapter` | adapter module |
| `model_type` | `templates/model-type-adapter` | plugin module |
| `rollout_metrics` | `templates/rollout-metrics-adapter` | adapter module |
| `deployment` | `templates/bentoml-service` | a serving-side receiver for the `webhook`/`bentoml` contract (no adapter needed) |
| `notification` | `templates/notification-receiver` | a receiver for the `webhook` sink |
| `job_queue` | `templates/job-queue-worker` | the Celery app the `celery` queue talks to |

Generate the registry project with cookiecutter:

```sh
pip install cookiecutter        # in a virtual environment
cookiecutter templates/registry-adapter
cd <project_slug> && pip install -e ".[test]" && pytest
```

As generated, the project is a working single-writer registry over a local directory. It passes
the conformance suite before you change anything, and
`tests/unit/test_phase2_registry.py::test_the_template_renders_an_adapter_that_passes_conformance`
renders it in the gate to keep it that way. Port it by replacing the storage helpers.

For any other port, render the same cookiecutter and change three things:

- the entry-point group in `pyproject.toml`, to `oran_adapt.<port>`;
- the adapter module, taken from the port's template or written against its
  `docs/adapters/<port>.md` "Writing a new adapter" section;
- the conformance import in `tests/test_conformance.py`, to `oran_adapt.conformance.<port>`.

## 3. Prove it: the conformance suite

Each port's suite (`oran_adapt.conformance.<port>`) exposes a `Context`, a `CHECKS` dict (check
name → function) and `run(adapter, ctx)`, which runs every check in order and returns their
names. A failing check raises `ConformanceFailure` saying which rule broke. The rules are
listed with their check names in each `docs/adapters/<port>.md`.

```python
from oran_adapt.conformance import policy as suite

def test_my_policy_passes_conformance():
    assert suite.run(MyPolicy(...), suite.Context()) == list(suite.CHECKS)
```

In this repository, **an installed adapter that no conformance test covers fails the gate**
(`tests/unit/test_phase13_conformance.py`). A new first-party adapter needs:

- a row in `COVERAGE` in `tests/unit/conformance_coverage.py`, naming the test that runs the
  suite on it; or
- an entry in `EXEMPT`, with a reason, an owner and an expiry date, in the rare case that the
  suite cannot run locally. An expired exemption fails the gate.

Adapters for cloud services are tested against a local emulator of the service's API. Name
what that leaves unverified in the adapter's docs.

## 4. Register, select, check

1. Install the package into the same environment as oran-adapt (`pip install -e .`). Entry
   points are read at startup, so no framework change is needed.
2. Select it with the port's selector key (`REGISTRY_BACKEND=<name>`, ...), in the
   environment or in the TOML file named by `ORAN_CONFIG_FILE`.
3. Run `oran-adapt config lint FILE` to check the file, including required keys and the
   feature checks. `GET /api/v1/capabilities` then lists the adapter with its descriptor.

For a first-party adapter, also:

- regenerate the capability matrix: `python scripts/capability_matrix.py`
  (`test_capability_matrix_is_current` fails until you do);
- add its keys to `.env.example`;
- record why the default is (or is not) changing in an ADR.
