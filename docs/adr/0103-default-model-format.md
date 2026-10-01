# ADR-0103: Default MODEL_FORMAT is `mlflow-flavors`

- **Status:** Accepted
- **Selector:** `MODEL_FORMAT`
- **Default:** `mlflow-flavors`
- **Alternatives shipped:** `native`
- **Open question:** [OPEN-QUESTIONS.md](../OPEN-QUESTIONS.md#model-type-plugins)

## Context

Saved models must be loadable by the adaptation engines and by serving. MLflow's model format is what the pilot used; some sites serve the frameworks' own files without MLflow.

## Decision

`mlflow-flavors` is the default for saving; loading picks whichever installed handler recognises the artifact. `native` (skops, UBJSON, torch files with a manifest) needs no MLflow.

## Consequences

Artifacts stay readable by MLflow-based serving. Sites without MLflow switch to `native` and must serve those formats (Triton reads ONNX; see `config/examples/triton.toml`).

## Revisit when

Serving is known not to read MLflow's format.
