# Hardening Phase 15 report: final audit and production readiness

Branch `phase14-production-hardening`. Everything marked "passed" below was run locally on
Windows 11 with Python 3.13.7, CPU only, on 2026-10-01. CI was not polled.

**Ran for real:**
- `scripts/audit.py`: the hardcoding scan (53 baseline literals, 26 source probes over `src/`,
  12 infrastructure probes) and the findings matrix (10 rows, 21 named tests, 76 cases);
- startup and `config lint` refusing each key that lost its default, and loading
  `.env.example` unchanged;
- a link and heading-anchor check over every relative link in `docs/` (155 links).

**Unverified locally:**
- `docker compose up` with the new compose variables, and Kafka Connect resolving the
  connector's `${env:...}` placeholders, because there is no Docker on the laptop. Both files
  parse, and static tests check them;
- the MLflow image built from `docker/mlflow/requirements.txt`;
- the docker sandbox with a digest-pinned image.

## 1. Findings closed

| Item | What changed |
|---|---|
| C4, C5: default LLM model ids (`claude-sonnet-5`, `gemini-3.6-flash`) | No default. `ANTHROPIC_MODEL` and `GEMINI_MODEL` are required with their provider; `llm_providers._model` names the missing key |
| C6: `SANDBOX_DOCKER_IMAGE` defaulted to `oran-adapt-sandbox:latest` | No default. `SANDBOX_BACKEND=docker` requires the image pinned by digest (`name@sha256:...`), checked by `Settings._sandbox_image_pinned`; the runner refuses an empty image |
| C10, C11: default CDC topic and consumer group | No default. Both are required keys of the `kafka` CDC source, in its descriptor and in `KafkaCdcSource` |
| C12, C14 | Decisions recorded in `docs/OPEN-QUESTIONS.md`: `MLFLOW_SKOPS_TRUSTED_TYPES` keeps its name; threshold and budget keys stay typed, bounded schema defaults |
| D4-D6: compose names, tags and ports | `docker-compose.yml` takes them from variables with defaults (compose section of `.env.example`); the Debezium connector takes its user, database, table and topic prefix from `${env:...}` |
| D7: MLflow image pins inline in the Dockerfile | `docker/mlflow/requirements.txt`, kept equal to `requirements.lock` by a test |
| No final audit | `docs/AUDIT-HARDCODING.md`, `docs/AUDIT-FINDINGS.md`, `docs/PRODUCTION-READINESS.md`, checked by `scripts/audit.py`; `scripts/verify.sh all` runs every gate and the audit |

## 2. Ports and adapters

No new ports or adapters. The `kafka` CDC source gained a required key (`cdc_consumer_group`).

## 3. Configuration keys

| Key | Default | Meaning |
|---|---|---|
| `ANTHROPIC_MODEL` | – (was `claude-sonnet-5`) | required with `LLM_PROVIDER=anthropic` |
| `GEMINI_MODEL` | – (was `gemini-3.6-flash`) | required with `LLM_PROVIDER=gemini` |
| `SANDBOX_DOCKER_IMAGE` | – (was `oran-adapt-sandbox:latest`) | required with `SANDBOX_BACKEND=docker`, pinned by digest |
| `CDC_KAFKA_TOPIC` | – (was `oran.public.kpi_sample`) | required with `CDC_MODE=kafka` |
| `CDC_CONSUMER_GROUP` | – (was `oran-adapt-cdc`) | required with `CDC_MODE=kafka`, unique per installation |

The compose variables (`POSTGRES_USER`, `POSTGRES_DB`, `ORAN_ADAPT_VERSION`,
`MLFLOW_IMAGE_TAG`, `API_PORT`, `MLFLOW_PORT`, `PROMETHEUS_PORT`, `CONNECT_PORT`,
`MLFLOW_ALLOWED_HOSTS`, `CONNECT_GROUP_ID`, `CONNECT_TOPIC_PREFIX`, `CDC_CONNECTOR_NAME`,
`CDC_TOPIC_PREFIX`, `CDC_SOURCE_TABLE`) belong to the development stack, not to `Settings`.

**Migration:** a deployment that relied on one of the removed defaults now fails at startup
naming the key. Set it to the old value to keep the old behaviour (for the sandbox, the
image's digest).

## 4. Acceptance criteria

| Criterion | Evidence | Status |
|---|---|---|
| Audit documents present and linked | acceptance check 1 (4 audit docs, 9 required docs, 155 links) | passed |
| Hardcoding audit: zero remaining | acceptance check 2; `AUDIT-HARDCODING.md` (2 rows kept with a reason) | passed |
| Every finding has modules, keys and a passing test | acceptance check 3: 21 named tests ran and passed (5 from the gate's report, 16 run by the audit) | passed |
| Last C and D items closed | acceptance check 4; `tests/unit/test_phase15_audit.py` | passed |
| Burn-down counters at zero | acceptance check 5: A 0, B 0, C 0, D 0 | passed |
| Production readiness checklist | `docs/PRODUCTION-READINESS.md` | documented |
| Compose stack and connector templating run | static tests only | unverified locally |

## 5. Hardcoding

The counters fall from C 7, D 5 to **A 0, B 0, C 0, D 0**. New literals:

| Where | Value | Why it is not a key |
|---|---|---|
| `docker-compose.yml` | service names, internal ports, the internal `mlflow` database | the development stack's own topology |
| `scripts/audit.py`, `scripts/acceptance/phase15.py` | probe patterns, required docs | acceptance criteria |

## 6. Assumptions and defaults

- A model id, a sandbox image and a consumer group are site decisions, so they have no
  default; see `docs/assumption-inventory.md` and `docs/OPEN-QUESTIONS.md`.
- The compose stack is for development. Production uses the Helm chart or kustomize tree.

## 7. Unverified locally

Everything in the header's list.

## 8. Gate

`bash scripts/verify.sh 15`: **PASS in 207 s** (budget 300 s; 320 scoped and smoke tests
passed, acceptance 5/5). The log is in the session's scratchpad (`verify15.log`).

`bash scripts/verify.sh all` (the release check): **PASS in 682 s** (budget 3600 s). 861 tests
passed, 5 skipped; acceptance 1-15 all passed; both audit scans passed. The first two runs
found three stale checks, fixed before this run:

- `test_phase15_api_security.py`: `WRITE_POLICY` lacked Phase 14's
  `POST /adaptation/events/from/{mapper}` (role action `submit`, as the route declares);
- `scripts/secret_scan.py` read the compose user `${POSTGRES_USER:-oran}` as user `${POSTGRES_USER`
  with password `-oran}:${POSTGRES_PASSWORD}`. The user may now be a whole `${...}` placeholder;
  a literal password is still caught. The fake credential in a Phase 12 log-redaction test got
  the `secret-scan: allow` marker;
- `scripts/acceptance/phase1.py` linted the example configs from an empty directory, so their
  repo-relative `DELIVERY_POLICY_FILE` paths did not resolve. It lints from the repo root, as
  Phase 14's acceptance does.
