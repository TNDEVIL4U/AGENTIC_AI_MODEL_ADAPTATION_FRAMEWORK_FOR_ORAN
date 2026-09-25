"""Phase K (Rules 16, 17 and 25): every API route, taken from the app's own route table, is
checked against the role policy - so a new write endpoint without a role check fails here - and
the metrics and error bodies carry no secrets."""

from __future__ import annotations

import re

from test_phase14_stage_b import KEYS, _h, secured, secured_settings

from oran_adapt.api.security import DATA_ROLES, PROMOTE_ROLES, READ_ROLES, SUBMIT_ROLES
from oran_adapt.core.enums import Role

__all__ = ["secured", "secured_settings"]  # fixtures re-used from test_phase14_stage_b

PUBLIC = {"/api/v1/health", "/api/v1/ready", "/api/v1/readiness", "/api/v1/metrics"}

# Every state-changing route and the roles allowed to call it.
WRITE_POLICY = {
    ("POST", "/api/v1/adaptation/events"): SUBMIT_ROLES,
    ("POST", "/api/v1/datasets"): DATA_ROLES,
    ("POST", "/api/v1/datasets/{dataset_id}/versions"): DATA_ROLES,
    ("POST", "/api/v1/datasets/{dataset_id}/cdc/materialize"): DATA_ROLES,
    ("POST", "/api/v1/models/attach"): DATA_ROLES,
    ("POST", "/api/v1/models/{model_id}/rollback"): PROMOTE_ROLES,
}

ROLE_OF = {who: Role(spec.partition(":")[0]) for who, (_, spec) in KEYS.items()}


def _routes(app) -> list[tuple[str, str]]:
    # The OpenAPI schema lists every mounted route with its full prefix (included routers are
    # nested in app.routes). Only /metrics is hidden from the schema, and it is public.
    routes = sorted(
        (method.upper(), path)
        for path, ops in app.openapi()["paths"].items()
        if path.startswith("/api/v1")
        for method in ops
    )
    assert len(routes) >= 15, routes  # never pass vacuously on an empty route table
    return routes


def _call(client, method: str, path: str, **kw):
    concrete = re.sub(r"\{[^}]+\}", "x", path)
    return client.request(method, concrete, **kw)


def test_every_write_route_has_a_declared_role_policy(secured) -> None:
    writes = {r for r in _routes(secured.app) if r[0] != "GET"}
    assert writes == set(WRITE_POLICY), "a write route was added or removed: update WRITE_POLICY"


def test_every_protected_route_refuses_a_missing_or_wrong_key(secured) -> None:
    for method, path in _routes(secured.app):
        if path in PUBLIC:
            continue
        for headers in ({}, {"X-API-Key": "not-a-real-key-000000"}):
            r = _call(secured, method, path, json={}, headers=headers)
            assert r.status_code == 401, (method, path, r.status_code)
            assert r.json()["code"] == "UNAUTHENTICATED"


def test_each_role_reaches_exactly_the_routes_its_policy_allows(secured) -> None:
    for method, path in _routes(secured.app):
        if path in PUBLIC:
            continue
        allowed = WRITE_POLICY.get((method, path), READ_ROLES)
        for who, role in ROLE_OF.items():
            # An empty body: allowed callers get past the role check and stop at validation
            # (422) or a missing resource (404); nothing is created.
            r = _call(secured, method, path, json={}, headers=_h(who))
            if role in allowed:
                assert r.status_code not in (401, 403), (method, path, who, r.status_code)
            else:
                assert r.status_code == 403, (method, path, who, r.status_code)
                assert r.json()["code"] == "FORBIDDEN"


def test_errors_are_structured_and_echo_no_input_or_secret(secured) -> None:
    secret = "sk-ant-should-never-be-echoed"
    r = secured.post(
        "/api/v1/adaptation/events",
        json={"model_id": 12345, "note": secret},
        headers={**_h("admin"), "X-Correlation-ID": "corr-phase-k"},
    )
    assert r.status_code == 422
    body = r.json()
    assert set(body) == {"code", "message", "context"} and body["code"] == "INVALID_REQUEST"
    assert secret not in r.text
    assert r.headers["X-Correlation-ID"] == "corr-phase-k"

    missing = secured.get("/api/v1/models/no-such-model", headers=_h("reader"))
    assert missing.status_code == 404 and missing.json()["code"] == "MODEL_NOT_FOUND"
    assert "Traceback" not in missing.text


def test_metrics_cover_rule17_and_expose_no_keys(secured, secured_settings) -> None:
    text = secured.get("/api/v1/metrics").text
    for name in (
        "http_requests",
        "adaptation_jobs",
        "adaptation_duration_seconds",
        "adaptation_failure",
        "job_timeouts",
        "drift_events",
        "strategy_selected",
        "fine_tune",
        "retrain",
        "llm_failures",
        "sandbox_failures",
        "validation_failure",
        "model_registrations",
        "model_promotions",
        "rollback",
        "cdc_events",
        "cdc_processing_lag",
    ):
        assert re.search(rf"^# TYPE {name}(_total)? ", text, re.MULTILINE), name
    for key, _ in KEYS.values():
        assert key not in text
    for digest in secured_settings.api_keys:
        assert digest not in text
