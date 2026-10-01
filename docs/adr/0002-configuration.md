# ADR-0002: Configuration: environment first, TOML files, lint before deploy

- **Status:** Accepted

## Context

Settings grew to hundreds of keys across adapters; misconfiguration used to surface at the first job.

## Decision

One `Settings` schema. Sources: the environment, then `.env`, then the TOML file named by `ORAN_CONFIG_FILE` (tables flatten: `[sagemaker] region` is `SAGEMAKER_REGION`), then the secrets backend. Files may not hold secrets. Selecting an adapter makes its required keys mandatory; `ENVIRONMENT=production` also requires the production keys and refuses development-only adapters. `oran-adapt config lint FILE...` applies the same checks to a file alone.

## Consequences

A bad configuration fails at startup or in CI, naming the key. Every file under `config/examples/` is linted by the Phase 1 and Phase 14 gates.
