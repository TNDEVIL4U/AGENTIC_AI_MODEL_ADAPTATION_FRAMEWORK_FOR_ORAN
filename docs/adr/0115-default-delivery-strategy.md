# ADR-0115: Default DELIVERY_STRATEGY is `shadow`

- **Status:** Accepted
- **Selector:** `DELIVERY_STRATEGY`
- **Default:** `shadow`
- **Alternatives shipped:** `canary`, `blue_green`, `ab`, `manual`
- **Open question:** [OPEN-QUESTIONS.md](../OPEN-QUESTIONS.md#validation-gate-and-progressive-delivery)

## Context

A candidate that passes the offline gate can reach traffic at once, gradually, or only after approval. The serving system's ability to split traffic is unknown.

## Decision

`shadow`, then manual approval: the candidate scores mirrored traffic and serves none. `canary`, `blue_green`, `ab` and `manual` ship; the policy lives in `DELIVERY_POLICY_FILE`.

## Consequences

Works with every deployment adapter (no traffic split needed) and changes nobody's traffic without a person agreeing. The walkthrough and the example configs use `canary` where the adapter splits traffic.

## Revisit when

The site trusts the gate and the health rules enough to let rollouts proceed on their own.
