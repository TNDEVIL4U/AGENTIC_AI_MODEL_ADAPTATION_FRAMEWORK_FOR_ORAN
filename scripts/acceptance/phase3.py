"""Hardening Phase 3 acceptance: deployment and serving propagation, checked end to end.

Run by scripts/verify.sh 3 after lint, the import-boundary test and the scoped tests.

1. Every installed deployment adapter passes the deployment conformance suite, including the
   failed-rollout check: registry-alias on the filesystem registry, webhook / bentoml / triton
   against the local HTTP serving stub, gitops against a real git checkout, and kserve /
   seldon / k8s / sagemaker / vertex against the API emulators. Stub and emulators are not
   the real systems: unverified against them.
2. Promotion rolls the version out and reads it back from the serving system; when the rollout
   fails, both LIVE and the serving system are back on the previous version and the history
   records no move.
3. With DEPLOYMENT_BACKEND unset, deployment is the registry-alias adapter on LIVE_ALIAS, so
   existing installations behave as before.
4. Vendor SDKs stay inside their adapters, every deployment adapter is documented in
   docs/adapters/deployment.md, and readiness reports the serving system.

Exit status 0 means every check passed.
"""

from __future__ import annotations

import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests" / "unit"
sys.path.insert(0, str(TESTS))  # serving_stub, emulators, test helpers


def conformance_everywhere(tmp: Path) -> str:
    import test_phase3_deployment as t
    from serving_stub import ServingStub

    from oran_adapt import plugins
    from oran_adapt.conformance.deployment import CHECKS, FAILURE_CHECKS, Context, run

    installed = set(plugins.adapters("deployment"))
    assert installed == set(t.BACKENDS), f"adapters without a harness: {installed ^ set(t.BACKENDS)}"
    repo = tmp / "triton-repo"
    repo.mkdir()
    with ServingStub(repository=str(repo)) as stub:
        for backend in t.BACKENDS:
            root = tmp / backend
            root.mkdir()
            h = t.build_harness(backend, root, stub)
            ran = run(h.port, Context(h.provision, prefix="acceptance",
                                      inject_failure=h.inject_failure))
            assert ran == [*CHECKS, *FAILURE_CHECKS], f"{backend}: ran {ran}"
    return (f"{len(t.BACKENDS)} adapters x {len(CHECKS) + len(FAILURE_CHECKS)} checks "
            "(stub/emulators; unverified against real serving systems)")


def promotion_keeps_live_and_serving_in_step(tmp: Path) -> str:
    import httpx
    import test_phase3_deployment as t
    from serving_stub import ServingStub

    from oran_adapt.adapters.deployment._common import HttpApi, StaticToken
    from oran_adapt.adapters.deployment.webhook import WebhookDeployment
    from oran_adapt.bootstrap import build_registry
    from oran_adapt.core.enums import PromotionKind
    from oran_adapt.core.errors import PromotionError
    from oran_adapt.db.base import create_db_engine, make_session_factory, session_scope
    from oran_adapt.db.migrate import upgrade_to_head
    from oran_adapt.db.models import ModelMetadata, ModelPromotion
    from oran_adapt.registry.deployment import Deployer
    from oran_adapt.registry.promotion import promote_version

    settings = t._settings(tmp)
    upgrade_to_head(settings.database_url)
    registry = build_registry(settings)
    for label in ("a", "b"):
        registry.create_version("kpi", t._artifact(tmp, "kpi", label))
    sessions = make_session_factory(create_db_engine(settings.database_url))
    with session_scope(sessions) as session:
        session.add(ModelMetadata(model_id="kpi", mlflow_model_name="kpi"))

    with ServingStub() as stub:
        port = WebhookDeployment(HttpApi(stub.url, service="webhook", http_factory=httpx.Client,
                                         token=StaticToken(None)))
        deployer = Deployer(port, backend="webhook", timeout_s=10, poll_s=0.01)

        def promote(version: str) -> str:
            with session_scope(sessions) as session:
                return promote_version(
                    session, registry, deployer=deployer, model_id="kpi", version=version,
                    kind=PromotionKind.PROMOTE_CANDIDATE, live_alias=settings.live_alias,
                    workdir=str(tmp / "work"),
                ).status

        def where() -> tuple[str, str | None, bool]:
            state = port.status("kpi")
            return registry.get_version_by_alias("kpi", settings.live_alias), state.version, state.ready

        assert promote("1") == "APPLIED" and where() == ("1", "1", True), where()
        stub.fail_next("kpi")
        try:
            promote("2")
        except PromotionError:
            pass
        else:
            raise AssertionError("a failed rollout was reported as a promotion")
        assert where() == ("1", "1", True), f"after the failed rollout: {where()}"
        with session_scope(sessions) as session:
            moves = [r.to_version for r in session.query(ModelPromotion).filter_by(model_id="kpi")]
        assert moves == ["1"], f"history records {moves}"
        assert promote("2") == "APPLIED" and where() == ("2", "2", True), where()
    return "promote 1 -> serving 1; failed rollout of 2 -> LIVE and serving back on 1; retry -> 2"


def default_is_the_live_alias(tmp: Path) -> str:
    import test_phase3_deployment as t

    from oran_adapt.adapters.deployment.alias import RegistryAliasDeployment
    from oran_adapt.bootstrap import build_deployer, build_registry

    settings = t._settings(tmp)
    assert settings.deployment_backend == "registry-alias"
    deployer = build_deployer(settings, build_registry(settings))
    assert isinstance(deployer.port, RegistryAliasDeployment), type(deployer.port)
    assert deployer.port.alias == settings.live_alias
    return f"DEPLOYMENT_BACKEND=registry-alias serving alias '{settings.live_alias}'"


def boundaries_docs_and_readiness(tmp: Path) -> str:
    from test_import_boundary import violations

    from oran_adapt import plugins
    from oran_adapt.api import routes_health

    found = violations()
    assert not found, f"vendor imports outside their adapters: {found}"
    path = ROOT / "docs" / "adapters" / "deployment.md"
    assert path.is_file(), f"{path.relative_to(ROOT)} is missing"
    guide = path.read_text(encoding="utf-8")
    undocumented = [a for a in plugins.adapters("deployment") if f"`{a}`" not in guide]
    assert not undocumented, f"not in docs/adapters/deployment.md: {undocumented}"
    source = Path(routes_health.__file__).read_text(encoding="utf-8")
    assert '"deployment": request.app.state.deployer.port.ping' in source
    return "import boundary clean; all adapters documented; /ready checks the serving system"


CHECKS: list[tuple[str, Callable[[Path], str]]] = [
    ("conformance suite green for every deployment adapter", conformance_everywhere),
    ("promotion rolls out, reads back, and undoes a failed rollout",
     promotion_keeps_live_and_serving_in_step),
    ("the default deployment is the live alias", default_is_the_live_alias),
    ("boundaries, documentation and readiness", boundaries_docs_and_readiness),
]


def main() -> int:
    failed = 0
    for name, check in CHECKS:
        with tempfile.TemporaryDirectory(prefix="oran-phase3-", ignore_cleanup_errors=True) as tmp:
            try:
                detail = check(Path(tmp))
            except AssertionError as exc:
                failed += 1
                print(f"FAIL  {name}\n      {exc}")
            else:
                print(f"PASS  {name}\n      {detail}")
    print(f"\nphase 3 acceptance: {len(CHECKS) - failed}/{len(CHECKS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
