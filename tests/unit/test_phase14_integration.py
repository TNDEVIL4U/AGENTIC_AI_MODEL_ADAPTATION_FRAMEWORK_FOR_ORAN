"""Hardening Phase 14: integration and documentation.

Monitoring -> DriftEvent mappers (core.event_mapping) over every shipped mapping and sample, through
the API route and the CLI; the ``opa`` policy adapter against an httpx.MockTransport OPA (the
conformance suite, fail-closed behaviour, the cache); the example configs for each target stack
pass config lint; the capability matrix is current; every Unknown-Stack default has an ADR whose
selector and default match the settings. The OpenAPI document and the clone-to-canary walkthrough
are checked by scripts/acceptance/phase14.py (each builds a full app).
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from oran_adapt import cli, plugins
from oran_adapt.adapters.opa import OpaPolicy
from oran_adapt.api.app import create_app
from oran_adapt.conformance import policy as policy_suite
from oran_adapt.core.config import ADAPTER_SELECTORS, Settings, lint_config_file
from oran_adapt.core.config_sources import read_config_file
from oran_adapt.core.enums import Role
from oran_adapt.core.errors import ConfigurationError, EventMappingError, PermissionDeniedError
from oran_adapt.core.event_mapping import (
    DriftMapping,
    check_mappers,
    load_mapping,
    map_payload,
    resolve,
)
from oran_adapt.ports import Principal

ROOT = Path(__file__).resolve().parents[2]
MAPPERS = ROOT / "config" / "mappers"
SAMPLES = MAPPERS / "samples"
EXAMPLES = ROOT / "config" / "examples"
TARGET_STACKS = {
    "mlflow-kserve": ("mlflow", "kserve"),
    "sagemaker": ("sagemaker", "sagemaker"),
    "vertex": ("vertex", "vertex"),
    "seldon": ("mlflow", "seldon"),
    "triton": ("filesystem", "triton"),
    "bentoml": ("mlflow", "bentoml"),
    "airgapped-filesystem": ("filesystem", "registry-alias"),
}


def _sample(name: str) -> dict:
    return json.loads((SAMPLES / f"{name}.json").read_text(encoding="utf-8"))


def _mapping(name: str) -> DriftMapping:
    return load_mapping(str(MAPPERS / f"{name}.toml"))


# ---- the shipped mappers ----------------------------------------------------------------------


def test_alertmanager_maps_firing_alerts_only() -> None:
    events = map_payload(_mapping("alertmanager"), _sample("alertmanager"), max_events=10)
    assert len(events) == 1
    event = events[0]
    assert event.model_id == "cell-a-throughput" and event.model_version == "1"
    assert event.dataset_id == "cell-a-kpis" and event.drifted_data_version == "drift-1"
    assert event.severity == "MEDIUM" and event.drift_score == pytest.approx(0.42)
    assert event.affected_features == ["prb_util", "cqi"]
    assert event.event_id == "5e1f3a0c9b7d2e41@2026-03-01T12:00:00Z"


def test_evidently_report_becomes_one_event_with_the_drifted_columns() -> None:
    events = map_payload(_mapping("evidently"), _sample("evidently"), max_events=10,
                         overrides={"model_id": "cell-a-throughput", "dataset_id": None})
    [event] = events
    assert event.model_id == "cell-a-throughput" and event.drift_detected is True
    assert event.drift_type == "feature" and event.affected_features == ["cqi", "prb_util"]
    assert set(event.drift_metrics) == {"cqi", "prb_util"}
    assert event.event_id is not None and event.event_id.startswith("cell-a-throughput@")


def test_evidently_without_a_model_id_is_refused_naming_the_field() -> None:
    with pytest.raises(EventMappingError) as info:
        map_payload(_mapping("evidently"), _sample("evidently"), max_events=10)
    assert info.value.code == "EVENT_MAPPING_FAILED"
    assert any(p["field"] == "model_id" for p in info.value.context["problems"])


def test_generic_json_keeps_only_detected_records() -> None:
    events = map_payload(_mapping("generic-json"), _sample("generic-json"), max_events=10)
    assert [e.event_id for e in events] == ["det-001"]


def test_every_shipped_mapper_has_a_sample_that_maps() -> None:
    for path in sorted(MAPPERS.glob("*.toml")):
        mapping = load_mapping(str(path))
        overrides = {"model_id": "m"} if path.stem == "evidently" else None
        assert map_payload(mapping, _sample(path.stem), max_events=10, overrides=overrides)


# ---- mapping rules and failures ---------------------------------------------------------------


def test_resolve_dotted_paths_and_list_selection() -> None:
    doc = {"a": {"b": [{"k": "x", "v": 1}, {"k": "y", "v": 2}]}, "flag": True}
    assert resolve(doc, "a.b[k=y].v") == 2
    missing = resolve(doc, "nope")
    assert resolve(doc, "a.b[k=z].v") is missing and resolve(doc, "a.missing") is missing
    assert resolve(doc, "flag") is True


def test_unmapped_severity_is_refused_with_the_known_values() -> None:
    payload = _sample("alertmanager")
    payload["alerts"][0]["labels"]["severity"] = "catastrophic"
    with pytest.raises(EventMappingError) as info:
        map_payload(_mapping("alertmanager"), payload, max_events=10)
    assert info.value.context["field"] == "severity"
    assert "critical" in info.value.context["known"]


def test_too_many_events_and_wrong_shapes_are_refused() -> None:
    payload = _sample("alertmanager")
    payload["alerts"] = [payload["alerts"][0]] * 3
    with pytest.raises(EventMappingError, match="DRIFT_MAPPER_MAX_EVENTS"):
        map_payload(_mapping("alertmanager"), payload, max_events=2)
    with pytest.raises(EventMappingError, match="not a JSON object"):
        map_payload(_mapping("alertmanager"), [1, 2], max_events=2)
    with pytest.raises(EventMappingError, match="no list"):
        map_payload(_mapping("alertmanager"), {"alerts": "x"}, max_events=2)


def test_invalid_mappings_are_configuration_errors(tmp_path) -> None:
    bad = tmp_path / "bad.toml"
    for body, cause in (
        ('[fields]\nnot_a_field = "x"\n', "unknown DriftEvent field"),
        ('[fields]\nmodel_id = "a[b"\n', "invalid path"),
        ('[split]\naffected_features = ","\n', "no fields.affected_features"),
        ('records = "x"\n', "version"),
    ):
        bad.write_text(('version = "1"\n' if cause != "version" else "") + body,
                       encoding="utf-8")
        with pytest.raises(ConfigurationError, match="not a valid drift-event mapping") as info:
            load_mapping(str(bad))
        assert cause in info.value.context["cause"]
    with pytest.raises(ConfigurationError, match="not a valid mapper name"):
        check_mappers({"Bad Name": str(MAPPERS / "alertmanager.toml")})
    with pytest.raises((ConfigurationError, ValidationError)):
        Settings(_env_file=None, drift_mappers={"x": str(tmp_path / "missing.toml")})


# ---- API and CLI ------------------------------------------------------------------------------


@pytest.fixture
def mapped_client(migrated_settings):
    # The database queue only queues: the route is under test here, not the pipeline (the
    # walkthrough in scripts/acceptance/phase14.py runs a mapped event to a canary).
    settings = migrated_settings.model_copy(update={"job_queue_backend": "database",
                                                    "drift_mappers": {
        "alertmanager": str(MAPPERS / "alertmanager.toml"),
        "generic": str(MAPPERS / "generic-json.toml"),
    }})
    with TestClient(create_app(settings)) as client:
        yield client


def test_api_maps_submits_and_deduplicates(mapped_client) -> None:
    url = "/api/v1/adaptation/events/from/generic"
    first = mapped_client.post(url, json=_sample("generic-json"))
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["mapper"] == "generic" and body["events"] == 1 and len(body["jobs"]) == 1
    again = mapped_client.post(url, json=_sample("generic-json"))
    assert again.status_code == 200
    assert again.json()["jobs"][0]["job_id"] == body["jobs"][0]["job_id"]
    assert again.json()["jobs"][0]["duplicate"] is True


def test_api_query_overrides_and_errors(mapped_client) -> None:
    payload = _sample("alertmanager")
    payload["alerts"][0]["labels"]["severity"] = "catastrophic"
    bad = mapped_client.post("/api/v1/adaptation/events/from/alertmanager", json=payload)
    assert bad.status_code == 422 and bad.json()["code"] == "EVENT_MAPPING_FAILED"
    missing = mapped_client.post("/api/v1/adaptation/events/from/nope", json={})
    assert missing.status_code == 404 and missing.json()["code"] == "EVENT_MAPPER_NOT_FOUND"
    assert missing.json()["context"]["available"] == ["alertmanager", "generic"]
    resolved = {"alerts": [a for a in _sample("alertmanager")["alerts"]
                           if a["status"] == "resolved"]}
    none = mapped_client.post("/api/v1/adaptation/events/from/alertmanager", json=resolved)
    assert none.status_code == 200 and none.json()["events"] == 0
    over = mapped_client.post("/api/v1/adaptation/events/from/generic",
                              params={"model_id": "override-model"}, json=_sample("generic-json"))
    assert over.status_code == 201 and over.json()["jobs"][0]["model_id"] == "override-model"


def test_cli_event_map(migrated_settings, monkeypatch, capsys) -> None:
    settings = migrated_settings.model_copy(update={"drift_mappers": {
        "alertmanager": str(MAPPERS / "alertmanager.toml")}})
    monkeypatch.setattr(cli, "get_settings", lambda: settings)

    def run(*argv: str) -> tuple[int, dict]:
        code = cli.main(list(argv))
        return code, json.loads(capsys.readouterr().out)

    code, out = run("event", "map", "--mapper", "alertmanager",
                    "--input", str(SAMPLES / "alertmanager.json"))
    assert code == 0 and out["mapper"] == "alertmanager"
    assert [e["model_id"] for e in out["events"]] == ["cell-a-throughput"] and "jobs" not in out
    code, out = run("event", "map", "--mapping", str(MAPPERS / "evidently.toml"),
                    "--input", str(SAMPLES / "evidently.json"), "--model-id", "m1")
    assert code == 0 and out["events"][0]["model_id"] == "m1"
    code, out = run("event", "map", "--mapper", "nope", "--input", "x.json")
    assert code == 1 and out["code"] == "EVENT_MAPPER_NOT_FOUND"
    code, out = run("event", "map", "--mapper", "alertmanager", "--input",
                    str(SAMPLES / "missing.json"))
    assert code == 1 and out["code"] == "EVENT_MAPPING_FAILED"


# ---- the opa policy adapter -------------------------------------------------------------------


MATRIX = {
    "read": set(Role), "submit": {Role.ADMIN, Role.OPERATOR, Role.ML_ENGINEER},
    "data": {Role.ADMIN, Role.ML_ENGINEER}, "promote": {Role.ADMIN, Role.OPERATOR},
    "admin": {Role.ADMIN},
}


class FakeOpa:
    """OPA's data API for the rule ``oran_adapt/authz/allow`` over MATRIX."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/data/oran_adapt/authz/allow"
        body = json.loads(request.content)
        self.calls.append({"input": body["input"],
                           "auth": request.headers.get("Authorization")})
        if self.status != 200:
            return httpx.Response(self.status, json={"code": "internal_error"})
        action, role = body["input"]["action"], Role(body["input"]["role"])
        if action not in MATRIX:
            return httpx.Response(200, json={})  # an undefined decision
        return httpx.Response(200, json={"result": role in MATRIX[action]})


def _opa(fake: FakeOpa, **kwargs) -> OpaPolicy:
    return OpaPolicy("http://opa.local:8181", path=kwargs.pop("path", "oran_adapt/authz/allow"),
                     timeout_s=2, cache_s=kwargs.pop("cache_s", 30),
                     transport=httpx.MockTransport(fake), **kwargs)


def test_opa_policy_conformance() -> None:
    assert policy_suite.run(_opa(FakeOpa()), policy_suite.Context()) == list(policy_suite.CHECKS)


def test_opa_sends_the_principal_and_token_and_caches_role_answers() -> None:
    fake = FakeOpa()
    policy = _opa(fake, token=SecretStr("t0k"))
    assert policy.allowed_roles("promote") == frozenset({Role.ADMIN, Role.OPERATOR})
    asked = len(fake.calls)
    assert asked == len(Role) and fake.calls[0]["auth"] == "Bearer t0k"
    policy.allowed_roles("promote")
    assert len(fake.calls) == asked  # cached
    policy.authorize(Principal(name="noc", role=Role.OPERATOR), "promote")
    assert fake.calls[-1]["input"] == {"action": "promote", "role": "OPERATOR",
                                       "principal": {"name": "noc", "role": "OPERATOR"}}
    uncached = _opa(fake, cache_s=0)
    before = len(fake.calls)
    uncached.allowed_roles("admin")
    uncached.allowed_roles("admin")
    assert len(fake.calls) == before + 2 * len(Role)


def test_opa_fails_closed() -> None:
    fake = FakeOpa()
    fake.status = 500
    policy = _opa(fake)
    with pytest.raises(PermissionDeniedError):
        policy.authorize(Principal(name="a", role=Role.ADMIN), "read")
    with pytest.raises(PermissionDeniedError, match="no role"):
        policy.allowed_roles("read")

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    down = OpaPolicy("http://opa.local:8181", path="a/allow", timeout_s=1, cache_s=0,
                     transport=httpx.MockTransport(unreachable))
    with pytest.raises(PermissionDeniedError):
        down.authorize(Principal(name="a", role=Role.ADMIN), "read")
    blocked = OpaPolicy("http://169.254.169.254", path="a/allow", timeout_s=1, cache_s=0,
                        transport=httpx.MockTransport(FakeOpa()))
    blocked.policy = type(blocked.policy)([])  # nothing allowlisted: the request is blocked
    with pytest.raises(PermissionDeniedError):
        blocked.authorize(Principal(name="a", role=Role.ADMIN), "read")


def test_opa_configuration() -> None:
    with pytest.raises(ConfigurationError, match="POLICY_OPA_PATH"):
        _opa(FakeOpa(), path="//")
    spec = plugins.adapters("policy")["opa"]
    assert spec.capability.required_keys == ("policy_opa_url",)
    with pytest.raises(ConfigurationError):
        spec.factory(Settings(_env_file=None))
    built = spec.factory(Settings(_env_file=None, policy_opa_url="http://opa.local:8181",
                                  policy_opa_path="/x/y/allow/"))
    assert isinstance(built, OpaPolicy)
    assert built.endpoint == "http://opa.local:8181/v1/data/x/y/allow"


# ---- example configs, capability matrix, ADRs --------------------------------------------------


@pytest.mark.parametrize("stack", sorted(TARGET_STACKS))
def test_example_config_for_each_target_stack_lints(stack: str) -> None:
    path = EXAMPLES / f"{stack}.toml"
    assert lint_config_file(str(path)) == []
    values = read_config_file(Settings, str(path))
    assert values["environment"] == "production"
    assert (values["registry_backend"], values["deployment_backend"]) == TARGET_STACKS[stack]
    for mapper in values.get("drift_mappers", {}).values():
        load_mapping(mapper)


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_lint_refuses_a_canary_on_a_deployment_without_traffic_split(tmp_path) -> None:
    from oran_adapt.core.config import lint_config_file

    path = tmp_path / "canary.toml"
    path.write_text('deployment_backend = "triton"\ndelivery_strategy = "canary"\n'
                    '[triton]\nurl = "http://triton.local:8000"\nrepository = "/models"\n',
                    encoding="utf-8")
    problems = lint_config_file(str(path))
    assert len(problems) == 1 and "traffic_split" in problems[0], problems
    assert "triton has none" in problems[0]
    path.write_text(path.read_text(encoding="utf-8").replace('"canary"', '"blue_green"'),
                    encoding="utf-8")
    assert lint_config_file(str(path)) == []


def test_capability_matrix_is_current() -> None:
    matrix = _load_script("capability_matrix")
    text = matrix.render()
    assert text == (ROOT / "docs" / "capability-matrix.md").read_text(encoding="utf-8")
    for port in ADAPTER_SELECTORS.values():
        assert f"## {port.replace('_', '-')}\n" in text
    assert "| `opa` |" in text


def test_every_unknown_stack_default_has_an_adr() -> None:
    adrs = {}
    for path in sorted((ROOT / "docs" / "adr").glob("*.md")):
        text = path.read_text(encoding="utf-8")
        selector = re.search(r"^- \*\*Selector:\*\* `([A-Z_]+)`$", text, re.MULTILINE)
        default = re.search(r"^- \*\*Default:\*\* `([^`]+)`$", text, re.MULTILINE)
        if selector:
            assert default, f"{path.name} names a selector but no default"
            adrs[selector[1].lower()] = (default[1], path.name)
    index = (ROOT / "docs" / "adr" / "README.md").read_text(encoding="utf-8")
    for key in ADAPTER_SELECTORS:
        assert key in adrs, f"no ADR records the default of {key.upper()}"
        default, name = adrs[key]
        assert default == str(Settings.model_fields[key].default), (key, default)
        assert name in index, f"{name} is not listed in docs/adr/README.md"
