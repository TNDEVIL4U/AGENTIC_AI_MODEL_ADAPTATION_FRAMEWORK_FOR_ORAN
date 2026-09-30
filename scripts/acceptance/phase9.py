"""Hardening Phase 9 acceptance: security.

Run by scripts/verify.sh 9 after lint, the import-boundary test and the scoped tests.

1. Route enumeration: every route the API serves declares a policy, a route without one stops
   startup, every role gets exactly what POLICY_ROLES grants (deny by default), and
   docs/security/authz-matrix.md matches the routes served.
2. OIDC against a local issuer: valid RS256 and ES256 tokens are accepted with the mapped role;
   forged, expired, misdirected, ``alg: none``, HMAC-confusion and tampered tokens are refused;
   key rotation refetches once; discovery cannot point the key fetch inward. The gateway and
   mTLS adapters are checked the same way.
3. SSRF suite: non-public destinations, names that resolve inward, redirects, plain HTTP in
   production and unverified TLS are refused before anything is sent, and every HTTP client in
   the source goes through the outbound policy.
4. Secret-leak scan: the tracked tree is clean, and the log of a run configured with a sentinel
   secret (driven through the API, errors and the effective config) is scanned for the sentinel
   and every credential shape.
5. Rate limits, security headers, the append-only audit log, Vault, the auth/secrets
   conformance suites and the supply-chain checks (non-root images, pinned lock, CI scans).

CVE scanning (pip-audit) and the SBOM run in CI only: unverified locally.
Exit status 0 means every check passed.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests" / "unit"
sys.path.insert(0, str(TESTS))  # the phase 9 test doubles

PHASE9 = TESTS / "test_phase9_security.py"


def _run(selection: str, *extra: str, target: Path = PHASE9) -> None:
    import pytest

    code = pytest.main(["-q", "-p", "no:cacheprovider", "-W", "ignore", *extra, str(target),
                        "-k", selection])
    # Apps built inside those tests pointed the root log handler at pytest's capture stream,
    # which is closed now; drop it so this process can keep logging.
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(getattr(handler, "stream", None), "closed", False):
            root.removeHandler(handler)
    assert code == 0, f"pytest -k {selection!r} failed with exit code {code}"


def route_enumeration(tmp: Path) -> str:
    from oran_adapt.api.app import create_app
    from oran_adapt.api.security import route_policies
    from oran_adapt.core.config import Settings

    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{(tmp / 'app.db').as_posix()}",
        mlflow_tracking_uri=f"sqlite:///{(tmp / 'mlflow.db').as_posix()}",
        artifact_workdir=str(tmp / "work"),
        notification_dispatch_enabled=False,
        api_docs_enabled=False,
    )
    policies = route_policies(create_app(settings))
    missing = [p.path for p in policies if not p.policy]
    assert policies and not missing, f"routes without a policy: {missing}"
    public = sorted(p.path for p in policies if p.policy == ("public",))
    _run("every_route_has or without_a_policy or authz_matrix or missing_from_the_policy "
         "or document_is_current or docs_are_off")
    return (f"{len(policies)} routes, each with a policy; public: {', '.join(public)}; "
            "matrix per role enforced and docs/security/authz-matrix.md current")


def oidc_local_issuer(tmp: Path) -> str:
    from security_doubles import SigningKey, local_issuer, sign

    from oran_adapt.adapters.auth import OIDC
    from oran_adapt.core.config import Settings
    from oran_adapt.core.enums import Role
    from oran_adapt.core.errors import AuthenticationError

    key, other = SigningKey.rsa("accept"), SigningKey.rsa("forger")
    with local_issuer(key) as issuer:
        auth = OIDC.factory(Settings(
            _env_file=None, auth_oidc_issuer=issuer.url, auth_oidc_audience="oran-adapt",
            auth_role_map={"oran-operator": "OPERATOR"}, outbound_resolve_hosts=False))
        who = auth.authenticate({"Authorization": "Bearer " + sign(issuer.claims(), key)})
        assert who.role is Role.OPERATOR, who
        try:
            auth.authenticate({"Authorization": "Bearer " + sign(issuer.claims(), other)})
        except AuthenticationError:
            pass
        else:
            raise AssertionError("a token signed by another key was accepted")
    _run("oidc or gateway or mtls")
    return ("local issuer: valid RS256/ES256 accepted with the mapped role; every forgery "
            "refused; rotation, discovery, gateway and mTLS checked")


def ssrf_suite(tmp: Path) -> str:
    from oran_adapt.core.config import Settings
    from oran_adapt.core.errors import OutboundBlockedError
    from oran_adapt.core.outbound import OutboundPolicy

    policy = OutboundPolicy.from_settings(Settings(_env_file=None, outbound_resolve_hosts=False))
    for url in ("http://169.254.169.254/latest/meta-data/", "http://127.0.0.1:5000/",
                "http://[::ffff:10.0.0.1]/", "http://metadata.google.internal/"):
        try:
            policy.check(url)
        except OutboundBlockedError:
            continue
        raise AssertionError(f"{url} was not refused")
    _run("ssrf or blocked_requests or plain_http or tls_is_always or every_http_client "
         "or policy_checked")
    return ("metadata, loopback, private, mapped-IPv6 and resolved-inward destinations refused; "
            "redirects not followed; TLS always verified; no client bypasses the policy")


def _script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def secret_leak_scan(tmp: Path) -> str:
    from test_phase9_security import LEAK_SENTINEL

    scan = _script("secret_scan")
    tracked = scan.tracked_files(ROOT)
    found = scan.scan_paths(tracked)
    assert not found, f"credentials in tracked files: {[str(f) for f in found]}"
    log = tmp / "leak-test.log"
    _run("secrets_do_not_reach_logs", f"--log-file={log}", "--log-file-level=DEBUG")
    assert log.is_file() and log.stat().st_size > 0, "the leak test wrote no log"
    leaks = scan.scan_paths([log], values=[LEAK_SENTINEL])
    assert not leaks, f"secret material in the captured log: {[f.kind for f in leaks]}"
    lines = sum(1 for _ in log.open(encoding="utf-8"))
    _run("secret_scan")
    return (f"{len(tracked)} tracked files clean; captured DEBUG log ({lines} lines) of a run "
            "with a sentinel secret holds neither the sentinel nor any credential shape")


def limits_audit_supply_chain(tmp: Path) -> str:
    _run("rate_limit or lock_out or token_buckets or vault or conformance or conformant "
         "or non_root")
    _run("test_audit_log_is_append_only", target=TESTS / "test_phase14_stage_b.py")
    return ("429 with Retry-After and auth-failure lockout; Vault KV v2; auth and secrets "
            "conformance (4 auth, 3 secrets adapters, template); audit_log append-only in ORM "
            "and database; non-root images, pinned lock; pip-audit/SBOM in CI (unverified "
            "locally)")


CHECKS: list[tuple[str, Callable[[Path], str]]] = [
    ("route enumeration and deny-by-default authorization", route_enumeration),
    ("OIDC accept/reject against a local issuer", oidc_local_issuer),
    ("SSRF suite", ssrf_suite),
    ("secret-leak scan: tree and captured logs", secret_leak_scan),
    ("rate limits, audit, secrets adapters, supply chain", limits_audit_supply_chain),
]


def main() -> int:
    failed = 0
    for name, check in CHECKS:
        with tempfile.TemporaryDirectory(prefix="oran-phase9-", ignore_cleanup_errors=True) as tmp:
            started = time.monotonic()
            try:
                detail = check(Path(tmp))
            except AssertionError as exc:
                failed += 1
                print(f"FAIL  {name}\n      {exc}", flush=True)
            else:
                print(f"PASS  {name} ({time.monotonic() - started:.0f}s)\n      {detail}",
                      flush=True)
    print(f"\nphase 9 acceptance: {len(CHECKS) - failed}/{len(CHECKS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
