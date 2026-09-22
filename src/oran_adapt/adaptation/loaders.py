"""Member 3 - loaders: turn a downloaded MLflow model directory into the real, native framework
object (an sklearn estimator, an xgboost estimator, a torch.nn.Module) so the inspector can look
at it directly instead of through MLflow's generic pyfunc wrapper.
"""

from __future__ import annotations

from oran_adapt.core.errors import ArtifactError, UnsupportedAdaptationError

_SKLEARN_LIKE = {"sklearn", "xgboost"}
_TORCH_LIKE = {"torch", "pytorch"}


def load_native_model(local_path: str, framework: str) -> object:
    """``local_path`` is a local directory containing an MLmodel file, as produced by
    ``MlflowRegistry.download_artifacts``. Raises ArtifactError if the directory doesn't hold a
    loadable model of the given framework, UnsupportedAdaptationError if the framework has no
    loader at all."""
    fw = framework.lower()
    try:
        if fw == "sklearn":
            import mlflow.sklearn

            return mlflow.sklearn.load_model(local_path)
        if fw == "xgboost":
            import mlflow.xgboost

            return mlflow.xgboost.load_model(local_path)
        if fw in _TORCH_LIKE:
            import mlflow.pytorch

            return mlflow.pytorch.load_model(local_path)
    except UnsupportedAdaptationError:
        raise
    except Exception as exc:
        raise ArtifactError(
            f"failed to load {framework} model artifact", path=local_path, cause=str(exc)
        ) from exc
    raise UnsupportedAdaptationError(f"no model loader for framework {framework!r}")
