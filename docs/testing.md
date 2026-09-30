# Testing: conformance, mutation, scenarios, skips, containers

Hardening Phase 13 made the test suite itself a gate. This page covers the five parts and
how to run each one. The tiers stay the same as before:

- **smoke**: the whole tier runs in under 60 s.
- **gate** (unmarked): a phase's scoped run takes under 5 min.
- **heavy**: excluded by default; CI runs it with `-m "heavy or not heavy"`.

## 1. Every adapter passes its port's conformance suite

Each port has a suite under `src/oran_adapt/conformance/`. Phase 13 added the five that were
missing:

| Port | Suite | Checks |
|---|---|---|
| `artifact_store` | `conformance.artifact_store` | protocol, file and directory round trip, write-once, missing key, unsafe keys refused, picklable, unreachable store is `RegistryUnavailableError` |
| `model_handler` | `conformance.model_handler` | protocol, save/load round trip predicts the same, detects a foreign artifact, never overwrites, refuses an undeclared framework, typed error on an unloadable artifact |
| `cdc_source` | `conformance.cdc_source` | protocol, delivers changes, honours `limit`, acknowledged changes are not redelivered, unacknowledged ones are redelivered after a restart, unreachable source is `CdcUnavailableError` |
| `job_executor` | `conformance.job_executor` | protocol, returns the result, typed errors cross unchanged, a crash is raised (never swallowed), timeout, the tick callback can stop the job |
| `policy` | `conformance.policy` | protocol, answers every action, `authorize` agrees with `allowed`, deny by default, stable answers |

`tests/unit/conformance_coverage.py` maps each installed adapter to the test that runs it:

- `SUITES`: port → suite module.
- `COVERAGE`: port → (test file, test function, {adapter: parameter id}).
- `EXEMPT`: (port, adapter) → reason, owner and expiry. It is empty today.

**The gate.**
`test_phase13_conformance::test_every_installed_adapter_is_conformance_tested` reads the
`oran_adapt.<port>` entry points. It fails when an adapter is:

- neither covered nor exempt;
- exempt, but the exemption has expired;
- mapped to a test function that does not exist.

So an adapter cannot be registered without passing its port's suite.
`scripts/acceptance/phase13.py` then runs every mapped test with a JUnit report and requires
at least one passing case for each (port, adapter).

**Writing a new adapter:**

1. Register the entry point.
2. Add a parametrized case to the port's conformance test.
3. Add the adapter to `COVERAGE`.

Each suite's test file also has a "suite catches …" test. It feeds the suite a deliberately
broken adapter (for example one that overwrites, loses data on a round trip, commits before
storing, swallows a crash, or allows by default) and asserts the suite fails it.

## 2. Mutation testing of the gate and the state machine

`scripts/mutation.py` mutates in process and needs no extra packages. For each function in
`TARGETS` it parses the source and applies one operator at one site, then swaps the
function's code object. It runs every kill test in `tests/unit/mutation_kills.py`, then
restores the original.

The operators:

- comparison swaps (`<`↔`<=`, `==`↔`!=`, `in`↔`not in`, …);
- `and`↔`or`;
- dropping `not` and unary minus;
- arithmetic swaps;
- `True`↔`False`;
- `n`→`n+1`;
- `min`↔`max`, `any`↔`all`;
- dropping an element from a set literal or from a membership tuple.

```
python scripts/mutation.py                 # both targets, fails below --min-score 1.0
python scripts/mutation.py --target gate --json
python scripts/mutation.py --write-golden  # regenerate tests/unit/mutation_golden.json
```

**Targets:**

- `oran_adapt.core.state_machine`: the transition table and its checks.
- `oran_adapt.validation.gate`: statistics, guards and `decide`.

The two timing helpers behind the latency guardrail are left out on purpose
(`NOT_MUTATED`), because a timing assertion would make the kill tests flaky.
`test_phase13_mutation` checks that nothing else in the gate module is left out.

**Score.** The score is the share of mutants killed, not counting mutants listed in
`EQUIVALENT`. Each `EQUIVALENT` entry names the function, the operator and the source line,
and says why no input can tell the mutant apart from the original. For example, `np.where`
already guards a zero denominator. `test_phase13_mutation` checks four things:

- every entry still matches a surviving mutant;
- the score is 1.0;
- weaker kill tests would score below 0.5;
- the originals are restored after a run.

## 3. Scenario matrix

`tests/unit/scenario_matrix.py` maps each end-to-end scenario to the tests that prove it:

- shadow;
- gate rejects a marginal candidate;
- gate passes → canary → promote → verified;
- canary breach → automatic rollback;
- duplicate event;
- stale version;
- cancellation;
- worker killed;
- large dataset;
- sequence model;
- unsupported model type;
- egress blocked;
- each dependency unavailable in turn.

`DEPENDENCIES` names the test that takes down each dependency:

- database, model registry, artifact store, job queue;
- CDC broker, rollout metrics, LLM provider, serving system;
- HTTP dataset, notification sink, Vault, OIDC issuer.

Each outage must end in that dependency's typed error. A notification sink's error must be
retryable, and a database or registry outage is requeued with backoff by the worker.

`test_phase13_scenarios` checks three things:

- the matrix names every scenario;
- every referenced test exists;
- every scenario has at least one non-heavy test.

The acceptance script runs the non-heavy tests in the matrix and fails the gate if they take
5 minutes or more.

## 4. No skip without an owner and an expiry

Every `skip`, `skipif`, `importorskip` and `xfail` reason must carry a tag:
`[owner=<who> expires=YYYY-MM-DD]`. The policy is enforced in two places:

- **At run time.** A hook in `tests/conftest.py` fails any skip or xfail whose reason is
  untagged or past its expiry.
- **In the source.** `test_phase13_skip_policy` reads every test file with the AST, so a skip
  that did not fire on this host is still checked.

To keep a skip past its date, change the date in the reason. That change is visible in the diff
and in review.

## 5. Real services with testcontainers

`tests/integration/test_testcontainers.py` starts PostgreSQL and Kafka and runs:

- the migrations and the polling CDC conformance suite on PostgreSQL;
- the Kafka CDC conformance suite against a real broker, where "unreachable" means the broker
  container is stopped;
- a PostgreSQL outage, which must be `DatabaseUnavailableError`.

It needs a Docker daemon and the `containers` extra (`pip install -e ".[containers]"`). The CI
job `containers` in `.github/workflows/ci.yml` runs it. On a host without Docker it skips, with
a tagged reason.

## Running the Phase 13 gate

```
bash scripts/verify.sh 13
```

This runs ruff, mypy, the import boundary, the no-gaps lint, the conformance, mutation,
scenario and skip-policy tests (`-n 2`), the smoke tier, and `scripts/acceptance/phase13.py`.
