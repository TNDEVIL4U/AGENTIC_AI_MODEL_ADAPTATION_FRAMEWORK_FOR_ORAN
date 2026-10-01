# ADR-0112: Default DATASET_BACKENDS is `none`

- **Status:** Accepted
- **Selector:** `DATASET_BACKENDS`
- **Default:** `none`
- **Alternatives shipped:** `file`, `fsspec`, `gcs`, `http`, `s3`
- **Open question:** [OPEN-QUESTIONS.md](../OPEN-QUESTIONS.md#dataset-adapters)

## Context

Training and drifted data can be sent inline or read from a store. Which stores exist, and which a job may read, is a per-site decision.

## Decision

`none`: data is sent inline or registered through the API. `file`, `fsspec`, `s3`, `gcs` and `http` ship; each needs an explicit allowlist of roots or hosts.

## Consequences

Nothing reads from a store the site did not allow.

## Revisit when

The site names where KPI data lives.
