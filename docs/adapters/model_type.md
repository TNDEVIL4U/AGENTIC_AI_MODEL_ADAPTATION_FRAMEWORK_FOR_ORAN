# Model type adapters

A **model type plugin** implements `oran_adapt.ports.ModelTypePort`. It is what the framework
knows about one kind of model: how to recognise a loaded object, what it is (inspection), how
to adapt it (its engines) and how it predicts. The pipeline, validation, the version reuse
check, the decision layer and the drift summary all go through one registry
(`adaptation/model_types.py`, `build_model_types`); none of them holds a table of framework
names. Plugins are found through the `oran_adapt.model_type` entry-point group.

| Plugin | Module | Frameworks | Engines (fine-tuning / full retraining) | Temporal | Verified |
|---|---|---|---|---|---|
| `sklearn` | `adapters/model_types/tabular.py` | `sklearn` | `partial_fit` / refit from hyperparameters | no | local |
| `xgboost` | `tabular.py` | `xgboost` | - / refit | no | local |
| `lightgbm` | `tabular.py` | `lightgbm` | continued boosting (`init_model=booster_`) / refit | no | test double; unverified against LightGBM |
| `catboost` | `tabular.py` | `catboost` | continued boosting (`init_model=model`) / refit | no | test double; unverified against CatBoost |
| `torch` | `tabular.py` | `torch`, `pytorch` | warm start / re-initialised training | no | local |
| `torch-sequence` | `adapters/model_types/sequence.py` | `torch`, `pytorch` | warm start / re-initialised training, over sliding windows | yes | local (LSTM, TCN) |
| `keras` | `sequence.py` | `keras`, `tensorflow` | clone + weights / clone, over sliding windows for rank-3 inputs | rank-3 input | test double; unverified against Keras |
| `onnx` | `adapters/model_types/onnx.py` | `onnx` | none: inference only | no | local with the `onnx` reference evaluator; onnxruntime unverified |
| `statsmodels` | `adapters/model_types/forecasters.py` | `statsmodels` | filter update (`apply(refit=False)`) / re-estimate (`apply(refit=True)`) | yes | local (SARIMAX) |

## How a plugin is chosen

For a model registered under framework `F`, the registry tries the plugins named in
`MODEL_TYPES` in that order (every installed plugin, sorted by name, when it is empty), keeps
those whose `frameworks` include `F`, and uses the first whose `accepts(model, F)` returns
true. `torch` and `torch-sequence` both serve `torch`: the tabular plugin refuses any module
with a recurrent, convolutional or attention layer or a `sequence_window` attribute, so each
module lands on exactly one of them.

## Unsupported models: a typed result, never a crash

`ModelTypes.inspect` returns `UnsupportedModelType` (`kind = "unsupported_model_type"`, the
framework, the model class, a reason and the installed plugins) when no plugin serves the
framework, none accepts the object, or the chosen plugin fails while describing it.
`require` raises `UnsupportedModelTypeError` (code `UNSUPPORTED_MODEL_TYPE`, a subclass of
`UnsupportedAdaptationError`) with that result as its context. The worker records a job's error
as the error's `to_dict()` - code, message, context - so a job on an unsupported model fails
with that typed error and no stack trace.

## Temporal models

A plugin marks a model `temporal` when rows must stay in time order. For those:

* `TrainingSet` rows arrive oldest first and are never shuffled; the pipeline's hold-out is the
  newest rows;
* a plugin that needs its own validation split uses `adaptation.sequence.time_ordered_split`
  (the newest fraction validates);
* windowing lives inside the plugin: `sliding_windows` builds one window per row (the first
  `window - 1` rows are edge-padded, so validation scores every held-out row), and
  `training_windows` keeps only windows whose history is all real rows. The window is the
  model's own (`sequence_window` attribute, Keras input shape) or `SEQUENCE_WINDOW`.

The conformance check `time_order` adapts every temporal model with `numpy.random.permutation`,
`numpy.random.shuffle`, `torch.randperm`, `train_test_split` and `sklearn.utils.shuffle`
disabled: a plugin that reaches for any of them fails.

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `MODEL_TYPES` | `[]` (all installed, by name) | which plugins to use and in which order |
| `DECISION_SUPPORTED_FRAMEWORKS` | `[]` (every framework a plugin can adapt) | frameworks the decision layer may adapt |
| `SEQUENCE_WINDOW` | 8 | window for a sequence model that declares none |
| `SEQUENCE_VALIDATION_FRACTION` | 0.2 | newest share of training windows used to keep the best epoch |
| `SEQUENCE_FINE_TUNE_EPOCHS` / `SEQUENCE_FULL_RETRAIN_EPOCHS` | 20 / 200 | epochs per strategy |
| `SEQUENCE_LEARNING_RATE` | 0.01 | Adam learning rate for torch sequence models (Keras keeps its compiled optimizer) |
| `ONNX_RUNTIME` | `onnxruntime` | `onnxruntime` or `reference` (the `onnx` package's evaluator: slow, no extra dependency) |
| `TORCH_FINE_TUNE_EPOCHS`, `TORCH_FULL_RETRAIN_EPOCHS`, `TORCH_LEARNING_RATE` | see MANUAL | tabular torch models |

Storage: the native handler writes LightGBM and CatBoost models with skops, ONNX with
`onnx.save_model`, statsmodels results with their own pickle `save`, Keras as `.keras` (loaded
with `safe_mode=True`). The MLflow handler uses the matching MLflow flavor; a flavor whose
library is missing is a typed `ADAPTATION_UNSUPPORTED` error.

## Writing a new plugin

Start from `templates/model-type-adapter/`. A plugin is a class with

* `frameworks`: the lowercase framework names it serves;
* `engines`: `Strategy -> engine name` for each strategy it can run (empty: inference only);
* `accepts(model, framework)`: whether this loaded object is one it handles;
* `inspect(model, framework) -> ModelInspection`;
* `adapt(model, strategy, *, inspection, data) -> CandidateModel`: train a copy on `data`, write
  it with joblib under `data.artifact_dir`, name the declared engine; raise
  `UnsupportedAdaptationError` for a strategy it cannot run;
* `predict(model, X, *, inspection) -> numpy array`: one value per row.

Register an `AdapterSpec` under `oran_adapt.model_type` (the entry-point name equals
`capability.adapter`; model libraries are imported lazily inside the plugin) and run the
conformance suite (`oran_adapt.conformance.model_types`). No core change is needed.

| Check | Rule |
|---|---|
| `protocol` | implements the port; lowercase framework names; engines keyed by `Strategy` |
| `accepts` | accepts its own model, refuses a foreign object |
| `inspect` | a valid estimator type and class; a sequence window implies `temporal` |
| `predict` | one value per row, repeatable |
| `adapt` | each declared engine yields a `CandidateModel` with that engine, a written artifact, the row count and finite metrics; the live model is unchanged; no engine means `UnsupportedAdaptationError` |
| `time_order` | a temporal model adapts with every random reordering disabled |

```python
from oran_adapt.conformance.model_types import CHECKS, Context

@pytest.mark.parametrize("check", sorted(CHECKS))
def test_my_type(check, tmp_path):
    CHECKS[check](MyType(...), Context(model=fitted, framework="myfw", X=X, y=y,
                                       target_column="kpi", workdir=str(tmp_path)))
```
