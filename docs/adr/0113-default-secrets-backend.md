# ADR-0113: Default SECRETS_BACKEND is `env`

- **Status:** Accepted
- **Selector:** `SECRETS_BACKEND`
- **Default:** `env`
- **Alternatives shipped:** `file`, `vault`
- **Open question:** [OPEN-QUESTIONS.md](../OPEN-QUESTIONS.md#security)

## Context

Credentials (API keys, tokens) must not live in config files. Whether a secrets manager is mandated is unknown.

## Decision

`env`: secrets come from the environment. `file` (mounted secret files, as Kubernetes and Docker provide) and `vault` (HashiCorp Vault KV) ship. Config files that contain a secret are refused.

## Consequences

Works everywhere; Kubernetes deployments usually switch to `file`.

## Revisit when

A secrets manager is mandated.
