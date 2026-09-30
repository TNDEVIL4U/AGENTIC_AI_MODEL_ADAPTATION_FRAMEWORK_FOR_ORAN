# ADR-0102: Default DEPLOYMENT_BACKEND is `registry-alias`

- **Status:** Accepted
- **Selector:** `DEPLOYMENT_BACKEND`
- **Default:** `registry-alias`
- **Alternatives shipped:** `bentoml`, `gitops`, `k8s`, `kserve`, `sagemaker`, `seldon`, `triton`, `vertex`, `webhook`
- **Open question:** [OPEN-QUESTIONS.md](../OPEN-QUESTIONS.md#deployment-adapters)

## Context

Promotion must reach whatever serves models in the RIC. The serving system is unknown: Triton, BentoML, KServe, Seldon, plain Kubernetes Deployments, GitOps and cloud endpoints are all plausible.

## Decision

`registry-alias` is the default: "deployed" means the `LIVE_ALIAS` alias (and, for canaries, the canary alias plus a traffic tag) in the registry; the inference service loads `model://<name>@<alias>`. Nine other adapters ship, including the `webhook` and `gitops` escape hatches for any system without an adapter (ADR-0004).

## Consequences

It needs no serving credentials and works with every registry. It relies on the inference service re-reading the alias; a serving system that caches must be told to reload (use a real adapter or the webhook).

## Revisit when

The serving platform is named; then select its adapter and keep `registry-alias` only for development.
