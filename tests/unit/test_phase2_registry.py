"""Phase 2: the model registry port, its adapters, the model URI scheme and the handler port.

Every adapter runs the conformance suite (oran_adapt.conformance.registry): filesystem (with
the filesystem and the fsspec artifact stores), MLflow on local sqlite, mirror, and SageMaker
and Vertex against the emulators in registry_emulators.py ("unverified against AWS/GCP"; the
live runs are the heavy tests at the end). One registry instance per backend is shared by the
checks, each of which uses its own model names, so the MLflow store is migrated once.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest
import registry_emulators as emu

from oran_adapt import plugins
from oran_adapt.adapters.registry.artifact_stores import FsspecArtifactStore, check_key
from oran_adapt.adapters.registry.filesystem import FilesystemRegistry
from oran_adapt.adapters.registry.mirror import SOURCE_TAG, MirrorRegistry
from oran_adapt.adapters.registry.sagemaker import NAME_TAG, SagemakerRegistry, group_name
from oran_adapt.adapters.registry.vertex import VertexRegistry, model_id
from oran_adapt.bootstrap import build_model_handler, build_registry
from oran_adapt.conformance import ConformanceFailure
from oran_adapt.conformance.registry import CHECKS, Context, run
from oran_adapt.core.config import Settings
from oran_adapt.core.errors import (
    ArtifactError,
    ConfigurationError,
    ConflictError,
    InvalidReferenceError,
    ModelNotFoundError,
    RegistryUnavailableError,
    UnsupportedAdaptationError,
)
from oran_adapt.core.integrity import ArtifactPolicy
from oran_adapt.core.model_uri import ModelUri
from oran_adapt.ports import ModelRegistryPort

BACKENDS = ("filesystem", "fsspec", "mlflow", "mirror", "sagemaker-emulator", "vertex-emulator")


def _settings(root: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": f"sqlite:///{(root / 'app.db').as_posix()}",
        "mlflow_tracking_uri": f"sqlite:///{(root / 'mlflow.db').as_posix()}",
        "artifact_workdir": str(root / "work"),
        "registry_fs_root": str(root / "registry"),
        "artifact_store_root": str(root / "store"),
        "log_json": False,
        "auth_enabled": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def sagemaker_registry(settings: Settings) -> SagemakerRegistry:
    return SagemakerRegistry(
        bucket="models",
        prefix="oran",
        inference_image="123.dkr.ecr.eu-west-1.amazonaws.com/serve:1",
        group_prefix="",
        content_types=["application/json"],
        artifact_policy=ArtifactPolicy.from_settings(settings),
        sm_client=emu.SagemakerEmulator(),
        s3_client=emu.S3Emulator("models"),
    )


def vertex_registry(settings: Settings) -> VertexRegistry:
    return VertexRegistry(
        project="proj",
        location="europe-west4",
        bucket="models",
        prefix="oran",
        serving_image="europe-docker.pkg.dev/proj/serve:1",
        api_endpoint=f"https://{emu.API_HOST}",
        storage_endpoint=f"https://{emu.STORAGE_HOST}",
        operation_timeout_s=5.0,
        operation_poll_s=0.001,
        tag_update_attempts=3,
        artifact_policy=ArtifactPolicy.from_settings(settings),
        http_factory=emu.EmulatorClientFactory(emu.VertexEmulator("proj", "europe-west4", "models")),
        token_provider=emu.emulator_token,
    )


def _build(backend: str, root: Path) -> ModelRegistryPort:
    if backend == "filesystem":
        return build_registry(_settings(root, registry_backend="filesystem"))
    if backend == "fsspec":
        url = f"memory://oran-conformance-{uuid.uuid4().hex}"
        return build_registry(
            _settings(
                root,
                registry_backend="filesystem",
                artifact_store_backend="fsspec",
                artifact_store_url=url,
            )
        )
    if backend == "mlflow":
        return build_registry(_settings(root, registry_backend="mlflow"))
    if backend == "mirror":
        primary = build_registry(_settings(root / "primary", registry_backend="filesystem"))
        replica = build_registry(_settings(root / "replica", registry_backend="filesystem"))
        return MirrorRegistry(primary, replica, on_replica_error="fail")
    if backend == "sagemaker-emulator":
        return sagemaker_registry(_settings(root))
    return vertex_registry(_settings(root))


@pytest.fixture(scope="module", params=BACKENDS)
def backend(request, tmp_path_factory) -> tuple[str, ModelRegistryPort]:
    root = tmp_path_factory.mktemp(request.param)
    return request.param, _build(request.param, root)


@pytest.mark.parametrize("check", list(CHECKS))
def test_conformance(backend, check, tmp_path) -> None:
    _, registry = backend
    CHECKS[check](registry, Context(tmp_path, prefix=check.replace("_", "-")))


def test_every_installed_registry_adapter_is_covered() -> None:
    covered = {b.removesuffix("-emulator") for b in BACKENDS} - {"fsspec"}
    assert set(plugins.adapters("registry")) == covered
    assert set(plugins.adapters("artifact_store")) == {"filesystem", "fsspec"}
    assert set(plugins.adapters("model_handler")) == {"mlflow-flavors", "native"}


def test_the_suite_catches_a_broken_adapter(tmp_path) -> None:
    class ForgetsTags(FilesystemRegistry):
        def set_version_tags(self, name: str, version: str, tags: dict[str, str]) -> None:
            return None

    registry = build_registry(_settings(tmp_path, registry_backend="filesystem"))
    assert isinstance(registry, FilesystemRegistry)
    broken = ForgetsTags(
        registry.root,
        registry.store,
        artifact_policy=registry.artifact_policy,
        lock_timeout_s=1.0,
    )
    with pytest.raises(ConformanceFailure, match="does not merge"):
        CHECKS["tag_merge"](broken, Context(tmp_path))


# ---- model URI -------------------------------------------------------------------------


@pytest.mark.smoke
def test_model_uri_parse_and_format() -> None:
    assert ModelUri.parse("model://kpi.forecaster/3") == ModelUri("kpi.forecaster", version="3")
    assert ModelUri.parse("model://m_1@champion") == ModelUri("m_1", alias="champion")
    assert str(ModelUri.for_version("m", "12")) == "model://m/12"
    for bad in (
        "models:/m/1",
        "model://m",
        "model://m/0",
        "model://m/01",
        "model://m@12",
        "model://-m/1",
        "model://m/1/2",
    ):
        with pytest.raises(InvalidReferenceError):
            ModelUri.parse(bad)


# ---- adapter specifics -----------------------------------------------------------------


def test_filesystem_lock_timeout_names_the_lock(tmp_path) -> None:
    registry = build_registry(
        _settings(tmp_path, registry_backend="filesystem", registry_fs_lock_timeout_s=0.05)
    )
    ctx = Context(tmp_path)
    registry.create_version("locked", ctx.artifact("one"))
    lock = tmp_path / "registry" / "models" / "locked" / ".lock"
    lock.write_text("held by a crashed writer", encoding="utf-8")
    with pytest.raises(RegistryUnavailableError, match="lock"):
        registry.create_version("locked", ctx.artifact("two"))
    lock.unlink()
    assert registry.create_version("locked", ctx.artifact("two")) == "2"


@pytest.mark.smoke
@pytest.mark.parametrize("key", ["", "/abs", "a/../b", "a\\b", "a//b", "."])
def test_artifact_store_rejects_unsafe_keys(key) -> None:
    with pytest.raises(ArtifactError):
        check_key(key)


def test_fsspec_store_refuses_to_overwrite(tmp_path) -> None:
    store = FsspecArtifactStore(f"memory://oran-store-{uuid.uuid4().hex}")
    src = tmp_path / "a"
    src.mkdir()
    (src / "f.txt").write_text("x", encoding="utf-8")
    store.put("k/model", str(src))
    assert store.exists("k/model")
    with pytest.raises(ArtifactError):
        store.put("k/model", str(src))


def test_mirror_config_is_validated(tmp_path) -> None:
    for primary, replica in (("filesystem", "filesystem"), ("mirror", "filesystem"), (None, "x")):
        with pytest.raises(ConfigurationError):
            build_registry(
                _settings(
                    tmp_path,
                    registry_backend="mirror",
                    registry_mirror_primary=primary,
                    registry_mirror_replica=replica,
                )
            )


def test_mirror_from_settings_and_replica_failure_then_sync(tmp_path) -> None:
    mirror = build_registry(
        _settings(
            tmp_path,
            registry_backend="mirror",
            registry_mirror_primary="filesystem",
            registry_mirror_replica="mlflow",
        )
    )
    assert isinstance(mirror, MirrorRegistry)
    ctx = Context(tmp_path)
    settings = _settings(tmp_path)
    replica = sagemaker_registry(settings)
    mirror = MirrorRegistry(mirror.primary, replica, on_replica_error="fail")
    sm = replica.sm
    assert isinstance(sm, emu.SagemakerEmulator)

    mirror.create_version("kpi", ctx.artifact("v1"), metrics={"rmse": 1.0}, tags={"t": "1"})
    sm.fail_next = "create_model_package"
    with pytest.raises(RegistryUnavailableError, match="run sync"):
        mirror.create_version("kpi", ctx.artifact("v2"))
    assert [v.version for v in mirror.list_versions("kpi")] == ["1", "2"]  # primary has it
    assert mirror.replica_version("kpi", "2") is None

    lenient = MirrorRegistry(mirror.primary, replica, on_replica_error="log")
    sm.fail_next = "create_model_package"
    assert lenient.create_version("kpi", ctx.artifact("v3")) == "3"
    lenient.primary.set_alias("kpi", "live", "3")  # a write the replica never saw
    replica.set_alias("kpi", "stale", replica.list_versions("kpi")[0].version)

    report = mirror.sync("kpi", str(tmp_path))
    assert report["copied_versions"] == ["2", "3"]
    assert report["removed"] == ["stale"]
    mapped = mirror.replica_version("kpi", "3")
    assert mapped is not None
    assert replica.get_version_by_alias("kpi", "live") == mapped
    assert replica.get_version("kpi", mapped).tags[SOURCE_TAG] == "3"
    assert replica.get_version_metrics("kpi", str(mirror.replica_version("kpi", "1"))) == {
        "rmse": 1.0
    }


def test_sagemaker_names_limits_and_outages(tmp_path) -> None:
    registry = sagemaker_registry(_settings(tmp_path))
    sm = registry.sm
    ctx = Context(tmp_path)
    assert group_name("", "kpi-model") == "kpi-model"
    assert group_name("", "kpi_model") != group_name("", "kpi-model")
    assert len(group_name("", "x" * 128)) <= 63

    registry.create_version("kpi_model", ctx.artifact("a"))
    arn = sm.groups[group_name("", "kpi_model")]["arn"]
    sm.add_tags(ResourceArn=arn, Tags=[{"Key": NAME_TAG, "Value": "someone-else"}])
    with pytest.raises(ModelNotFoundError):
        registry.get_registered_model("kpi_model")
    with pytest.raises(ConflictError):
        registry.create_version("kpi_model", ctx.artifact("b"))

    registry.create_version("limits", ctx.artifact("c"))
    with pytest.raises(UnsupportedAdaptationError):
        registry.set_version_tags("limits", "1", {"note": "x" * 257})
    with pytest.raises(UnsupportedAdaptationError):
        registry.set_version_tags("limits", "1", {f"k{i}": "v" for i in range(50)})
    with pytest.raises(UnsupportedAdaptationError):
        registry.set_version_tags("limits", "1", {"oran:metrics-uri": "s3://elsewhere"})

    sm.fail_next = "describe_model_package_group"  # an outage is not "model not found"
    with pytest.raises(RegistryUnavailableError):
        registry.get_version("limits", "1")


def test_vertex_aliases_ids_and_conflicts(tmp_path) -> None:
    registry = vertex_registry(_settings(tmp_path))
    ctx = Context(tmp_path)
    assert model_id("kpi-model") == "kpi-model"
    assert model_id("KPI.model") != "kpi-model" and model_id("KPI.model").startswith("kpi-model-")
    assert model_id("9lives").startswith("m9lives-")

    registry.create_version("kpi-model", ctx.artifact("a"))
    assert registry.get_registered_model("kpi-model").aliases == {}  # "default" is hidden
    for alias in ("default", "Live", "x", "has_underscore"):
        with pytest.raises(UnsupportedAdaptationError):
            registry.set_alias("kpi-model", alias, "1")

    factory = registry.http_factory
    assert isinstance(factory, emu.EmulatorClientFactory)
    factory.emulator.models[model_id("clash")] = {
        "displayName": "not-clash",
        "createTime": "2026-01-01T00:00:00Z",
        "versions": [],
    }
    with pytest.raises(ModelNotFoundError):
        registry.get_registered_model("clash")
    with pytest.raises(ConflictError):
        registry.create_version("clash", ctx.artifact("b"))


def test_vertex_rejects_bad_credentials(tmp_path) -> None:
    registry = vertex_registry(_settings(tmp_path))
    registry.token_provider = lambda: "wrong"
    with pytest.raises(RegistryUnavailableError, match="401"):
        registry.ping()


# ---- model handlers --------------------------------------------------------------------


@pytest.fixture
def sklearn_model() -> object:
    import numpy as np
    from sklearn.linear_model import LinearRegression

    x = np.arange(20, dtype=float).reshape(10, 2)
    return LinearRegression().fit(x, x.sum(axis=1))


@pytest.mark.parametrize("fmt", ["native", "mlflow-flavors"])
def test_handler_roundtrip_and_detection(fmt, sklearn_model, tmp_path) -> None:
    """Whatever MODEL_FORMAT saved a version, the handler set loads it."""
    import numpy as np

    saver = build_model_handler(_settings(tmp_path, model_format=fmt))
    other = "mlflow-flavors" if fmt == "native" else "native"
    loader = build_model_handler(_settings(tmp_path, model_format=other))
    path = saver.save(sklearn_model, "sklearn", str(tmp_path / "model"))
    assert loader.detect(path)
    loaded = loader.load(path, "sklearn")
    x = np.ones((1, 2))
    assert loaded.predict(x) == pytest.approx(sklearn_model.predict(x))  # type: ignore[attr-defined]


def test_handler_refuses_an_unknown_artifact(tmp_path) -> None:
    handler = build_model_handler(_settings(tmp_path))
    (tmp_path / "junk").mkdir()
    (tmp_path / "junk" / "model.bin").write_bytes(b"\0")
    assert not handler.detect(str(tmp_path / "junk"))
    with pytest.raises(ArtifactError):
        handler.load(str(tmp_path / "junk"), "sklearn")


# ---- extension path: the cookiecutter template -----------------------------------------

TEMPLATE = Path(__file__).resolve().parents[2] / "templates" / "registry-adapter"


def _render_template(out: Path, **names: str) -> Path:
    """Render templates/registry-adapter the way cookiecutter does; returns the project dir."""
    import json

    jinja2 = pytest.importorskip("jinja2", reason="rendering the template needs jinja2 [owner=TNDEVIL4U expires=2027-03-31]")
    env = jinja2.Environment(keep_trailing_newline=True, undefined=jinja2.StrictUndefined)
    cc: dict[str, str] = {}
    for key, value in json.loads((TEMPLATE / "cookiecutter.json").read_text()).items():
        cc[key] = names.get(key) or env.from_string(value).render(cookiecutter=cc)
    for path in TEMPLATE.rglob("*"):
        if path.is_file() and path.name != "cookiecutter.json":
            rel = env.from_string(path.relative_to(TEMPLATE).as_posix()).render(cookiecutter=cc)
            dst = out / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            text = path.read_text(encoding="utf-8")
            dst.write_text(env.from_string(text).render(cookiecutter=cc), encoding="utf-8")
    return out / cc["project_slug"]


def test_the_template_renders_an_adapter_that_passes_conformance(tmp_path, monkeypatch) -> None:
    import importlib

    project = _render_template(
        tmp_path / "out", project_name="Tpl Check Registry", adapter_name="tplcheck",
        class_name="TplCheckRegistry", env_prefix="TPLCHECK_",
    )
    assert 'tplcheck = "tpl_check_registry:SPEC"' in (project / "pyproject.toml").read_text()
    monkeypatch.syspath_prepend(str(project / "src"))
    package = importlib.import_module("tpl_check_registry")
    try:
        spec = package.SPEC
        assert (spec.capability.port, spec.capability.adapter) == ("registry", "tplcheck")
        monkeypatch.delenv("TPLCHECK_ROOT", raising=False)
        with pytest.raises(ConfigurationError, match="TPLCHECK_ROOT"):
            spec.factory(_settings(tmp_path))
        monkeypatch.setenv("TPLCHECK_ROOT", str(tmp_path / "tpl-registry"))
        assert run(spec.factory(_settings(tmp_path)), Context(tmp_path / "work")) == list(CHECKS)
    finally:
        for module in [m for m in list(sys.modules) if m.startswith("tpl_check_registry")]:
            del sys.modules[module]


# ---- live cloud runs (heavy: never part of the phase gate) ----------------------------


def _live(backend: str) -> Settings:
    """Settings from the environment; skips when the backend's required keys are unset."""
    settings = Settings()
    keys = plugins.adapters("registry")[backend].capability.required_keys
    missing = [k for k in keys if getattr(settings, k) in (None, "")]
    if missing:
        pytest.skip(f"live {backend} run needs {', '.join(k.upper() for k in missing)}"
                    " [owner=TNDEVIL4U expires=2027-03-31]")
    return settings


@pytest.mark.heavy
@pytest.mark.parametrize("backend_name", ["sagemaker", "vertex"])
def test_live_cloud_conformance(backend_name, tmp_path) -> None:
    settings = _live(backend_name)
    registry = plugins.adapters("registry")[backend_name].factory(settings)
    prefix = f"oran-conformance-{uuid.uuid4().hex[:8]}"
    assert run(registry, Context(tmp_path, prefix=prefix)) == list(CHECKS)


def test_live_runs_skip_without_config(monkeypatch) -> None:
    for key in list(os.environ):
        if key.startswith(("SAGEMAKER_", "VERTEX_")):
            monkeypatch.delenv(key)
    with pytest.raises(pytest.skip.Exception, match="SAGEMAKER_REGION"):
        _live("sagemaker")
