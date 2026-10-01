# OranAdaptLlmBudgetExhausted

**Fires when** LLM calls were refused in the last hour because `LLM_TOKEN_BUDGET` or
`LLM_COST_BUDGET` was reached (`llm_calls_refused_total{reason=~"token_budget|cost_budget"}`).

**Impact.** Small: the LLM is optional and fenced. Decisions fall back to the rule-based path,
recorded as a fallback (`llm_fallbacks_total`), until the budget window
(`LLM_BUDGET_WINDOW_S`) moves past the spend.

**Check.**
1. `llm_tokens_total` and `llm_cost_total` by provider: the spend that used the budget.
2. The usage ledger (the `llm_usage` table) by `job_id` and `prompt_id`: one job or prompt using
   most of it points to an oversized prompt or a retry storm.

**Fix.** Nothing is required. Raise the budget if the spend is expected, or lower
`LLM_MAX_OUTPUT_TOKENS` and `LLM_MAX_INPUT_TOKENS` to spend less per call.
