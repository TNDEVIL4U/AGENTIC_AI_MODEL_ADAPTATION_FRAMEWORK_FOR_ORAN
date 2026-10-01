# Model type adapter template

A starting point for a plugin that teaches the framework a new kind of model — see
[docs/adapters/model_type.md](../../docs/adapters/model_type.md) for the port and its rules.

1. Copy `adapter.py` into your package and rename `FRAMEWORK`, `LinearModel` and `LinearType`.
2. Implement the six members of `ModelTypePort`: `frameworks`, `engines`, `accepts`,
   `inspect`, `adapt` and `predict`.
3. Register it:

   ```toml
   [project.entry-points."oran_adapt.model_type"]
   my-model = "my_package.adapter:SPEC"
   ```

   The entry-point name must equal `SPEC.capability.adapter`.
4. Run the conformance suite against a fitted model of your kind:

   ```python
   from oran_adapt.conformance import model_types as conformance

   ctx = conformance.Context(model=fitted, framework="my-framework", X=X, y=y,
                             target_column="kpi", workdir=str(tmp_path))
   conformance.run(SPEC.factory(settings), ctx)
   ```

5. Install your package. No change to oran_adapt is needed: the pipeline, validation, the
   decision layer and the drift summary pick the new framework up from the registry. Set
   `MODEL_TYPES` to choose or order plugins when more than one serves the same framework.
