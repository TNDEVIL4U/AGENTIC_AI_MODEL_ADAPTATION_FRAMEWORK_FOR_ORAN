"""Hardening Phase 9: security.

Route policy (every route has an explicit policy; deny by default; the matrix doc is current),
the auth adapters (OIDC against a local issuer, gateway assertions, forwarded mTLS certificates),
the outbound policy (SSRF allowlisting, redirects, TLS), rate limits, security headers, the
Vault secrets adapter, the secret-leak scan and the supply-chain checks."""

from __future__ import annotations

import ast
import importlib.util
import json
import logging
import re
import socket
import ssl
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from security_doubles import (
    SigningKey,
    client_cert,
    forwarded,
    local_issuer,
    local_vault,
    make_ca,
    sign,
)

from oran_adapt.adapters.access import API_KEY, hash_api_key
from oran_adapt.adapters.auth import GATEWAY, MTLS, OIDC
from oran_adapt.adapters.secrets import ENV as SECRETS_ENV
from oran_adapt.adapters.secrets import FILE as SECRETS_FILE
from oran_adapt.adapters.vault import VAULT
from oran_adapt.api.app import create_app
from oran_adapt.api.ratelimit import TokenBuckets
from oran_adapt.api.security import (
    PUBLIC,
    authz_matrix_markdown,
    enforce_route_policies,
    route_policies,
)
from oran_adapt.conformance import ConformanceFailure
from oran_adapt.conformance import auth as conformance
from oran_adapt.core.config import Settings
from oran_adapt.core.enums import Role
from oran_adapt.core.errors import (
    AuthenticationError,
    ConfigurationError,
    OutboundBlockedError,
    PermissionDeniedError,
)
from oran_adapt.core.outbound import OutboundPolicy, outbound_client
from oran_adapt.ports import Principal

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "oran_adapt"
ROLE_MAP = {"oran-admin": "ADMIN", "oran-operator": "OPERATOR", "oran-ml": "ML_ENGINEER",
            "oran-viewer": "READ_ONLY"}
# Configured as secrets by the leak test; scripts/acceptance/phase9.py scans its log for it.
LEAK_SENTINEL = "sk-sentinel-DO-NOT-LOG-5f2c9a"
KEYS = {role: (f"{role.lower()}-key-0123456789", role) for role in Role}


def _keyed(migrated_settings, **extra):
    return migrated_settings.model_copy(update={
        "auth_enabled": True,
        "api_keys": {hash_api_key(key): spec for key, spec in KEYS.values()},
        **extra,
    })


# ---- route policy ------------------------------------------------------------------------------
def test_every_route_has_an_explicit_policy(migrated_settings) -> None:
    app = create_app(_keyed(migrated_settings))
    policies = route_policies(app)
    assert policies, "no routes found"
    missing = [(p.methods, p.path) for p in policies if not p.policy]
    assert not missing, f"routes without a policy: {missing}"
    public = {p.path for p in policies if p.policy == (PUBLIC,)}
    # Only liveness/readiness (and the API docs, while enabled) are open to anyone.
    assert public <= {"/api/v1/health", "/api/v1/ready", "/api/v1/readiness", "/docs",
                      "/docs/oauth2-redirect", "/redoc", "/openapi.json"}, public
    for p in policies:
        if p.path.startswith("/api/v1/") and p.path not in public and "metrics" not in p.path:
            assert "read" in p.policy, (p.path, p.policy)


def test_a_route_without_a_policy_stops_startup(migrated_settings) -> None:
    app = create_app(_keyed(migrated_settings))
    stray = APIRouter()

    @stray.get("/api/v1/unguarded")
    def unguarded() -> dict[str, str]:
        return {"ok": "yes"}

    app.include_router(stray)
    with pytest.raises(ConfigurationError) as info:
        enforce_route_policies(app)
    assert "/api/v1/unguarded" in str(info.value.context)


def _fill(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


def test_authz_matrix_is_deny_by_default_for_every_route_and_role(migrated_settings) -> None:
    settings = _keyed(migrated_settings)
    matrix = settings.policy_roles
    with TestClient(create_app(settings)) as client:
        for p in route_policies(client.app):
            if p.policy == (PUBLIC,) or not p.path.startswith("/api/"):
                continue
            conditional = "metrics" in p.path  # public while METRICS_PUBLIC
            for method in p.methods:
                url = _fill(p.path)
                if not conditional:
                    anon = client.request(method, url)
                    assert anon.status_code == 401, (method, url, anon.status_code)
                for role in Role:
                    allowed = all(role.value in matrix.get(a, []) for a in p.policy)
                    r = client.request(method, url, headers={"X-API-Key": KEYS[role][0]})
                    if allowed or conditional:
                        assert r.status_code != 403, (method, url, role, r.json())
                    else:
                        assert r.status_code == 403, (method, url, role, r.status_code)


def test_an_action_missing_from_the_policy_is_denied_to_everyone(migrated_settings) -> None:
    roles = dict(migrated_settings.policy_roles)
    roles.pop("promote")
    settings = _keyed(migrated_settings, policy_roles=roles)
    with TestClient(create_app(settings)) as client:
        r = client.post("/api/v1/models/x/rollback", json={},
                        headers={"X-API-Key": KEYS[Role.ADMIN][0]})
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN"


def test_the_authz_matrix_document_is_current(migrated_settings) -> None:
    app = create_app(_keyed(migrated_settings, api_docs_enabled=False))
    expected = authz_matrix_markdown(app, migrated_settings.policy_roles)
    doc = (ROOT / "docs" / "security" / "authz-matrix.md").read_text(encoding="utf-8")
    assert expected in doc, "docs/security/authz-matrix.md is stale; regenerate it (see its header)"


def test_api_docs_are_off_in_production_and_security_headers_are_set(migrated_settings) -> None:
    off = _keyed(migrated_settings, api_docs_enabled=False, api_hsts_max_age_s=31536000)
    with TestClient(create_app(off)) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404
        r = client.get("/api/v1/health")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "no-referrer"
    assert r.headers["Cache-Control"] == "no-store"
    assert "default-src 'none'" in r.headers["Content-Security-Policy"]
    assert r.headers["Strict-Transport-Security"] == "max-age=31536000"
    with TestClient(create_app(_keyed(migrated_settings))) as client:  # development default
        assert client.get("/openapi.json").status_code == 200
        assert "Strict-Transport-Security" not in client.get("/api/v1/health").headers


# ---- OIDC against a local issuer ---------------------------------------------------------------
def _oidc_settings(migrated_settings, issuer_url: str, **extra):
    return migrated_settings.model_copy(update={
        "auth_enabled": True, "auth_backend": "oidc", "auth_oidc_issuer": issuer_url,
        "auth_oidc_audience": "oran-adapt", "auth_role_map": ROLE_MAP, **extra,
    })


def test_oidc_accepts_valid_tokens_from_the_local_issuer(migrated_settings) -> None:
    rsa_key, ec_key = SigningKey.rsa("r1"), SigningKey.ec("e1")
    with local_issuer(rsa_key, ec_key) as issuer:
        settings = _oidc_settings(migrated_settings, issuer.url,
                                  auth_jwt_algorithms=["RS256", "ES256"])
        with TestClient(create_app(settings)) as client:
            for key in (rsa_key, ec_key):
                token = sign(issuer.claims(sub=f"user-{key.kid}"), key)
                r = client.get("/api/v1/models", headers={"Authorization": f"Bearer {token}"})
                assert r.status_code == 200, r.json()
            # OPERATOR may submit but not create data.
            op = sign(issuer.claims(roles=["oran-operator"]), rsa_key)
            r = client.post("/api/v1/datasets", json={"dataset_id": "kpi"},
                            headers={"Authorization": f"Bearer {op}"})
            assert r.status_code == 403
            assert client.get("/api/v1/models").status_code == 401  # no token
        # The most privileged mapped role wins; the name comes from AUTH_NAME_CLAIM.
        auth = OIDC.factory(settings)
        who = auth.authenticate({"Authorization": "Bearer " + sign(
            issuer.claims(roles=["oran-viewer", "oran-admin", "unmapped"]), rsa_key)})
        assert who.role is Role.ADMIN and who.name == "alice"
        nested = OIDC.factory(settings.model_copy(update={"auth_role_claim": "realm.roles"}))
        who = nested.authenticate({"Authorization": "Bearer " + sign(
            issuer.claims(roles=None, realm={"roles": ["oran-ml"]}), rsa_key)})
        assert who.role is Role.ML_ENGINEER
        assert issuer.jwks_hits <= 3  # cached, not fetched per request


def _forgeries(issuer, key: SigningKey, other: SigningKey) -> dict[str, str]:
    now = int(time.time())
    claims = issuer.claims()
    no_exp = dict(claims)
    no_exp.pop("exp")
    return {
        "expired": sign(issuer.claims(exp=now - 3600, iat=now - 7200, nbf=now - 7200), key),
        "wrong audience": sign(issuer.claims(aud="someone-else"), key),
        "wrong issuer": sign(issuer.claims(iss="https://evil.example"), key),
        "not yet valid": sign(issuer.claims(nbf=now + 3600), key),
        "issued in the future": sign(issuer.claims(iat=now + 3600), key),
        "no exp": sign(no_exp, key),
        "signed by another key with the same kid": sign(claims, SigningKey(key.kid, key.alg,
                                                                             other.private)),
        "unknown kid": sign(claims, other),
        "alg none": sign(claims, None, alg="none", kid=key.kid),
        "HS256 with the public key as secret": sign(claims, None, alg="HS256", kid=key.kid,
                                                    hmac_secret=key.public_pem()),
        "alg not allowed (RS512)": sign(claims, key, alg="RS512"),
        "tampered payload": _tamper(sign(claims, key)),
        "malformed": "not.a-token",
    }


def _tamper(token: str) -> str:
    header, payload, sig = token.split(".")
    return f"{header}.{payload[:-2]}AA.{sig}"


def test_oidc_rejects_forged_expired_and_misdirected_tokens(migrated_settings) -> None:
    key, other = SigningKey.rsa("r1"), SigningKey.rsa("r2")
    with local_issuer(key) as issuer:
        auth = OIDC.factory(_oidc_settings(migrated_settings, issuer.url,
                                           auth_jwt_leeway_s=5, auth_jwks_min_refetch_s=0))
        for name, token in _forgeries(issuer, key, other).items():
            with pytest.raises(AuthenticationError) as info:
                auth.authenticate({"Authorization": f"Bearer {token}"})
            assert token not in str(info.value.to_dict()), name  # never echoed
        with pytest.raises(PermissionDeniedError):
            auth.authenticate({"Authorization": "Bearer " + sign(issuer.claims(roles=["x"]),
                                                                 key)})


def test_oidc_key_rotation_refetches_once_and_forged_kids_cannot_hammer_the_issuer(
    migrated_settings,
) -> None:
    old, new = SigningKey.rsa("old"), SigningKey.rsa("new")
    with local_issuer(old) as issuer:
        auth = OIDC.factory(_oidc_settings(migrated_settings, issuer.url,
                                           auth_jwks_min_refetch_s=0))
        auth.authenticate({"Authorization": "Bearer " + sign(issuer.claims(), old)})
        issuer.keys = [new]  # the issuer rotates
        who = auth.authenticate({"Authorization": "Bearer " + sign(issuer.claims(), new)})
        assert who.role is Role.OPERATOR and issuer.jwks_hits == 2

        throttled = OIDC.factory(_oidc_settings(migrated_settings, issuer.url,
                                                auth_jwks_min_refetch_s=300))
        throttled.authenticate({"Authorization": "Bearer " + sign(issuer.claims(), new)})
        hits = issuer.jwks_hits
        for i in range(5):
            with pytest.raises(AuthenticationError):
                throttled.authenticate({"Authorization": "Bearer " + sign(
                    issuer.claims(), SigningKey.rsa(f"forged-{i}"))})
        assert issuer.jwks_hits == hits  # no refetch inside AUTH_JWKS_MIN_REFETCH_S


def test_oidc_discovery_cannot_point_the_key_fetch_inward(migrated_settings) -> None:
    key = SigningKey.rsa("r1")
    with local_issuer(key) as issuer:
        issuer.discovery_jwks_uri = "http://169.254.169.254/latest/meta-data/jwks"
        auth = OIDC.factory(_oidc_settings(migrated_settings, issuer.url))
        with pytest.raises(AuthenticationError) as info:
            auth.authenticate({"Authorization": "Bearer " + sign(issuer.claims(), key)})
    assert info.value.context["cause"] == "OutboundBlockedError"
    assert issuer.jwks_hits == 0


# ---- gateway assertions and forwarded mTLS certificates ----------------------------------------
def test_gateway_assertions_need_a_trusted_proxy_and_a_listed_issuer(migrated_settings) -> None:
    key = SigningKey.rsa("gw")
    with local_issuer(key) as issuer:
        settings = migrated_settings.model_copy(update={
            "auth_enabled": True, "auth_backend": "gateway",
            "auth_gateway_issuers": {issuer.url: f"{issuer.url}/jwks"},
            "auth_trusted_proxies": ["10.0.0.0/24"], "auth_role_map": ROLE_MAP,
        })
        assertion = sign(issuer.claims(aud=None), key)
        headers = {"X-Jwt-Assertion": assertion}
        with TestClient(create_app(settings), client=("10.0.0.5", 50000)) as proxied:
            assert proxied.get("/api/v1/models", headers=headers).status_code == 200
            other = sign({**issuer.claims(aud=None), "iss": "https://other.example"}, key)
            assert proxied.get("/api/v1/models",
                               headers={"X-Jwt-Assertion": other}).status_code == 401
        with TestClient(create_app(settings), client=("192.0.2.10", 50000)) as direct:
            r = direct.get("/api/v1/models", headers=headers)
        assert r.status_code == 401 and "AUTH_TRUSTED_PROXIES" in r.json()["message"]
        gateway = GATEWAY.factory(settings)
        with pytest.raises(AuthenticationError):
            gateway.authenticate(headers, peer=None)


def test_mtls_accepts_forwarded_certificates_from_the_trusted_proxy_only(
    migrated_settings, tmp_path,
) -> None:
    ca, rogue = make_ca("oran-ca"), make_ca("rogue-ca")
    ca_file = tmp_path / "ca.pem"
    ca_file.write_bytes(ca.pem())
    settings = migrated_settings.model_copy(update={
        "auth_enabled": True, "auth_backend": "mtls", "auth_mtls_ca_file": str(ca_file),
        "auth_mtls_identities": {"noc-client": "OPERATOR", "svc.oran.local": "ML_ENGINEER"},
        "auth_trusted_proxies": ["10.0.0.0/24"],
    })
    auth = MTLS.factory(settings)
    good = client_cert(ca, "noc-client")
    for envoy in (False, True):
        who = auth.authenticate({"X-Forwarded-Client-Cert": forwarded(good, envoy=envoy)},
                                peer="10.0.0.9")
        assert who.name == "noc-client" and who.role is Role.OPERATOR
    san = client_cert(ca, "unlisted-cn", dns="svc.oran.local")
    assert auth.authenticate({"X-Forwarded-Client-Cert": forwarded(san)},
                             peer="10.0.0.9").role is Role.ML_ENGINEER
    refused = {
        "untrusted peer": ({"X-Forwarded-Client-Cert": forwarded(good)}, "192.0.2.1"),
        "other CA": ({"X-Forwarded-Client-Cert": forwarded(client_cert(rogue, "noc-client"))},
                     "10.0.0.9"),
        "expired": ({"X-Forwarded-Client-Cert": forwarded(
            client_cert(ca, "noc-client", expired=True))}, "10.0.0.9"),
        "unknown identity": ({"X-Forwarded-Client-Cert": forwarded(
            client_cert(ca, "stranger"))}, "10.0.0.9"),
        "garbage": ({"X-Forwarded-Client-Cert": "not-a-cert"}, "10.0.0.9"),
        "missing": ({}, "10.0.0.9"),
    }
    for name, (headers, peer) in refused.items():
        with pytest.raises(AuthenticationError):
            auth.authenticate(headers, peer=peer)
        assert name
    with TestClient(create_app(settings), client=("10.0.0.9", 443)) as client:
        ok = client.get("/api/v1/models",
                        headers={"X-Forwarded-Client-Cert": forwarded(good)})
        assert ok.status_code == 200


# ---- outbound policy: SSRF and TLS -------------------------------------------------------------
BLOCKED = [
    "http://127.0.0.1/", "http://127.1.2.3:8080/x", "http://10.0.0.1/", "http://172.16.5.4/",
    "http://192.168.1.1/", "http://169.254.169.254/latest/meta-data/", "http://[::1]/",
    "http://[fd00::1]/", "http://[fe80::1]/", "http://[::ffff:127.0.0.1]/",
    "http://100.64.0.1/", "http://0.0.0.0/", "http://224.0.0.1/", "http://localhost:5000/",
    "http://api.localhost/", "http://metadata.google.internal/computeMetadata/v1/",
    "file:///etc/passwd", "gopher://10.0.0.1/", "ftp://example.com/",
]


def _policy(**extra) -> OutboundPolicy:
    options = {"outbound_resolve_hosts": False, **extra}
    return OutboundPolicy.from_settings(Settings(_env_file=None, **options))


@pytest.mark.smoke
def test_ssrf_rejects_non_public_destinations_by_default() -> None:
    policy = _policy()
    for url in BLOCKED:
        with pytest.raises(OutboundBlockedError) as info:
            policy.check(url)
        assert "meta-data" not in str(info.value.to_dict())  # paths/queries never echoed
    for url in ("https://8.8.8.8/", "https://example.com/data.parquet",
                "https://[2001:4860:4860::8888]/"):
        policy.check(url)


@pytest.mark.smoke
def test_ssrf_checks_what_a_name_resolves_to(monkeypatch) -> None:
    answers = {"inward.example": "10.1.2.3", "public.example": "93.184.215.14",
               "rebind.example": "::ffff:169.254.169.254"}

    def fake_getaddrinfo(host, *args, **kwargs):
        if host not in answers:
            raise socket.gaierror("unknown")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (answers[host], 0))]

    monkeypatch.setattr("oran_adapt.core.outbound.socket.getaddrinfo", fake_getaddrinfo)
    policy = _policy(outbound_resolve_hosts=True)
    for host in ("inward.example", "rebind.example"):
        with pytest.raises(OutboundBlockedError):
            policy.check(f"https://{host}/")
    policy.check("https://public.example/")
    policy.check("https://does-not-resolve.example/")  # fails later, on connect


@pytest.mark.smoke
def test_ssrf_allowlist_and_configured_endpoints_are_trusted(monkeypatch) -> None:
    # Every name resolves inward, so only an allowlisted name may pass.
    monkeypatch.setattr("oran_adapt.core.outbound.socket.getaddrinfo",
                        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                                          ("10.1.1.1", 0))])
    policy = _policy(outbound_resolve_hosts=True,
                     outbound_allowlist=["mlflow.internal", ".svc.cluster.local", "10.20.0.0/16"],
                     triton_url="http://127.0.0.1:8000",
                     dataset_http_allowed_hosts=["192.168.7.7:9000"],
                     auth_gateway_issuers={"https://idp.internal": "http://10.9.9.9/jwks"})
    for url in ("http://mlflow.internal:5000/", "http://kserve.models.svc.cluster.local/v1",
                "http://10.20.3.4/", "http://127.0.0.1:8000/v2/health/ready",
                "https://192.168.7.7:9000/data.csv", "http://10.9.9.9/jwks",
                "https://idp.internal/.well-known/openid-configuration"):
        policy.check(url)
    for url in ("http://10.21.0.1/", "http://127.0.0.2/", "http://evil-mlflow.internal.io/"):
        with pytest.raises(OutboundBlockedError):
            policy.check(url)


@pytest.mark.smoke
def test_plain_http_is_refused_in_production_except_to_configured_http_endpoints() -> None:
    dev = _policy()
    assert not dev.require_https
    prod = _policy(outbound_require_https=True, triton_url="http://triton.serving:8000")
    prod.check("http://triton.serving:8000/v2/models")
    with pytest.raises(OutboundBlockedError, match="plain http"):
        prod.check("http://8.8.8.8/")
    prod.check("https://8.8.8.8/")


@pytest.mark.smoke
def test_blocked_requests_are_never_sent_and_redirects_are_not_followed() -> None:
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})

    client = _policy().client(timeout=5, transport=httpx.MockTransport(handler))
    with pytest.raises(OutboundBlockedError):
        client.get("http://169.254.169.254/latest/meta-data/iam/security-credentials/")
    assert sent == []
    r = client.get("https://8.8.8.8/start")
    assert r.status_code == 302 and sent == ["https://8.8.8.8/start"]  # not followed


@pytest.mark.smoke
def test_tls_is_always_verified_with_a_minimum_version() -> None:
    context = _policy().ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    strict = _policy(outbound_tls_min_version="TLSv1.3").ssl_context()
    assert strict.minimum_version == ssl.TLSVersion.TLSv1_3
    client = outbound_client(Settings(_env_file=None), timeout=3)
    assert client.follow_redirects is False and client.event_hooks["request"]


def _calls(tree: ast.AST, name: str) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)
            and ast.unparse(n.func) in (name, f"httpx.{name}")]


@pytest.mark.smoke
def test_every_http_client_goes_through_the_outbound_policy() -> None:
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        text = ast.unparse(tree)
        if rel != "core/outbound.py":
            offenders += [f"{rel}:{c.lineno} httpx.Client" for c in _calls(tree, "Client")]
            offenders += [f"{rel}:{c.lineno} httpx.AsyncClient"
                          for c in _calls(tree, "AsyncClient")]
            if "partial(httpx.Client" in text:
                offenders.append(f"{rel}: partial(httpx.Client, ...)")
        for node in ast.walk(tree):
            if (isinstance(node, ast.keyword) and node.arg == "verify"
                    and isinstance(node.value, ast.Constant) and node.value.value is False):
                offenders.append(f"{rel}:{node.value.lineno} verify=False")
    assert not offenders, offenders


def test_adapters_get_policy_checked_clients(settings) -> None:
    from oran_adapt.adapters.deployment.kubernetes import kube_api
    from oran_adapt.adapters.notify import http_client

    checked = settings.model_copy(update={"k8s_api_url": "https://k8s.example:6443"})
    for client in (http_client(checked), kube_api(checked).client):
        assert client.event_hooks["request"], client
        assert client.follow_redirects is False
        with pytest.raises(OutboundBlockedError):
            client.get("http://169.254.169.254/")


# ---- rate limits -------------------------------------------------------------------------------
@pytest.mark.smoke
def test_token_buckets_refill_and_stay_bounded() -> None:
    now = [0.0]
    buckets = TokenBuckets(60, 2, max_keys=3, clock=lambda: now[0])
    assert buckets.take("a") == 0 and buckets.take("a") == 0
    assert buckets.take("a") == pytest.approx(1.0)
    now[0] += 1.0
    assert buckets.take("a") == 0
    for key in "bcdef":
        buckets.take(key)
    assert len(buckets._buckets) == 3
    assert TokenBuckets(0, 5, max_keys=3).take("x") == 0  # 0 turns the limit off


def test_api_rate_limit_answers_429_with_retry_after(migrated_settings) -> None:
    settings = _keyed(migrated_settings, api_rate_limit_per_minute=60, api_rate_limit_burst=3)
    headers = {"X-API-Key": KEYS[Role.READ_ONLY][0]}
    with TestClient(create_app(settings)) as client:
        codes = [client.get("/api/v1/models", headers=headers).status_code for _ in range(4)]
        assert codes == [200, 200, 200, 429]
        r = client.get("/api/v1/models", headers=headers)
        assert r.json()["code"] == "RATE_LIMITED" and int(r.headers["Retry-After"]) >= 1
        # Another caller has its own bucket; health is never limited.
        assert client.get("/api/v1/models",
                          headers={"X-API-Key": KEYS[Role.ADMIN][0]}).status_code == 200
        assert client.get("/api/v1/health").status_code == 200


def test_repeated_authentication_failures_lock_out_the_address(migrated_settings) -> None:
    settings = _keyed(migrated_settings, api_auth_failure_limit_per_minute=3)
    with TestClient(create_app(settings), client=("198.51.100.7", 1)) as client:
        codes = [client.get("/api/v1/models", headers={"X-API-Key": f"guess-{i:012d}"}
                            ).status_code for i in range(4)]
        assert codes == [401, 401, 401, 429]
        # Even the right key is refused until the bucket refills: guessing is slowed down.
        r = client.get("/api/v1/models", headers={"X-API-Key": KEYS[Role.ADMIN][0]})
        assert r.status_code == 429 and "Retry-After" in r.headers
    with TestClient(create_app(settings), client=("198.51.100.8", 1)) as other:
        assert other.get("/api/v1/models",
                         headers={"X-API-Key": KEYS[Role.ADMIN][0]}).status_code == 200


# ---- secrets -----------------------------------------------------------------------------------
def test_vault_secrets_feed_secret_typed_settings(tmp_path, monkeypatch) -> None:
    token_file = tmp_path / "vault-token"
    token_file.write_text("s.local-test-token\n", encoding="utf-8")
    monkeypatch.delenv("DATASET_HTTP_TOKEN", raising=False)
    with local_vault("s.local-test-token", {"dataset_http_token": "from-vault-123"}) as url:
        loaded = Settings(_env_file=None, secrets_backend="vault", secrets_vault_url=url,
                          secrets_vault_token_file=str(token_file))
        assert loaded.dataset_http_token is not None
        assert loaded.dataset_http_token.get_secret_value() == "from-vault-123"
        store = VAULT.factory(loaded)
        assert store.get("DATASET_HTTP_TOKEN") == "from-vault-123"
        assert store.get("missing") is None
        token_file.write_text("s.wrong", encoding="utf-8")
        with pytest.raises(ConfigurationError, match="refused the token"):
            VAULT.factory(loaded).get("dataset_http_token")
    with pytest.raises(ConfigurationError, match="SECRETS_VAULT_TOKEN_FILE"):
        VAULT.factory(Settings(_env_file=None, secrets_vault_url="https://vault.example"))


def test_secrets_do_not_reach_logs_errors_or_the_effective_config(
    migrated_settings, caplog,
) -> None:
    sentinel = LEAK_SENTINEL
    settings = _keyed(migrated_settings, dataset_http_token=sentinel,
                      anthropic_api_key=sentinel, log_level="DEBUG")
    caplog.set_level(logging.DEBUG)
    with TestClient(create_app(settings)) as client:
        admin = {"X-API-Key": KEYS[Role.ADMIN][0]}
        bodies = [client.get("/api/v1/config/effective", headers=admin).text,
                  client.get("/api/v1/models", headers={"X-API-Key": sentinel}).text,
                  client.post("/api/v1/adaptation/events", headers=admin,
                              json={"model_id": "no-such-model"}).text]
    for text in [*bodies, caplog.text]:
        assert sentinel not in text
    assert KEYS[Role.ADMIN][0] not in caplog.text


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_secret_scan_is_clean_and_catches_a_planted_secret(tmp_path) -> None:
    scan = _load_script("secret_scan")
    assert scan.scan_paths(scan.tracked_files(ROOT)) == []
    planted = tmp_path / "leak.log"
    fake_key = "AKIA" + "Q" * 16
    header = "-----BEGIN " + "RSA PRIVATE KEY-----"
    planted.write_text(f"x={fake_key}\n{header}\nabc\n", encoding="utf-8")
    kinds = {f.kind for f in scan.scan_paths([planted])}
    assert {"aws-access-key-id", "private-key"} <= kinds
    assert scan.scan_paths([planted], values=["abc"])  # configured secret values too


# ---- supply chain ------------------------------------------------------------------------------
@pytest.mark.smoke
def test_images_run_as_non_root_and_runtime_dependencies_are_pinned() -> None:
    for dockerfile in (ROOT / "Dockerfile", ROOT / "docker/mlflow/Dockerfile",
                       ROOT / "docker/sandbox/Dockerfile"):
        users = re.findall(r"^USER\s+(\S+)", dockerfile.read_text(encoding="utf-8"), re.MULTILINE)
        assert users and users[-1] not in ("root", "0"), dockerfile
    lock = (ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines()
    pins = [line for line in lock if line.strip() and not line.startswith("#")]
    # An exact pin, optionally with an environment marker (``; sys_platform == "win32"``).
    unpinned = [p for p in pins
                if not re.fullmatch(r"[A-Za-z0-9_.\-\[\]]+==[^\s;]+(\s*;.*)?", p)]
    assert pins and not unpinned, unpinned
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "pip-audit" in ci and "cyclonedx" in ci


# ---- conformance: every auth and secrets adapter, and the template ----------------------------
def _roles_map_claims(role: Role) -> list[str]:
    return [name for name, mapped in ROLE_MAP.items() if mapped == role.value]


def test_auth_adapters_pass_the_conformance_suite(migrated_settings, tmp_path) -> None:
    api_key = conformance.AuthContext(
        credentials=lambda role: {"X-API-Key": KEYS[role][0]},
        forgeries={"unknown key": {"X-API-Key": "not-a-real-key-0123456789"},
                   "wrong scheme": {"Authorization": f"Basic {KEYS[Role.ADMIN][0]}"}},
    )
    assert conformance.run_auth(API_KEY.factory(_keyed(migrated_settings)), api_key) == list(conformance.AUTH_CHECKS)

    key, other = SigningKey.rsa("c1"), SigningKey.rsa("c2")
    with local_issuer(key) as issuer:
        oidc = OIDC.factory(_oidc_settings(migrated_settings, issuer.url))
        bearer = {name: {"Authorization": f"Bearer {token}"}
                  for name, token in _forgeries(issuer, key, other).items()}
        conformance.run_auth(oidc, conformance.AuthContext(
            credentials=lambda role: {"Authorization": "Bearer " + sign(
                issuer.claims(roles=_roles_map_claims(role)), key)},
            forgeries=bearer))

        gateway = GATEWAY.factory(migrated_settings.model_copy(update={
            "auth_gateway_issuers": {issuer.url: f"{issuer.url}/jwks"},
            "auth_trusted_proxies": ["10.0.0.0/24"], "auth_role_map": ROLE_MAP}))
        conformance.run_auth(gateway, conformance.AuthContext(
            credentials=lambda role: {"X-Jwt-Assertion": sign(
                issuer.claims(aud=None, roles=_roles_map_claims(role)), key)},
            forgeries={"other key": {"X-Jwt-Assertion": sign(issuer.claims(aud=None), other)}},
            peer="10.0.0.5"))

    ca, rogue = make_ca("conf-ca"), make_ca("conf-rogue")
    ca_file = tmp_path / "ca.pem"
    ca_file.write_bytes(ca.pem())
    mtls = MTLS.factory(migrated_settings.model_copy(update={
        "auth_mtls_ca_file": str(ca_file), "auth_trusted_proxies": ["10.0.0.0/24"],
        "auth_mtls_identities": {f"client-{r.value.lower()}": r.value for r in Role}}))
    conformance.run_auth(mtls, conformance.AuthContext(
        credentials=lambda role: {"X-Forwarded-Client-Cert": forwarded(
            client_cert(ca, f"client-{role.value.lower()}"))},
        forgeries={"other CA": {"X-Forwarded-Client-Cert": forwarded(
            client_cert(rogue, "client-admin"))}},
        peer="10.0.0.9"))


def test_secrets_adapters_pass_the_conformance_suite(tmp_path, monkeypatch) -> None:
    known = {"dataset_http_token": "conformance-secret-42"}
    ctx = conformance.SecretsContext(known=known)
    monkeypatch.setenv("DATASET_HTTP_TOKEN", known["dataset_http_token"])
    monkeypatch.delenv("CONFORMANCE_ABSENT_SECRET", raising=False)
    conformance.run_secrets(SECRETS_ENV.factory(Settings(_env_file=None)), ctx)
    (tmp_path / "dataset_http_token").write_text(known["dataset_http_token"] + "\n",
                                                encoding="utf-8")
    conformance.run_secrets(SECRETS_FILE.factory(Settings(_env_file=None,
                                                          secrets_dir=str(tmp_path))), ctx)
    token_file = tmp_path / "vault-token"
    token_file.write_text("s.conformance", encoding="utf-8")
    with local_vault("s.conformance", known) as url:
        vault = VAULT.factory(Settings(_env_file=None, secrets_vault_url=url,
                                       secrets_vault_token_file=str(token_file)))
        assert conformance.run_secrets(vault, ctx) == list(conformance.SECRETS_CHECKS)


def test_the_auth_template_is_conformant(tmp_path, monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "templates" / "auth-adapter"))
    for name in ("adapter", "secrets_adapter"):
        sys.modules.pop(name, None)
    import adapter  # the template modules
    import secrets_adapter

    tokens = {role: f"{role.value.lower()}-token-0123456789" for role in Role}
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({adapter.digest(t): f"{r.value}:{r.value.lower()}"
                                for r, t in tokens.items()}), encoding="utf-8")
    conformance.run_auth(adapter.TokenFileAuth(str(path)), conformance.AuthContext(
        credentials=lambda role: {"Authorization": f"Bearer {tokens[role]}"},
        forgeries={"unknown token": {"Authorization": "Bearer not-a-real-token-123"}}))
    store = tmp_path / "secrets.json"
    store.write_text(json.dumps({"dataset_http_token": "s3cr3t-value"}), encoding="utf-8")
    conformance.run_secrets(secrets_adapter.JsonFileSecrets(str(store)),
                            conformance.SecretsContext(known={"dataset_http_token": "s3cr3t-value"}))
    assert adapter.SPEC.capability.port == "auth"
    assert secrets_adapter.SPEC.capability.port == "secrets"
    for name in ("adapter", "secrets_adapter"):
        sys.modules.pop(name, None)


def test_conformance_catches_an_adapter_that_echoes_the_credential() -> None:
    class Leaky:
        def authenticate(self, headers, *, peer=None):
            value = next(iter(headers.values()), "")
            if value == "good-credential-123":
                return Principal(name="x", role=Role.ADMIN)
            raise AuthenticationError(f"invalid credential {value}")

    ctx = conformance.AuthContext(credentials=lambda role: {"X-Key": "good-credential-123"},
                                  forgeries={"bad": {"X-Key": "bad-credential-456"}},
                                  roles=(Role.ADMIN,))
    conformance.check_forgeries_refused(Leaky(), ctx)
    with pytest.raises(ConformanceFailure, match="repeats the presented credential"):
        conformance.check_errors_do_not_echo_credentials(Leaky(), ctx)
