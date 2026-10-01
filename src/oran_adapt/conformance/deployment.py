"""Conformance suite for ``DeploymentPort`` adapters.

Each check takes an adapter and a ``Context`` and raises ConformanceFailure on a deviation. The
context's ``provision(model, version_label)`` makes a deployable version exist wherever the
adapter needs it (registered in the adapter's registry, an artifact directory to stage) and
returns its DeploymentTarget; model names come from ``Context.name`` so checks never collide::

    @pytest.mark.parametrize("check", sorted(CHECKS))
    def test_my_adapter(check, tmp_path):
        CHECKS[check](MyDeployment(...), Context(provision=my_provision))

``FAILURE_CHECKS`` need ``Context.inject_failure(model)``, which makes the serving system reject
or fail the next rollout of that model; run them when the adapter's test double can do that.
Every rollout goes through ``Deployer``, so a check passes only if the adapter's ``status``
reads back what ``deploy``/``restore`` asked for. docs/adapters/deployment.md explains each rule.
"""

from __future__ import annotations

import itertools
import pickle
from collections.abc import Callable
from dataclasses import dataclass, field

from oran_adapt.conformance import ConformanceFailure, expect
from oran_adapt.core.errors import DeploymentError
from oran_adapt.ports import DeploymentPort, DeploymentTarget
from oran_adapt.registry.deployment import Deployer

_names = itertools.count(1)


@dataclass
class Context:
    provision: Callable[[str], DeploymentTarget]
    """``provision(model)``: a new deployable version of ``model`` (1, 2, … on each call)."""
    prefix: str = "conformance"
    timeout_s: float = 10.0
    poll_s: float = 0.01
    inject_failure: Callable[[str], None] | None = None
    _run: int = field(default_factory=lambda: next(_names), repr=False)

    def name(self, label: str) -> str:
        return f"{self.prefix}-{label}-{self._run}"

    def deployer(self, port: DeploymentPort) -> Deployer:
        return Deployer(port, backend="conformance", timeout_s=self.timeout_s, poll_s=self.poll_s)


def _serving(deployer: Deployer, model: str, version: str | None, what: str) -> None:
    state = deployer.port.status(model)
    expect(
        state.ready and state.version == version and not state.failed,
        f"{what}: status reads {state}, expected version {version} ready",
    )


def check_protocol(port: DeploymentPort, ctx: Context) -> None:
    expect(isinstance(port, DeploymentPort), "does not implement DeploymentPort")


def check_ping(port: DeploymentPort, ctx: Context) -> None:
    port.ping()


def check_fresh_status(port: DeploymentPort, ctx: Context) -> None:
    state = port.status(ctx.name("fresh"))
    expect(state.version is None, f"a model never deployed reads as {state.version!r}")
    expect(not state.failed, "a model never deployed reads as failed")


def check_deploy_reads_back(port: DeploymentPort, ctx: Context) -> None:
    model = ctx.name("deploy")
    target = ctx.provision(model)
    deployer = ctx.deployer(port)
    state = deployer.rollout(target, None)
    expect(state.version == target.version, f"rollout returned {state}")
    _serving(deployer, model, target.version, "after deploy")


def check_redeploy_moves(port: DeploymentPort, ctx: Context) -> None:
    model = ctx.name("move")
    first, second = ctx.provision(model), ctx.provision(model)
    deployer = ctx.deployer(port)
    deployer.rollout(first, None)
    deployer.rollout(second, first)
    _serving(deployer, model, second.version, "after deploying a newer version")


def check_restore_previous(port: DeploymentPort, ctx: Context) -> None:
    model = ctx.name("restore")
    first, second = ctx.provision(model), ctx.provision(model)
    deployer = ctx.deployer(port)
    deployer.rollout(first, None)
    deployer.rollout(second, first)
    expect(deployer.revert(model, first), "restoring the previous version did not read back")
    _serving(deployer, model, first.version, "after restore")


def check_restore_none(port: DeploymentPort, ctx: Context) -> None:
    model = ctx.name("undeploy")
    target = ctx.provision(model)
    deployer = ctx.deployer(port)
    deployer.rollout(target, None)
    expect(deployer.revert(model, None), "undeploying did not read back")
    state = port.status(model)
    expect(state.version is None, f"after undeploy status reads version {state.version!r}")


def check_idempotent_redeploy(port: DeploymentPort, ctx: Context) -> None:
    model = ctx.name("again")
    target = ctx.provision(model)
    deployer = ctx.deployer(port)
    deployer.rollout(target, None)
    deployer.rollout(target, target)
    _serving(deployer, model, target.version, "after deploying the same version twice")


def check_pickle(port: DeploymentPort, ctx: Context) -> None:
    """Jobs run in worker processes: the adapter must survive pickling and still work."""
    copy = pickle.loads(pickle.dumps(port))
    model = ctx.name("pickle")
    target = ctx.provision(model)
    deployer = ctx.deployer(copy)
    deployer.rollout(target, None)
    _serving(deployer, model, target.version, "through an unpickled copy")


def check_failed_rollout_restored(port: DeploymentPort, ctx: Context) -> None:
    if ctx.inject_failure is None:
        raise ConformanceFailure("failed_rollout_restored needs Context.inject_failure")
    model = ctx.name("fail")
    first, second = ctx.provision(model), ctx.provision(model)
    deployer = ctx.deployer(port)
    deployer.rollout(first, None)
    ctx.inject_failure(model)
    try:
        deployer.rollout(second, first)
    except DeploymentError as exc:
        expect(exc.context.get("restored") is True, f"the failed rollout was not restored: {exc}")
    else:
        raise ConformanceFailure("a rollout the serving system failed was reported as a success")
    _serving(deployer, model, first.version, "after a failed rollout")


CHECKS: dict[str, Callable[[DeploymentPort, Context], None]] = {
    "protocol": check_protocol,
    "ping": check_ping,
    "fresh_status": check_fresh_status,
    "deploy_reads_back": check_deploy_reads_back,
    "redeploy_moves": check_redeploy_moves,
    "restore_previous": check_restore_previous,
    "restore_none": check_restore_none,
    "idempotent_redeploy": check_idempotent_redeploy,
    "pickle": check_pickle,
}

FAILURE_CHECKS: dict[str, Callable[[DeploymentPort, Context], None]] = {
    "failed_rollout_restored": check_failed_rollout_restored,
}


def run(port: DeploymentPort, ctx: Context) -> list[str]:
    """Run every check (and the failure checks when ``ctx.inject_failure`` is set) in order;
    returns their names. Stops at the first failure."""
    checks = dict(CHECKS)
    if ctx.inject_failure is not None:
        checks.update(FAILURE_CHECKS)
    for check in checks.values():
        check(port, ctx)
    return list(checks)


__all__ = ["CHECKS", "FAILURE_CHECKS", "ConformanceFailure", "Context", "run"]
