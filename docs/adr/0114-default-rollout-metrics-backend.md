# ADR-0114: Default ROLLOUT_METRICS_BACKEND is `api`

- **Status:** Accepted
- **Selector:** `ROLLOUT_METRICS_BACKEND`
- **Default:** `api`
- **Alternatives shipped:** `prometheus`
- **Open question:** [OPEN-QUESTIONS.md](../OPEN-QUESTIONS.md#validation-gate-and-progressive-delivery)

## Context

Canary, A/B and shadow rollouts compare online metrics of the stable and candidate arms. Whether a metrics system exists is unknown.

## Decision

`api`: the serving layer posts observations to `/api/v1/rollouts/{id}/observations`. `prometheus` reads PromQL range queries per arm.

## Consequences

Works with no metrics system; the serving layer (or a small relay) must post observations.

## Revisit when

A Prometheus (or compatible) server scrapes the serving system.
