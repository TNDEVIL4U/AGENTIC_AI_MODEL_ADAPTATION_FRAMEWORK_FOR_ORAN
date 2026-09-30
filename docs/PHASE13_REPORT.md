# Hardening Phase 13 report: tests and conformance as a gate

Branch `phase14-production-hardening`. Everything marked "passed" below was run locally on
Windows 11 with Python 3.13.7, CPU only, on 2026-09-30. CI was not polled.

**Ran for real:**
- every conformance suite on every installed adapter: 65 adapters on 15 ports, 203 cases;
- the in-process mutation runner on the validation gate and the job state machine;
- the scenario matrix's non-heavy tests, including:
  - a 100 000-row CSV registered by reference and sampled under tracemalloc;
  - each outbound dependency (HTTP dataset, serving system, notification webhook, Vault,
    OIDC JWKS) pointed at a closed localhost port;
- the skip-policy hook and source scan.

**Doubles:** the same emulators and in-memory brokers the earlier phases' conformance tests use.
Examples: the Kafka CDC suite runs against an in-memory broker with committed offsets per group,
and the Celery, RQ and Kubernetes queues against fakes. The stale-version test uses a registry
that only answers which version is LIVE.

**Unverified locally.** There is no Docker daemon on the laptop, and testcontainers may not be
installed:
- `tests/integration/test_testcontainers.py`: migrations and the polling CDC suite on a real
  PostgreSQL, the Kafka CDC suite against a real broker, and a PostgreSQL outage. It runs in the
  new CI job `containers`.
- The heavy scenario tests. They run in CI's `quality` job (`-m "heavy or not heavy"`), not in
  the gate.

## 1. Findings closed

| Finding | What changed |
|---|---|
| Five ports had no conformance suite (`artifact_store`, `model_handler`, `cdc_source`, `job_executor`, `policy`) | A suite for each under `oran_adapt.conformance`, run on every installed adapter, plus a "suite catches a broken adapter" test for each |
| An adapter could be registered without passing its port's suite | `tests/unit/conformance_coverage.py` maps each (port, adapter) to its test. `test_every_installed_adapter_is_conformance_tested` fails on an entry point that is neither covered nor exempt (an exemption needs an owner and an unexpired date). The acceptance script requires each adapter's cases to pass |
| Found by the new suite: the filesystem artifact store let a raw `FileNotFoundError`/`NotADirectoryError` escape when its root was unreachable | `os.makedirs` moved inside the `try`; the error is now `RegistryUnavailableError` like every other store failure |
| Found by the new suite: the native model handler raised a bare `FileExistsError` when saving into an existing directory | It now raises `ArtifactError` ("refusing to save into an existing directory") |
| Nothing showed that the gate and state-machine tests would catch a regression | `scripts/mutation.py`: 193 mutants, all killed except 5 documented equivalents (score 1.0). Kill tests are in `tests/unit/mutation_kills.py`, with golden gate decisions in `mutation_golden.json` |
| No single place showed that each end-to-end scenario is tested | `tests/unit/scenario_matrix.py`: 13 scenarios mapped to 37 non-heavy tests (plus heavy ones), with 12 dependencies taken down in turn. New tests fill the gaps: stale version on a fast path, large dataset, and five outbound dependencies |
| Skips could hide missing coverage indefinitely | Every skip, skipif, importorskip and xfail carries `[owner=... expires=YYYY-MM-DD]`. The conftest hook fails an untagged or expired skip when it happens, and a source scan checks those that did not fire |

## 2. Ports and adapters

There are no new ports. New conformance suites:

- `oran_adapt.conformance.artifact_store`
- `oran_adapt.conformance.model_handler`
- `oran_adapt.conformance.cdc_source`
- `oran_adapt.conformance.job_executor`
- `oran_adapt.conformance.policy`

Each suite has a `CHECKS` mapping and a `run(port, ctx)` function, like the earlier suites.
`docs/testing.md` lists every check.

## 3. Configuration keys

There are no new runtime keys. New developer-facing items:

| Item | Meaning |
|---|---|
| `pip install -e ".[containers]"` | testcontainers (PostgreSQL, Kafka) and confluent-kafka for the containers tier |
| `python scripts/mutation.py [--target gate\|state_machine] [--min-score 1.0] [--json] [--write-golden]` | the mutation run |
| `[owner=<who> expires=YYYY-MM-DD]` in a skip reason | required by the skip policy |

## 4. Acceptance criteria

| Criterion (spec) | Evidence | Status |
|---|---|---|
| A conformance suite per port, gating adapter registration | acceptance check 1 (65 adapters, 15 ports, 203 cases); `test_every_installed_adapter_is_conformance_tested`, `test_the_gate_names_every_port_that_has_adapters` | passed |
| Mutation testing on the gate and the state machine | acceptance check 2: gate 157 mutants (4 equivalent), state_machine 36 (1 equivalent), score 1.0; `test_phase13_mutation` (including `test_weaker_kill_tests_let_mutants_survive` and `test_the_originals_are_restored`) | passed |
| testcontainers | `tests/integration/test_testcontainers.py`, CI job `containers` (acceptance check 5 checks the wiring) | written; unverified locally |
| Scenario matrix: shadow; gate rejects marginal; gate passes → canary → promote → verified; canary breach → auto-rollback; duplicate event; stale version; cancellation; worker kill; large dataset; sequence model; unsupported type; egress blocked; each dependency unavailable in turn | acceptance check 3: 13 scenarios, 37 non-heavy tests (45 cases) passed; `test_every_scenario_in_the_spec_is_mapped_to_tests_that_exist`, `test_every_scenario_has_a_fast_test`, `test_every_dependency_is_taken_down_in_turn` | passed |
| Non-heavy scenarios under 5 min | 55 s with 2 workers | passed |
| No skip without an owner and an expiry | acceptance check 4: 12 skip sites, all tagged; `test_phase13_skip_policy` (12 tests, including the run-time hook) | passed |

## 5. Hardcoding

No category count changes (A 0, B 0, C 7, D 5). New literals, and why they are not keys:

| Where | Value | Why it is not a key |
|---|---|---|
| `scripts/acceptance/phase13.py` | the 300 s scenario budget, `-n 2` | the spec's gate budget and the laptop's worker cap |
| `scripts/mutation.py` | `--min-score` default 1.0 | the gate's requirement; a flag for exploration |
| `tests/unit/scenario_matrix.py`, `conformance_coverage.py` | test ids | test inventory |
| skip reasons | `owner=TNDEVIL4U expires=2027-03-31` | a review date on each skip; change it in the diff |

## 6. Assumptions and defaults

- An equivalent mutant is accepted only with a written reason, and only while a mutant still
  survives on that exact line. A stale entry fails the gate.
- The two latency helpers in the gate are not mutated, because timing assertions would be
  flaky. A test checks that nothing else in the module is left out.
- "Large dataset" is 100 000 rows (4 MB) in the gate. The bound is relative: chunked reading
  must peak below half of what reading the whole file at once costs.
- A closed localhost port stands in for an unreachable dependency. DNS failures and timeouts go
  down the same `httpx.HTTPError` path.
- Each skip's owner is the repository owner, and each expiry is 2027-03-31.

## 7. Unverified locally

Everything in the header's list.

## 8. Gate

`bash scripts/verify.sh 13`: **PASS in 249 s** (budget 300 s). The log is in the session's
scratchpad (`verify13.log`).

| Step | Started at | Result |
|---|---|---|
| 1 ruff, mypy | 0 s | clean (mypy: 182 files) |
| 2 import boundary | 2 s | 2 passed |
| 3 no-gaps lint | 11 s | clean |
| 4 scoped tests (the new suites and the mutation tests; 2 files) | 12 s | 39 passed in 27 s |
| 4 smoke tier (files not run above) | 47 s | 320 passed in 79 s |
| 5 acceptance (`scripts/acceptance/phase13.py`) | 135 s | 5/5 passed: conformance 53 s, mutation 4 s, scenarios 55 s, skips 2 s, CI wiring 0 s |
