# ADR-0105: Default LLM_PROVIDER is `gemini`, on by default

- **Status:** Accepted (supersedes the earlier default `none`)
- **Selector:** `LLM_PROVIDER`
- **Default:** `gemini`
- **Switch:** `LLM_ENABLED=true`, `GEMINI_MODEL=gemini-3.8-flash`
- **Alternatives shipped:** `anthropic`, `openai-compatible`, `none`
- **Open question:** [OPEN-QUESTIONS.md](../OPEN-QUESTIONS.md#llm)

## Context

The project's design has the Agentic AI Decision Engine consult an LLM to choose among the
strategies the hard constraints allow, and the Engine Selection step ask an LLM to write an
adaptation engine when the model type has no specific engine and no generic engine fits. An
LLM may not be allowed to receive data from the RIC environment, and it adds cost and latency.

## Decision

`gemini`, switched on by default. The provider sits behind the guard (caps, budget, circuit
breaker, recorded fallbacks), its strategy choice is validated against the allowed set, and
generated code is AST-checked and run only in the sandbox; any failure falls back to the rules.
A missing `GEMINI_API_KEY` is not a startup error: `build_llm` returns `None`, the rules decide,
and nothing is sent anywhere.

## Consequences

With a key, decisions can differ between runs (the LLM's choice is recorded with its
confidence and rationale). Without a key, the framework makes the same decision on every run
with no outbound call. `LLM_ENABLED=false` or `LLM_PROVIDER=none` turns the LLM off.

## Revisit when

The operator's data-handling policy forbids sending drift evidence to an external provider:
then use `openai-compatible` with a self-hosted model, or `none`.
