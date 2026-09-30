# ADR-0101: Default REGISTRY_BACKEND is `mlflow`

- **Status:** Accepted
- **Selector:** `REGISTRY_BACKEND`
- **Default:** `mlflow`
- **Alternatives shipped:** `filesystem`, `mirror`, `sagemaker`, `vertex`
- **Open question:** [OPEN-QUESTIONS.md](../OPEN-QUESTIONS.md#registry-adapters)

## Context

Models must be versioned, tagged (checksums, lineage) and addressed by alias somewhere. The pilot used MLflow, and nothing in the brief named another registry; cloud registries (SageMaker, Vertex) and registry-less sites (air-gapped) exist in the target estate.

## Decision

`mlflow` stays the default: an MLflow tracking server with `--serve-artifacts`. `filesystem` (JSON metadata beside an `artifact_store`), `mirror` (primary plus replica), `sagemaker` and `vertex` ship behind the same `RegistryPort` and pass the same conformance suite.

## Consequences

Existing installations keep working with no configuration change. `MLFLOW_TRACKING_URI` is required, and under `ENVIRONMENT=production` it must be set explicitly (it is a production key). Sites without MLflow select `filesystem` (see `config/examples/airgapped-filesystem.toml`) or a cloud registry.

## Revisit when

The operator names the registry of record, or MLflow is retired from the estate.
