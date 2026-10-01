# LLM adapters, the guard and prompts

The LLM is **optional**. With `LLM_ENABLED=false` (the default) `bootstrap.build_llm` returns
`None`, the decision engine picks the strategy by its rules and the adaptation step uses the
native engines only. Nothing is sent anywhere: `tests/unit/test_phase10_llm.py` runs the whole
pipeline with every outbound connection and name lookup failing the test.

Turning it on takes `LLM_ENABLED=true` **and** an adapter in `LLM_PROVIDER`; `LLM_ENABLED=true`
with `LLM_PROVIDER=none` is refused at startup.

An **LLM adapter** implements `oran_adapt.ports.LLMPort` (`complete(*, system, prompt) -> str`).
The core finds adapters through the `oran_adapt.llm` entry-point group.

| Adapter | Module | Talks to | Needs | Verified |
|---|---|---|---|---|
| `anthropic` | `adapters/llm_providers.py` | Messages API (`anthropic` SDK, `httpx2` client) | `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`; `ANTHROPIC_BASE_URL` optional | local wire-format double; live service unverified locally |
| `gemini` | `adapters/llm_providers.py` | `generate_content` (`google-genai` SDK) | `GEMINI_API_KEY`, `GEMINI_MODEL`; `GEMINI_BASE_URL` optional | local wire-format double; live service unverified locally |
| `openai-compatible` | `adapters/llm_providers.py` | `POST {base}/chat/completions`; covers vLLM, Ollama, LM Studio, llama.cpp server, LiteLLM and most gateways | `LLM_OPENAI_BASE_URL`, `LLM_OPENAI_MODEL`; `LLM_OPENAI_API_KEY` optional | local wire-format double; no live server tried |

For a model that must not leave the site, use `openai-compatible` with a self-hosted server.

## What `build_llm` returns

```
GuardedLlmClient            (llm/guard.py: caps, retries, breaker, usage ledger)
  └ InstrumentedLlmClient   (llm/client.py: latency and error metrics)
      └ the adapter         (anthropic | gemini | openai-compatible | a plugin)
```

The guard does the following, in order, on every call:

1. **Input cap.** It estimates the prompt's tokens (`LLM_CHARS_PER_TOKEN`). A prompt over
   `LLM_MAX_INPUT_TOKENS` is refused before anything is sent.
2. **Budget.** It reads the spend in the last `LLM_BUDGET_WINDOW_S` from the `llm_usage`
   table, which every API process and worker share. If this call could take the spend past
   `LLM_TOKEN_BUDGET` or `LLM_COST_BUDGET`, it is refused. "Could" means the input plus
   `LLM_MAX_OUTPUT_TOKENS` of output, priced at `LLM_COST_PER_1K_*`. A ledger that cannot be
   read also refuses the call, so an unreadable budget never leads to overspending.
3. **Circuit breaker.** One breaker per provider per process. After
   `LLM_BREAKER_FAILURE_THRESHOLD` failed calls in a row it opens and refuses calls unsent.
   After `LLM_BREAKER_RESET_S` it lets one trial call through: success closes it, failure
   opens it again.
4. **Retries.** It retries `LLM_MAX_RETRIES` times (default 0), with backoff
   `LLM_RETRY_BACKOFF_S * 2^attempt`. SDK retries are turned off so the retries happen only
   here.
5. **Usage.** It records the provider's token counts, or an estimate flagged `estimated`,
   with the cost, in `llm_usage`. Each row carries the provider, prompt id, prompt version,
   job id and outcome. It also feeds the metrics `llm_tokens_total`, `llm_cost_total`,
   `llm_calls_refused_total{reason}`, `llm_circuit_open` and
   `llm_fallbacks_total{reason}`.

`LLM_TIMEOUT_S` bounds each request.

## Every failure falls back to the rules

A failure never fails a job. Each one is recorded with a **fallback reason**:

| Reason | When |
|---|---|
| `disabled` | no LLM configured |
| `unavailable` | timeout, connection error, HTTP error, outbound policy refusal, or a provider bug (`LlmUnavailableError`) |
| `circuit_open` | the breaker refused the call (`LlmCircuitOpenError`) |
| `budget_exceeded` | an input, token or cost cap refused the call (`LlmBudgetExceededError`; `context.cap` names the cap) |
| `invalid_output` | the strategy reply is not the strict JSON shape |
| `outside_compatible_set` | the strategy reply names a strategy the constraints ruled out |
| `unsafe_code` / `sandbox_failed` | generated adaptation code was refused by the static check, or failed in the sandbox |

The reason is recorded in four places:

- **The decision:** `Decision.llm = {prompt_id, prompt_version, prompt_sha256, used,
  fallback_reason}`.
- **The job:** `JobResult.llm_calls`, one entry per call, with the prompt stamp, outcome,
  error code, provider, tokens, cost and duration.
- **The audit trail:** `ADAPTER_GENERATED` / `SANDBOX_EXECUTED` entries carry
  `{"fallback_reason": ...}`.
- **The fallback counter.**

When the LLM adaptation path fails for a model that can be fully retrained, the job retrains
natively. The candidate's `adaptation_note` records why.

## Prompts are versioned

The built-in prompts are `strategy-selection@1` and `adaptation-code@1` (`llm/prompts.py`).

- `LLM_PROMPT_DIR` adds versions from files named `<id>@<version>.txt`.
- `LLM_PROMPT_VERSIONS` pins a version per id. The default is the newest version, with
  numeric versions compared as numbers.
- An unknown id or version is a configuration error.

Every call is stamped with the prompt's id, version and SHA-256, so an operator can tell which
prompt text produced a decision.

## Writing an adapter

Start from `templates/llm-adapter/`. The rules, which `oran_adapt.conformance.llm` checks:

| Check | Rule |
|---|---|
| `protocol` | implements `LLMPort` |
| `returns_text` | returns the provider's text (non-empty) |
| `failures_typed` | every failure (unreachable endpoint, HTTP error, bad payload) is `LlmUnavailableError` with a `reason`; no SDK exception crosses the port |
| `errors_do_not_echo_key` | no error repeats the API key (record the exception *type* and HTTP status only) |
| `pickles` | pickles to its configuration and rebuilds its client (a job worker process receives it) |
| `usage_reported` | calls `llm.calls.report_usage(input, output)` when the provider returns token counts |

Also:

- Get HTTP clients from `OutboundPolicy.client` or `client_from`, never from
  `httpx.Client` / `httpx2.Client` directly; a test enforces this.
- Do not retry inside the adapter.
- Import the vendor SDK only inside the adapter.

```python
import pytest
from oran_adapt.conformance.llm import LLM_CHECKS, LlmContext

@pytest.mark.parametrize("check", sorted(LLM_CHECKS))
def test_my_llm(check):
    LLM_CHECKS[check](MyLlm(url), LlmContext(unreachable=lambda: MyLlm(dead_url), secret=KEY))
```

`tests/unit/llm_doubles.py` serves the three providers' wire formats and the template's format
on 127.0.0.1.

## Live smoke

`test_live_provider_smoke` is `heavy` and skipped unless `ORAN_LLM_LIVE=1`. It reads the real
configuration (`LLM_ENABLED`, `LLM_PROVIDER`, key, model) and makes one call. It is never part
of a phase gate.
