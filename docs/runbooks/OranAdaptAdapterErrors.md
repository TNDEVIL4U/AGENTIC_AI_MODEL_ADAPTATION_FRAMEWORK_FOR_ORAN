# OranAdaptAdapterErrors

**Fires when** more than 20 % of the calls to one adapter of one port failed over 10 minutes
(`adapter_errors_total` / `adapter_call_duration_seconds_count`, by `port` and `adapter`), for
10 minutes.

**Impact.** Depends on the port: `registry` and `dataset` failures fail jobs; `deployment`
failures fail promotions and roll rollouts back; `notify` failures are retried and may be
dead-lettered; `llm` failures fall back to the rule-based decision; `rollout_metrics` failures
hold rollouts at their step.

**Check.**
1. `adapter_errors_total` by `code` and `operation`: `*_UNAVAILABLE` codes mean the system is
   unreachable (network, credentials, outage); `UNEXPECTED` is an error the adapter did not
   translate, and its traceback is in the logs.
2. The logs of the failing calls carry `trace_id`; the trace shows the failing span and its
   exception.
3. The dependency's own health: `GET /api/v1/ready` checks the configured backends.

**Fix.** Restore the dependency or its credentials (secrets are read at startup: restart the
pods after rotating them). Jobs that failed on it can be resubmitted.
