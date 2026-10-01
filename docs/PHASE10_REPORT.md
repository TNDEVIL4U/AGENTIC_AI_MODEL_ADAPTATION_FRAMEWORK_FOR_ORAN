# Hardening Phase 10 report: the LLM is optional and fenced

Branch `phase14-production-hardening`. Everything marked "passed" below was run locally on
Windows 11 with Python 3.13.7, CPU only, on 2026-09-30. CI was not polled. What ran for real
and what used doubles:
- **Ran for real:**
  - the whole adaptation pipeline (decision, native adaptation, validation gate, MLflow
    registration) with every outbound connection and name lookup failing the test;
  - the guard: caps, the token and cost budget over a real SQLite `llm_usage` table shared by a
    pickled copy of the client, retries, the circuit breaker;
  - the three provider adapters' real SDK or HTTP code (`anthropic` 1.7.0 over `httpx2`,
    `google-genai`, `httpx`) talking to the local wire-format doubles.
- **Local doubles:** `tests/unit/llm_doubles.py` is a real HTTP server on 127.0.0.1 that speaks
  the Anthropic Messages API, Gemini `generate_content`, OpenAI chat completions and the
  template's `/generate` format, including errors, malformed bodies and usage counts.

**Unverified locally:**
- the live Anthropic, Gemini and OpenAI-compatible services (vLLM, Ollama, LM Studio, LiteLLM),
  since the gate runs with egress blocked by design. `test_live_provider_smoke` is `heavy` and
  runs only with `ORAN_LLM_LIVE=1`; it was not run;
- the quality of any model's strategy choices or generated code. Only the fences are tested:
  shape, safety, sandbox and fallback.

## 1. Findings closed

| Finding | Closed by |
|---|---|
| Finding 8: the LLM assumes internet access; `.env.example` shipped `LLM_PROVIDER=anthropic`, so copying it without a key failed startup | `LLM_ENABLED` (default `false`) switches the LLM on; `LLM_PROVIDER` only names the adapter. With the switch off, `bootstrap.build_llm` returns `None`, the provider's keys are not demanded (`ENABLE_SWITCHES` in `core/config.py`) and nothing is sent anywhere. `LLM_ENABLED=true` with `LLM_PROVIDER=none` is refused at startup. `.env.example` now ships `LLM_ENABLED=false`, `LLM_PROVIDER=none` |
| Only Anthropic and Gemini SaaS; no self-hosted option | LLM providers are adapters in the `oran_adapt.llm` entry-point group (`adapters/llm_providers.py`): `anthropic`, `gemini` and `openai-compatible` (`POST {base}/chat/completions`, which covers vLLM, Ollama, LM Studio, llama.cpp server and LiteLLM). Base URLs are keys for all three. Plugins register the same way (`templates/llm-adapter/`) |
| No circuit breaker, token or cost cap | `llm/guard.py`, `GuardedLlmClient`, wraps every adapter. In order: the input cap, the token and cost budget over a window (read from the `llm_usage` table, migration 0010, which every API process and worker share; an unreadable ledger refuses), the per-provider circuit breaker (opens after N failures, half-opens after a reset time), retries with exponential backoff (SDK retries off), and a usage row per call with provider, prompt stamp, job id, tokens, cost and outcome |
| No prompt versioning | `llm/prompts.py`: `strategy-selection@1` and `adaptation-code@1` built in; `LLM_PROMPT_DIR` adds `<id>@<version>.txt`; `LLM_PROMPT_VERSIONS` pins; unknown id or version is a configuration error. Every call is stamped with id, version and SHA-256 |
| An LLM failure in adaptation propagated as `LlmUnavailableError` | Every failure degrades to the rules with a recorded **fallback reason** (`disabled`, `unavailable`, `circuit_open`, `budget_exceeded`, `invalid_output`, `outside_compatible_set`, `unsafe_code`, `sandbox_failed`) on `Decision.llm`, `JobResult.llm_calls`, the audit trail (`ADAPTER_GENERATED` / `SANDBOX_EXECUTED`) and `llm_fallbacks_total{reason}`. A failed LLM adaptation for a fully retrainable model retrains natively; `adaptation_note` says why |
| **New:** MLflow sends usage telemetry to `config.mlflow-telemetry.io` from inside the registry adapter; the egress-blocked e2e caught it | `MLFLOW_TELEMETRY` (default `false`): the MLflow adapter sets `MLFLOW_DISABLE_TELEMETRY` and drops the telemetry client MLflow starts on import |
| **New:** every model save ran MLflow's `infer_pip_requirements`, a subprocess that imports the model (about 20 s per save on this laptop) | `MLFLOW_PIP_REQUIREMENTS`: when set, the list is written into the saved model's environment and inference is skipped. Default empty: MLflow infers, as before |
| **New:** the Gemini SDK's automatic function calling could loop inside the adapter | turned off (`AutomaticFunctionCallingConfig(disable=True)`); the adapter makes plain completions only |

REST contract: additive only. `Decision.llm` and `JobResult.llm_calls` are new optional fields.
Migration `0010_llm_usage` adds one table. The existing round trip
(`test_phase1_foundation.py::test_migration_up_and_down`) takes it up and down.

## 2. Ports and adapters

`LLMPort.complete(*, system, prompt) -> str` is unchanged. What `build_llm` returns is
`GuardedLlmClient` → `InstrumentedLlmClient` → the adapter.

| Port | Adapter | Verified |
|---|---|---|
| llm | `anthropic` | local Messages API double (real SDK over `httpx2`) |
| llm | `gemini` | local `generate_content` double (real `google-genai` SDK) |
| llm | `openai-compatible` | local chat-completions double |
| llm | template `my-llm` (`templates/llm-adapter/`) | local `/generate` double |

Extension path:
- `templates/llm-adapter/` (adapter and README);
- `docs/adapters/llm.md`;
- the conformance suite `oran_adapt.conformance.llm` (`LLM_CHECKS`): protocol, returns text,
  failures typed, errors do not echo the key, pickles, usage reported. Every adapter above
  passes it.

HTTP clients come only from the outbound policy (`client_from` / `OutboundPolicy.client`); a
test scans the source for any other `httpx` or `httpx2` client.

## 3. Configuration keys

| Key | Type | Default | Required |
|---|---|---|---|
| `LLM_ENABLED` | bool | `false` | no |
| `LLM_PROVIDER` | `anthropic`\|`gemini`\|`openai-compatible` (+ plugins) | `none` | when `LLM_ENABLED=true` |
| `ANTHROPIC_BASE_URL` / `GEMINI_BASE_URL` | URL | unset (SDK default) | no |
| `LLM_OPENAI_BASE_URL` / `LLM_OPENAI_MODEL` / `LLM_OPENAI_API_KEY` | URL / str / secret | unset | URL and model when `openai-compatible` |
| `LLM_TIMEOUT_S` / `LLM_MAX_OUTPUT_TOKENS` | float / int | 60 / 2048 | no |
| `LLM_MAX_RETRIES` / `LLM_RETRY_BACKOFF_S` | int / float | 0 / 0.5 | no |
| `LLM_BREAKER_FAILURE_THRESHOLD` / `LLM_BREAKER_RESET_S` | int / float | 3 / 60 | no |
| `LLM_MAX_INPUT_TOKENS` | int (0 = no cap) | 16000 | no |
| `LLM_TOKEN_BUDGET` / `LLM_COST_BUDGET` | int / float (0 = no cap) | 0 / 0 | no |
| `LLM_BUDGET_WINDOW_S` | int | 86400 | no |
| `LLM_COST_PER_1K_INPUT_TOKENS` / `LLM_COST_PER_1K_OUTPUT_TOKENS` | float | 0 / 0 | for a cost budget |
| `LLM_CHARS_PER_TOKEN` | float | 4 | no |
| `LLM_PROMPT_VERSIONS` / `LLM_PROMPT_DIR` | dict / path | `{}` (newest) / unset | no |
| `MLFLOW_TELEMETRY` | bool | `false` | no |
| `MLFLOW_PIP_REQUIREMENTS` | list | `[]` (MLflow infers) | no |

All of these keys are documented in `.env.example`, commented out below the active
`LLM_ENABLED=false` and `LLM_PROVIDER=none`.

## 4. Acceptance criteria

`scripts/acceptance/phase10.py`, over `tests/unit/test_phase10_llm.py` (32 tests: 31 in the
gate, plus the heavy live smoke):

| Criterion | Evidence | Result |
|---|---|---|
| Offline by default: egress-blocked e2e | settings, `.env.example` and `build_llm` checked directly; `test_llm_is_off_by_default*`, `test_enabling_the_llm_without_a_provider*`, `test_the_egress_blocker*`, `test_mlflow_telemetry*`, `test_the_pipeline_runs_end_to_end_with_egress_blocked` (REGISTERED, `llm_calls == []`, zero outbound attempts) | passed |
| Unreachable provider → rules, reason recorded | `test_an_unreachable_provider*` (reason `unavailable` on the decision, the job, the counter); `invalid_output`, `budget_exceeded`, `circuit_open`; `test_*retrains_instead*` (`unsafe_code` in the note, `llm_calls` and the audit trail); a valid LLM choice is stamped | passed |
| Cost cap enforced | input cap, token budget, cost budget (memory ledger, and SQL ledger shared by a pickled client), unreadable ledger refuses | passed |
| Adapters, guard, prompts | conformance × 3 providers + template; retries and backoff `[0.5, 1.0]`; typed last failure; breaker opens (gauge 1) and half-opens; prompts versioned, pinned and stamped; outbound policy refuses 169.254.169.254; no unchecked HTTP client | passed |
| Live smoke is heavy and off | AST check: `pytest.mark.heavy` and `skipif(ORAN_LLM_LIVE != "1")` | passed; **the live call itself unverified locally** |

Inside the gate, the acceptance checks 1–4 read the JUnit report of this gate's scoped test step
(`scripts/acceptance/_gate.py`) instead of running the same tests a second time. The `-k`
selection is evaluated with pytest's own expression parser against the recorded names. A check
fails if nothing matches or any match did not pass (a skip is not a pass), and the report must
be newer than the start of the gate run. The recorded counts equal the direct `pytest -k`
counts (5, 7, 5, 14). Run on its own, the script runs pytest as before.

## 5. Hardcoding

No baseline item reopened, and the counts do not move. A1 and A2 (`llm/client.py` literals)
were already keys. C4 and C5 (default model ids `claude-sonnet-5`, `gemini-3.6-flash`) stay
open: they apply only once `LLM_ENABLED=true`, and making them required is an operator-facing
change recorded in `docs/OPEN-QUESTIONS.md`. New literals, and why they are not keys:

| Where | Value | Why it is not a key |
|---|---|---|
| `llm/prompts.py` | the text of `strategy-selection@1`, `adaptation-code@1` | versioned built-ins; `LLM_PROMPT_DIR` and `LLM_PROMPT_VERSIONS` replace them |
| `adapters/llm_providers.py` | `/chat/completions`, message roles | the OpenAI chat completions wire format |
| `llm/guard.py`, `decision/llm_selector.py` | fallback reason names | the recorded vocabulary, documented in `docs/adapters/llm.md` |
| `adapters/registry/mlflow/registry.py` | `MLFLOW_DISABLE_TELEMETRY` | MLflow's own switch; whether it is set is `MLFLOW_TELEMETRY` |

## 6. Assumptions and defaults

Recorded in `docs/OPEN-QUESTIONS.md` ("LLM"):
- the LLM is off by default;
- there are no token or cost budgets by default (0), while the input cap is 16000 tokens;
- prices are 0, so cost is not tracked until they are set;
- the breaker is per process and per provider, not shared;
- retries default to 0;
- a token estimate of 4 characters per token is used when a provider reports no usage;
- the default model ids;
- MLflow telemetry is off.

`scripts/verify.sh` exports `LLM_PROVIDER=none`. The tests' autouse fixture resets the breakers
between tests.

## 7. Unverified locally

- The live Anthropic, Gemini and any OpenAI-compatible server. The doubles follow the documented
  wire formats; vendor quirks (rate-limit headers, streaming, safety blocks) are not exercised
  beyond an HTTP error status.
- Pricing accuracy: the cost budget is only as good as `LLM_COST_PER_1K_*`.
- The budget under concurrent writers on PostgreSQL. The check reads, then the call writes, so
  two processes can both pass a nearly spent budget; the overshoot is bounded by one call's
  maximum per concurrent caller. This was tested on SQLite only.
- The Docker image build still downloads from PyPI and `download.pytorch.org` at build time
  (Docker is not installed here); the running service needs no internet.

## 8. Gate

`bash scripts/verify.sh 10`: **PASS in 248 s** (budget 300 s).

| Step | Started at | Result |
|---|---|---|
| 1 ruff, mypy | 0 s | clean |
| 2 import boundary | 2 s | 2 passed |
| 3 no-gaps lint | 22 s | clean |
| 4 scoped tests (llm, adapters.llm_providers, conformance.llm, decision.llm_selector; 3 files) | 22 s | 65 passed in 64 s |
| 4 smoke tier (files not run above) | 103 s | 233 passed in 124 s |
| 5 acceptance (`scripts/acceptance/phase10.py`) | 244 s | 5/5 passed (3 s, 0 s, 0 s, 0 s, 0 s; recorded results) |

The first gate run used a wider scope, which also pulled in every file importing
`oran_adapt.decision` or `adaptation.llm_adapter`: `test_phase3_decision`,
`test_phase7_sandbox`, `test_phase11_docker_sandbox`, `test_phase14_stage_d` and the three
`test_phase15_*` decision and API files. All steps passed: 136 scoped tests, 218 smoke tests
and 5/5 acceptance. But it took 390 s, over the budget. The final scope keeps the three files
that import the LLM modules. The other files' smoke tests still run in the smoke tier.

`tests/unit/test_phase1_foundation.py` changed in this phase: its LLM-key validator test now
sets `LLM_ENABLED=true`. It imports no LLM module, so it is outside the scope. It was run on
its own, including the migration round trip through `0010`: 15 passed in 51 s.
