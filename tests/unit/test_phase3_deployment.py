"""Hardening Phase 3: deployment and serving propagation.

Every deployment adapter runs the conformance suite (oran_adapt.conformance.deployment),
including the failed-rollout check: ``registry-alias`` on the filesystem registry; ``webhook``,
``bentoml`` and ``triton`` against the local HTTP serving stub (serving_stub.py); ``gitops``
against a real git checkout and the fake controller; ``kserve``, ``seldon``, ``k8s``,
``sagemaker`` and ``vertex`` against the emulators in deployment_emulators.py. Stub and
emulators are not the real systems, so those results are "unverified against real systems";
the live runs are the heavy tests at the end.

Then: the Deployer's failure paths, promotion keeping LIVE and the serving system in step when
a rollout fails, and the adapters' configuration checks.
"""

from __future__ import annotations

import itertools
import os
import subprocess
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import deployment_emulators as demu
import httpx
import pytest
import registry_emulators as emu
from fastapi.testclient import TestClient
from pydantic import SecretStr
from serving_stub import ServingStub

from oran_adapt import plugins
from oran_adapt.adapters.deployment._common import HttpApi, StaticToken, dns_name
from oran_adapt.adapters.deployment.alias import RegistryAliasDeployment
from oran_adapt.adapters.deployment.gitops import GitOpsDeployment
from oran_adapt.adapters.deployment.kubernetes import (
    K8sDeployment,
    KServeDeployment,
    SeldonDeployment,
)
from oran_adapt.adapters.deployment.sagemaker import SagemakerDeployment
from oran_adapt.adapters.deployment.triton import TritonDeployment
from oran_adapt.adapters.deployment.vertex import VertexDeployment
from oran_adapt.adapters.deployment.webhook import WebhookDeployment
from oran_adapt.adapters.registry.sagemaker import SagemakerRegistry
from oran_adapt.adapters.registry.vertex import VertexRegistry, model_id
from oran_adapt.api.app import create_app
from oran_adapt.bootstrap import build_deployer, build_deployment, build_registry
from oran_adapt.conformance import ConformanceFailure
from oran_adapt.conformance.deployment import CHECKS, FAILURE_CHECKS, Context, run
from oran_adapt.core import metrics
from oran_adapt.core.config import Settings
from oran_adapt.core.enums import PromotionKind
from oran_adapt.core.errors import (
    ConfigurationError,
    DeploymentError,
    DeploymentUnavailableError,
    PromotionError,
    RegistryUnavailableError,
)
from oran_adapt.core.integrity import ArtifactPolicy
from oran_adapt.db.base import create_db_engine, make_session_factory, session_scope
from oran_adapt.db.models import ModelMetadata, ModelPromotion
from oran_adapt.ports import DeploymentPort, DeploymentState, DeploymentTarget, ModelRegistryPort
from oran_adapt.registry.deployment import Deployer
from oran_adapt.registry.promotion import promote_version

BACKENDS = (
    "registry-alias", "webhook", "bentoml", "gitops", "triton",
    "kserve", "seldon", "k8s", "sagemaker", "vertex",
)
ALL_CHECKS = {**CHECKS, **FAILURE_CHECKS}


def _settings(root: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": f"sqlite:///{(root / 'app.db').as_posix()}",
        "mlflow_tracking_uri": f"sqlite:///{(root / 'mlflow.db').as_posix()}",
        "artifact_workdir": str(root / "work"),
        "registry_backend": "filesystem",
        "registry_fs_root": str(root / "registry"),
        "artifact_store_root": str(root / "store"),
        "log_json": False,
        "auth_enabled": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def _artifact(root: Path, model: str, label: str) -> str:
    """A small artifact directory, unique per (model, label)."""
    path = root / "artifacts" / model / label
    path.mkdir(parents=True, exist_ok=True)
    (path / "model.onnx").write_text(f"{model}:{label}:{uuid.uuid4().hex}", encoding="utf-8")
    return str(path)


def no_sleep(_: float) -> None:
    return None


def init_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


class FlakyAliases:
    """The alias calls of a registry, where ``fail_next(model)`` makes the next alias move of
    that model fail as an unreachable registry would (an alias move cannot fail later)."""

    def __init__(self, registry: ModelRegistryPort) -> None:
        self.registry = registry
        self.failing: set[str] = set()

    def fail_next(self, model: str) -> None:
        self.failing.add(model)

    def ping(self) -> None:
        self.registry.ping()

    def get_version_by_alias(self, name: str, alias: str) -> str:
        return self.registry.get_version_by_alias(name, alias)

    def set_alias(self, name: str, alias: str, version: str) -> None:
        if name in self.failing:
            self.failing.discard(name)
            raise RegistryUnavailableError("injected: the registry is not reachable", name=name)
        self.registry.set_alias(name, alias, version)

    def delete_alias(self, name: str, alias: str) -> None:
        self.registry.delete_alias(name, alias)


@dataclass
class Harness:
    port: DeploymentPort
    provision: Callable[[str], DeploymentTarget]
    inject_failure: Callable[[str], None]


def _counter_provision(root: Path) -> Callable[[str], DeploymentTarget]:
    """Versions 1, 2, ... per model, each with its own artifact directory."""
    counts: dict[str, itertools.count[int]] = {}

    def provision(model: str) -> DeploymentTarget:
        version = str(next(counts.setdefault(model, itertools.count(1))))
        return DeploymentTarget(
            model=model, version=version, source=f"model://{model}/{version}",
            artifact_dir=_artifact(root, model, version),
        )

    return provision


def _registry_provision(registry: ModelRegistryPort, root: Path) -> Callable[[str], DeploymentTarget]:
    """Register a new version of ``model`` in ``registry``; the target carries its source."""
    labels = itertools.count(1)

    def provision(model: str) -> DeploymentTarget:
        artifact = _artifact(root, model, f"r{next(labels)}")
        version = registry.create_version(model, artifact)
        return DeploymentTarget(
            model=model, version=version, source=registry.get_version(model, version).source,
            artifact_dir=artifact,
        )

    return provision


def _kube_api(kube: demu.KubeEmulator) -> HttpApi:
    return HttpApi(
        "https://k8s.emulator", service="the Kubernetes API",
        http_factory=demu.MockClientFactory(kube),
        token=StaticToken(SecretStr(demu.K8S_TOKEN)),
    )


def sagemaker_pair(settings: Settings) -> tuple[SagemakerRegistry, SagemakerDeployment, demu.SagemakerEndpointEmulator]:
    sm = demu.SagemakerEndpointEmulator()
    registry = SagemakerRegistry(
        bucket="models", prefix="oran",
        inference_image="123.dkr.ecr.eu-west-1.amazonaws.com/serve:1",
        group_prefix="", content_types=["application/json"],
        artifact_policy=ArtifactPolicy.from_settings(settings),
        sm_client=sm, s3_client=emu.S3Emulator("models"),
    )
    deployment = SagemakerDeployment(
        role_arn=f"arn:aws:iam::{emu.ACCOUNT}:role/serve", instance_type="ml.m5.large",
        instance_count=1, group_prefix="", endpoint_prefix="", sm_client=sm,
    )
    return registry, deployment, sm


def vertex_pair(settings: Settings) -> tuple[VertexRegistry, VertexDeployment, demu.VertexEndpointEmulator]:
    vx = demu.VertexEndpointEmulator("proj", "europe-west4", "models")
    factory = emu.EmulatorClientFactory(vx)
    registry = VertexRegistry(
        project="proj", location="europe-west4", bucket="models", prefix="oran",
        serving_image="europe-docker.pkg.dev/proj/serve:1",
        api_endpoint=f"https://{emu.API_HOST}", storage_endpoint=f"https://{emu.STORAGE_HOST}",
        operation_timeout_s=5.0, operation_poll_s=0.001, tag_update_attempts=3,
        artifact_policy=ArtifactPolicy.from_settings(settings),
        http_factory=factory, token_provider=emu.emulator_token,
    )
    deployment = VertexDeployment(
        HttpApi(f"https://{emu.API_HOST}/v1", service="Vertex AI", http_factory=factory,
                token=emu.emulator_token),
        project="proj", location="europe-west4", endpoint_prefix="",
        machine_type="n1-standard-2", min_replicas=1, max_replicas=1,
        operation_timeout_s=5.0, operation_poll_s=0.001, sleep=no_sleep,
    )
    return registry, deployment, vx


def gitops_deployment(repo: Path, controller: demu.GitOpsController) -> GitOpsDeployment:
    return GitOpsDeployment(
        repo_dir=str(repo), manifest_path="deployments/{name}.json", manifest_template=None,
        push=False, remote="origin", branch=None, author_name="oran-adapt",
        author_email="oran-adapt@localhost", git_timeout_s=60.0,
        status_api=HttpApi("http://gitops.status/status", service="the GitOps status endpoint",
                           http_factory=demu.MockClientFactory(controller),
                           token=StaticToken(None)),
    )


def build_harness(backend: str, root: Path, stub: ServingStub) -> Harness:
    counter = _counter_provision(root)
    if backend == "registry-alias":
        registry = build_registry(_settings(root))
        flaky = FlakyAliases(registry)
        return Harness(RegistryAliasDeployment(flaky, "serving",  # type: ignore[arg-type]
                                               canary_alias="canary",
                                               traffic_tag="oran.traffic_percent"),
                       _registry_provision(registry, root), flaky.fail_next)
    if backend in ("webhook", "bentoml"):
        suffix = "/oran" if backend == "bentoml" else ""
        api = HttpApi(stub.url + suffix, service=backend, http_factory=httpx.Client,
                      token=StaticToken(None))
        return Harness(WebhookDeployment(api), counter, stub.fail_next)
    if backend == "triton":
        api = HttpApi(stub.url, service="the Triton server", http_factory=httpx.Client,
                      token=StaticToken(None))
        assert stub.repository is not None
        port = TritonDeployment(api, repository=str(stub.repository),
                                base_config='backend: "onnxruntime"')
        return Harness(port, counter, stub.fail_next)
    if backend == "gitops":
        repo = init_git_repo(root / "gitops-repo")
        controller = demu.GitOpsController(str(repo))
        return Harness(gitops_deployment(repo, controller), counter, controller.fail_next)
    if backend in ("kserve", "seldon", "k8s"):
        kube = demu.KubeEmulator(deployments_exist=backend == "k8s")
        port: KServeDeployment | SeldonDeployment | K8sDeployment
        if backend == "kserve":
            port = KServeDeployment(_kube_api(kube), namespace="serving", name_prefix="",
                                    uri_template="s3://models/{name}/{version}",
                                    model_format="onnx")
        elif backend == "seldon":
            port = SeldonDeployment(_kube_api(kube), namespace="serving", name_prefix="",
                                    uri_template="s3://models/{name}/{version}",
                                    requirements=["onnx"])
        else:
            port = K8sDeployment(_kube_api(kube), namespace="serving", name_prefix="",
                                 env="MODEL_URI", uri_template="model://{model}/{version}",
                                 container=None)
        kube_port = port
        return Harness(port, counter, lambda model: kube.fail_next(kube_port.name(model)))
    settings = _settings(root)
    if backend == "sagemaker":
        sm_registry, sm_port, sm = sagemaker_pair(settings)
        return Harness(sm_port, _registry_provision(sm_registry, root),
                       lambda model: sm.fail_endpoint(sm_port.endpoint(model)))
    vx_registry, vx_port, vx = vertex_pair(settings)
    return Harness(vx_port, _registry_provision(vx_registry, root),
                   lambda model: vx.fail_next(model_id(model)))


@pytest.fixture(scope="module")
def stub(tmp_path_factory) -> Iterator[ServingStub]:
    with ServingStub(repository=str(tmp_path_factory.mktemp("triton-repo"))) as server:
        yield server


@pytest.fixture(scope="module", params=BACKENDS)
def harness(request, tmp_path_factory, stub) -> Harness:
    return build_harness(request.param, tmp_path_factory.mktemp(request.param), stub)


@pytest.mark.parametrize("check", list(ALL_CHECKS))
def test_conformance(harness, check) -> None:
    ctx = Context(harness.provision, prefix=check.replace("_", "-"),
                  inject_failure=harness.inject_failure)
    ALL_CHECKS[check](harness.port, ctx)


def test_every_installed_deployment_adapter_is_covered() -> None:
    assert set(plugins.adapters("deployment")) == set(BACKENDS)


def test_run_includes_failure_checks_only_with_injection(tmp_path, stub) -> None:
    h = build_harness("webhook", tmp_path, stub)
    assert run(h.port, Context(h.provision, prefix="run-a")) == list(CHECKS)
    with_failures = Context(h.provision, prefix="run-b", inject_failure=h.inject_failure)
    assert run(h.port, with_failures) == [*CHECKS, *FAILURE_CHECKS]


class _Memory:
    """A scripted in-memory serving system for the Deployer and conformance tests."""

    def __init__(self, *, settle: bool = True, restore_works: bool = True) -> None:
        self.serving: dict[str, str] = {}
        self.settle = settle
        self.restore_works = restore_works

    def ping(self) -> None:
        return None

    def status(self, model: str) -> DeploymentState:
        return DeploymentState(model=model, version=self.serving.get(model), ready=self.settle)

    def deploy(self, target: DeploymentTarget) -> None:
        self.serving[target.model] = target.version

    def restore(self, model: str, previous: DeploymentTarget | None) -> None:
        if not self.restore_works:
            return
        if previous is None:
            self.serving.pop(model, None)
        else:
            self.serving[model] = previous.version


def _target(model: str, version: str) -> DeploymentTarget:
    return DeploymentTarget(model=model, version=version)


def _count(backend: str, outcome: str) -> float:
    return metrics.DEPLOYMENTS.labels(backend=backend, outcome=outcome)._value.get()


def test_the_suite_catches_an_adapter_that_ignores_restore() -> None:
    labels = itertools.count(1)
    ctx = Context(lambda m: _target(m, str(next(labels))), timeout_s=0.1)
    with pytest.raises(ConformanceFailure, match="did not read back"):
        CHECKS["restore_previous"](_Memory(restore_works=False), ctx)


def test_rollout_that_never_settles_times_out_and_restores() -> None:
    class Stuck(_Memory):
        def status(self, model: str) -> DeploymentState:
            version = self.serving.get(model)
            return DeploymentState(model=model, version=version, ready=version != "2")

    port = Stuck()
    port.serving["m"] = "1"
    deployer = Deployer(port, backend="memory-timeout", timeout_s=0.05, poll_s=0.01)
    before = _count("memory-timeout", "failed")
    with pytest.raises(DeploymentError, match="restored 1") as caught:
        deployer.rollout(_target("m", "2"), _target("m", "1"))
    assert "DEPLOYMENT_TIMEOUT_S" in caught.value.context["cause"]
    assert caught.value.context["restored"] is True
    assert port.serving["m"] == "1"
    assert _count("memory-timeout", "failed") == before + 1


def test_rollout_that_settles_elsewhere_fails_fast() -> None:
    class Rejects(_Memory):
        def deploy(self, target: DeploymentTarget) -> None:
            return None  # accepted, then the system keeps the old version

    port = Rejects()
    port.serving["m"] = "1"
    deployer = Deployer(port, backend="memory", timeout_s=30, poll_s=0.01)
    with pytest.raises(DeploymentError) as caught:
        deployer.rollout(_target("m", "2"), _target("m", "1"))
    assert "settled at version 1" in caught.value.context["cause"]
    assert caught.value.context["restored"] is True


def test_rollout_reports_a_restore_that_did_not_hold() -> None:
    class Fails(_Memory):
        def status(self, model: str) -> DeploymentState:
            return DeploymentState(model=model, version="2", ready=False, failed=True,
                                   detail="crash-loop")

    deployer = Deployer(Fails(), backend="memory", timeout_s=0.05, poll_s=0.01)
    with pytest.raises(DeploymentError, match="could not be restored") as caught:
        deployer.rollout(_target("m", "2"), _target("m", "1"))
    assert caught.value.context["restored"] is False
    assert caught.value.context["detail"] == "crash-loop"


def test_successful_rollout_is_counted() -> None:
    deployer = Deployer(_Memory(), backend="memory-ok", timeout_s=1, poll_s=0.01)
    before = _count("memory-ok", "ok")
    state = deployer.rollout(_target("m", "3"), None)
    assert (state.version, state.ready) == ("3", True)
    assert _count("memory-ok", "ok") == before + 1


# ---- promotion keeps LIVE and the serving system in step -------------------------------


@pytest.fixture
def promotion_env(migrated_settings, tmp_path, stub):
    settings = migrated_settings.model_copy(update={
        "registry_backend": "filesystem",
        "registry_fs_root": str(tmp_path / "registry"),
        "artifact_store_root": str(tmp_path / "store"),
    })
    registry = build_registry(settings)
    model = f"cell_{uuid.uuid4().hex[:8]}"
    for label in ("a", "b"):
        registry.create_version(model, _artifact(tmp_path, model, label))
    session_factory = make_session_factory(create_db_engine(settings.database_url))
    with session_scope(session_factory) as session:
        session.add(ModelMetadata(model_id=model.replace("_", "-"), mlflow_model_name=model))
    webhook = WebhookDeployment(HttpApi(stub.url, service="webhook", http_factory=httpx.Client,
                                        token=StaticToken(None)))
    deployer = Deployer(webhook, backend="webhook", timeout_s=10, poll_s=0.01)
    return settings, registry, session_factory, deployer, model


def _promote(env, version: str, tmp_path: Path):
    settings, registry, session_factory, deployer, model = env
    with session_scope(session_factory) as session:
        return promote_version(
            session, registry, deployer=deployer, model_id=model.replace("_", "-"),
            version=version, kind=PromotionKind.PROMOTE_CANDIDATE,
            live_alias=settings.live_alias, workdir=str(tmp_path / "w"),
        )


def test_promotion_rolls_out_and_reads_back(promotion_env, tmp_path) -> None:
    settings, registry, _, deployer, model = promotion_env
    result = _promote(promotion_env, "1", tmp_path)
    assert result.status == "APPLIED"
    assert registry.get_version_by_alias(model, settings.live_alias) == "1"
    state = deployer.port.status(model)
    assert (state.version, state.ready) == ("1", True)


def test_failed_rollout_restores_live_and_serving(promotion_env, tmp_path, stub) -> None:
    settings, registry, session_factory, deployer, model = promotion_env
    _promote(promotion_env, "1", tmp_path)
    stub.fail_next(model)
    with pytest.raises(PromotionError):
        _promote(promotion_env, "2", tmp_path)
    assert registry.get_version_by_alias(model, settings.live_alias) == "1"
    state = deployer.port.status(model)
    assert (state.version, state.ready, state.failed) == ("1", True, False)
    with session_scope(session_factory) as session:
        moved = session.query(ModelPromotion).filter_by(model_id=model.replace("_", "-")).all()
    assert [row.to_version for row in moved] == ["1"]
    # The next attempt succeeds, and serving follows.
    assert _promote(promotion_env, "2", tmp_path).status == "APPLIED"
    assert deployer.port.status(model).version == "2"


def test_default_backend_serves_the_live_alias(tmp_path) -> None:
    settings = _settings(tmp_path)
    registry = build_registry(settings)
    deployer = build_deployer(settings, registry)
    assert isinstance(deployer.port, RegistryAliasDeployment)
    assert deployer.port.alias == settings.live_alias
    version = registry.create_version("kpi", _artifact(tmp_path, "kpi", "a"))
    deployer.rollout(_target("kpi", version), None)
    assert registry.get_version_by_alias("kpi", settings.live_alias) == version


# ---- configuration ---------------------------------------------------------------------


@pytest.mark.smoke
def test_selected_adapter_demands_its_keys(tmp_path) -> None:
    for backend, key in (("webhook", "DEPLOYMENT_WEBHOOK_URL"), ("bentoml", "BENTOML_URL"),
                         ("triton", "TRITON_URL"), ("kserve", "K8S_API_URL"),
                         ("gitops", "GITOPS_REPO_DIR")):
        with pytest.raises(ConfigurationError, match=key):
            _settings(tmp_path, deployment_backend=backend)


def test_adapters_refuse_inconsistent_config(tmp_path) -> None:
    with pytest.raises(ConfigurationError, match="CANDIDATE_ALIAS"):
        build_deployment(_settings(tmp_path, deployment_alias="candidate",
                                   candidate_alias="candidate"),
                         build_registry(_settings(tmp_path)))
    registry = build_registry(_settings(tmp_path))
    for backend in ("sagemaker", "vertex"):
        with pytest.raises(ConfigurationError, match="REGISTRY_BACKEND"):
            spec = plugins.adapters("deployment")[backend]
            spec.factory(_settings(tmp_path), registry)
    k8s = {"deployment_backend": "kserve", "k8s_api_url": "https://k8s.invalid"}
    with pytest.raises(ConfigurationError, match="must contain"):
        build_deployment(_settings(tmp_path, **k8s, kserve_storage_uri_template="s3://m/{name}"),
                         registry)
    with pytest.raises(ConfigurationError, match="unknown placeholders"):
        build_deployment(
            _settings(tmp_path, **k8s, kserve_storage_uri_template="s3://{bucket}/{version}"),
            registry,
        )
    base = tmp_path / "config.pbtxt"
    base.write_text("version_policy: { latest: { num_versions: 1 } }", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="version_policy"):
        build_deployment(
            _settings(tmp_path, deployment_backend="triton", triton_url="http://t.invalid",
                      triton_repository=str(tmp_path), triton_base_config=str(base)),
            registry,
        )
    with pytest.raises(ConfigurationError, match="not a git checkout"):
        build_deployment(
            _settings(tmp_path, deployment_backend="gitops", gitops_repo_dir=str(tmp_path),
                      gitops_status_url="http://s.invalid"),
            registry,
        )


def test_gitops_manifest_path_cannot_escape_the_checkout(tmp_path) -> None:
    repo = init_git_repo(tmp_path / "repo")
    port = gitops_deployment(repo, demu.GitOpsController(str(repo)))
    port.manifest_path = "../outside/{name}.json"
    with pytest.raises(ConfigurationError, match="outside GITOPS_REPO_DIR"):
        port.deploy(_target("kpi", "1"))
    assert not (tmp_path / "outside").exists()


@pytest.mark.smoke
def test_dns_names_are_safe_and_distinct() -> None:
    assert dns_name("", "kpi-model") == "kpi-model"
    odd = [dns_name("", n) for n in ("cell_a", "cell.a", "Cell-A", "x" * 80)]
    assert len(set(odd)) == len(odd)
    assert all(len(n) <= 63 and n[0].isalpha() and n == n.lower() for n in odd)


def test_webhook_sends_the_token_and_reports_auth_failures(tmp_path) -> None:
    with ServingStub(token="s3cret") as server:
        good = WebhookDeployment(HttpApi(server.url, service="webhook", http_factory=httpx.Client,
                                         token=StaticToken(SecretStr("s3cret"))))
        Deployer(good, backend="webhook", timeout_s=5, poll_s=0.01).rollout(_target("m", "1"), None)
        bad = WebhookDeployment(HttpApi(server.url, service="webhook", http_factory=httpx.Client,
                                        token=StaticToken(SecretStr("wrong"))))
        with pytest.raises(DeploymentUnavailableError, match="HTTP 401"):
            bad.status("m")


def test_readiness_reports_an_unreachable_serving_system(migrated_settings) -> None:
    settings = migrated_settings.model_copy(update={
        "deployment_backend": "webhook", "deployment_webhook_url": "http://127.0.0.1:9",
        "deployment_http_timeout_s": 2.0,
    })
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/ready")
    assert response.status_code == 503
    parts = {c["name"]: c for c in response.json()["components"]}
    assert parts["deployment"]["ok"] is False
    assert parts["database"]["ok"] is True


# ---- live runs (heavy: never part of the phase gate) -----------------------------------


def _live(backend: str) -> Settings:
    """Settings from the environment with DEPLOYMENT_BACKEND=<backend>; skips when that
    adapter's required keys are unset."""
    keys = plugins.adapters("deployment")[backend].capability.required_keys
    missing = [k for k in keys if not os.environ.get(k.upper())]
    if missing:
        pytest.skip(f"live {backend} run needs {', '.join(k.upper() for k in missing)}"
                    " [owner=TNDEVIL4U expires=2027-03-31]")
    return Settings(deployment_backend=backend)


@pytest.mark.heavy
@pytest.mark.parametrize("backend_name", [b for b in BACKENDS if b != "registry-alias"])
def test_live_conformance(backend_name, tmp_path) -> None:
    settings = _live(backend_name)
    registry = build_registry(settings)
    port = build_deployment(settings, registry)
    prefix = f"oran-conformance-{uuid.uuid4().hex[:8]}"
    ctx = Context(_registry_provision(registry, tmp_path), prefix=prefix,
                  timeout_s=settings.deployment_timeout_s, poll_s=settings.deployment_poll_s)
    assert run(port, ctx) == list(CHECKS)


def test_live_runs_skip_without_config(monkeypatch) -> None:
    monkeypatch.delenv("K8S_API_URL", raising=False)
    with pytest.raises(pytest.skip.Exception, match="K8S_API_URL"):
        _live("kserve")
