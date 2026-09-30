# ADR-0105: Default LLM_PROVIDER is `none`

- **Status:** Accepted
- **Selector:** `LLM_PROVIDER`
- **Default:** `none`
- **Alternatives shipped:** `anthropic`, `gemini`, `openai-compatible`
- **Open question:** [OPEN-QUESTIONS.md](../OPEN-QUESTIONS.md#llm)

## Context

An LLM can explain and propose adaptation strategies, but it may not be allowed to receive network data from the RIC environment, and it adds cost and latency.

## Decision

`none`: the deterministic rules decide and explain. `anthropic`, `gemini` and `openai-compatible` ship behind a guard (caps, budget, circuit breaker, recorded fallbacks); any failure falls back to the rules.

## Consequences

The framework makes the same decision on every run with no outbound call. Turning an LLM on is an explicit, reviewable configuration change.

## Revisit when

The operator approves a provider and a data-handling policy for it.
