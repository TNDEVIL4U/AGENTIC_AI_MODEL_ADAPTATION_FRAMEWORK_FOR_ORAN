# ADR-0104: Default ARTIFACT_STORE_BACKEND is `filesystem`

- **Status:** Accepted
- **Selector:** `ARTIFACT_STORE_BACKEND`
- **Default:** `filesystem`
- **Alternatives shipped:** `fsspec`
- **Open question:** [OPEN-QUESTIONS.md](../OPEN-QUESTIONS.md#registry-adapters)

## Context

The `filesystem` registry keeps artifacts outside itself. Whether a shared volume or an object store is available is unknown.

## Decision

`filesystem` (`ARTIFACT_STORE_ROOT`) is the default; `fsspec` (`ARTIFACT_STORE_URL`, any fsspec URL: `s3://`, `gs://`, `az://`) is the alternative. Only the `filesystem` registry uses this port.

## Consequences

A local path works everywhere, including air-gapped sites; under `ENVIRONMENT=production` the root must be set explicitly so artifacts do not land in a container's scratch space.

## Revisit when

An object store is mandated for model artifacts.
