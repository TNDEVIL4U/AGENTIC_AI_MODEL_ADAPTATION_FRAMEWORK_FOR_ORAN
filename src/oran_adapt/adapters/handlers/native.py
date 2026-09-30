"""Model handler adapter ``native``: each framework's own serialization, no registry format.

An artifact directory holds the model file and ``oran-model.json``, a manifest naming the
format, the framework and the file. sklearn, LightGBM and CatBoost models are written with
skops (only the reviewed types in MLFLOW_SKOPS_TRUSTED_TYPES are accepted, when saving and again
when loading), xgboost models with xgboost's own ``save_model`` (UBJSON), ONNX graphs as the
protobuf they are, Keras models in the ``.keras`` format (loaded in Keras' safe mode), torch
modules with ``torch.save`` and statsmodels results with their own ``save``. torch and
statsmodels artifacts are pickles; they are only loaded after the caller has checked their
registered SHA-256 (oran_adapt.registry.promotion.verify_version_artifact), as with the MLflow
pytorch flavor. Each library is imported only when a model of its framework is saved or loaded.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from oran_adapt.core.errors import ArtifactError, UnsupportedAdaptationError
from oran_adapt.ports import AdapterSpec, Capability

if TYPE_CHECKING:
    from oran_adapt.core.config import Settings

FORMAT = "oran-native/1"
MANIFEST = "oran-model.json"
# Framework name -> canonical name. "pytorch" is accepted as an alias of "torch", "tensorflow"
# of "keras".
_CANONICAL = {
    "sklearn": "sklearn",
    "xgboost": "xgboost",
    "torch": "torch",
    "pytorch": "torch",
    "lightgbm": "lightgbm",
    "catboost": "catboost",
    "onnx": "onnx",
    "statsmodels": "statsmodels",
    "keras": "keras",
    "tensorflow": "keras",
}
_FILES = {
    "sklearn": "model.skops",
    "xgboost": "model.ubj",
    "torch": "model.pt",
    "lightgbm": "model.skops",
    "catboost": "model.skops",
    "onnx": "model.onnx",
    "statsmodels": "model.pickle",
    "keras": "model.keras",
}
# Frameworks whose scikit-learn-API models skops serializes.
_SKOPS = frozenset({"sklearn", "lightgbm", "catboost"})
# xgboost estimator classes a manifest may name; anything else is refused on load.
_XGB_CLASSES = frozenset(
    {"Booster", "XGBRegressor", "XGBClassifier", "XGBRanker", "XGBRFRegressor", "XGBRFClassifier"}
)


def resolve_skops_trusted_types(model: object, allowed: Sequence[str]) -> list[str]:
    """The skops types ``model`` needs trusted to be serialized. Raises ArtifactError - a
    deterministic failure, never retried - when it needs any type outside ``allowed``, rather
    than letting a later step fail with a generic error that looks like an outage."""
    import skops.io as sio

    try:
        needed = sio.get_untrusted_types(data=sio.dumps(model))
    except Exception as exc:
        raise ArtifactError(
            f"could not serialize {type(model).__name__} with skops", cause=str(exc)
        ) from exc
    _refuse_untrusted(type(model).__name__, needed, allowed)
    return sorted(needed)


def _refuse_untrusted(what: str, needed: Sequence[str], allowed: Sequence[str]) -> None:
    refused = sorted(set(needed) - set(allowed))
    if refused:
        raise ArtifactError(
            f"{what} needs skops types that are not on the trusted list",
            untrusted_types=refused,
            hint="review them, then add to MLFLOW_SKOPS_TRUSTED_TYPES",
        )


def _canonical(framework: str) -> str:
    fw = _CANONICAL.get(framework.lower())
    if fw is None:
        raise UnsupportedAdaptationError(f"the native handler has no format for {framework!r}")
    return fw


class NativeHandler:
    def __init__(self, skops_trusted_types: Sequence[str]) -> None:
        self.skops_trusted_types = tuple(skops_trusted_types)

    @property
    def frameworks(self) -> frozenset[str]:
        return frozenset(_CANONICAL)

    @property
    def format(self) -> str:
        return FORMAT

    def detect(self, local_path: str) -> bool:
        path = os.path.join(local_path, MANIFEST)
        if not os.path.isfile(path):
            return False
        return self._manifest(local_path).get("format") == FORMAT

    def save(self, model: object, framework: str, dst_dir: str) -> str:
        fw = _canonical(framework)
        os.makedirs(dst_dir, exist_ok=False)
        target = os.path.join(dst_dir, _FILES[fw])
        manifest: dict[str, Any] = {"format": FORMAT, "framework": fw, "file": _FILES[fw]}
        try:
            if fw in _SKOPS:
                import skops.io as sio

                resolve_skops_trusted_types(model, self.skops_trusted_types)
                sio.dump(model, target)
            elif fw == "xgboost":
                cls = type(model).__name__
                if cls not in _XGB_CLASSES:
                    raise UnsupportedAdaptationError(f"no native xgboost format for {cls}")
                model.save_model(target)  # type: ignore[attr-defined]
                manifest["class"] = cls
            elif fw == "onnx":
                import onnx

                onnx.save_model(model, target)  # type: ignore[arg-type]
            elif fw in ("statsmodels", "keras"):
                # Both write their own format to the path: a results pickle, a .keras archive.
                model.save(target)  # type: ignore[attr-defined]
            else:
                import torch

                torch.save(model, target)
        except (ArtifactError, UnsupportedAdaptationError):
            raise
        except Exception as exc:
            raise ArtifactError(
                f"could not serialize the {fw} model", path=dst_dir, cause=str(exc)
            ) from exc
        with open(os.path.join(dst_dir, MANIFEST), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, sort_keys=True)
        return dst_dir

    def load(self, local_path: str, framework: str) -> object:
        fw = _canonical(framework)
        manifest = self._manifest(local_path)
        if manifest.get("format") != FORMAT or manifest.get("framework") != fw:
            raise ArtifactError(
                f"artifact is not a native {fw} model",
                path=local_path,
                manifest_format=manifest.get("format"),
                manifest_framework=manifest.get("framework"),
            )
        target = os.path.join(local_path, str(manifest.get("file")))
        if os.path.dirname(os.path.normpath(target)) != os.path.normpath(local_path):
            raise ArtifactError("manifest file escapes the artifact directory", path=local_path)
        try:
            if fw in _SKOPS:
                import skops.io as sio

                needed = sio.get_untrusted_types(file=target)
                _refuse_untrusted("the stored model", needed, self.skops_trusted_types)
                return sio.load(target, trusted=needed)
            if fw == "xgboost":
                return _load_xgboost(target, str(manifest.get("class")))
            if fw == "onnx":
                import onnx

                return onnx.load_model(target)
            if fw == "statsmodels":
                from statsmodels.iolib.smpickle import load_pickle

                return load_pickle(target)
            if fw == "keras":
                import keras

                return keras.saving.load_model(target, safe_mode=True)
            import torch

            return torch.load(target, map_location="cpu", weights_only=False)
        except (ArtifactError, UnsupportedAdaptationError):
            raise
        except Exception as exc:
            raise ArtifactError(
                f"failed to load {framework} model artifact", path=local_path, cause=str(exc)
            ) from exc

    @staticmethod
    def _manifest(local_path: str) -> dict[str, Any]:
        path = os.path.join(local_path, MANIFEST)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            raise ArtifactError("unreadable model manifest", path=path, cause=str(exc)) from exc
        if not isinstance(data, dict):
            raise ArtifactError("model manifest is not a JSON object", path=path)
        return data


def _load_xgboost(target: str, cls_name: str) -> object:
    import xgboost

    if cls_name not in _XGB_CLASSES:
        raise ArtifactError(f"manifest names an unknown xgboost class {cls_name!r}", path=target)
    model = getattr(xgboost, cls_name)()
    model.load_model(target)
    return model


def _build(settings: Settings) -> NativeHandler:
    return NativeHandler(settings.mlflow_skops_trusted_types)


SPEC = AdapterSpec(
    capability=Capability(
        port="model_handler",
        adapter="native",
        description="each framework's own format: skops, UBJSON, ONNX, .keras, torch, statsmodels",
        features=frozenset({"load", "save", *_CANONICAL}),
        config_keys=("mlflow_skops_trusted_types",),
    ),
    factory=_build,
)
