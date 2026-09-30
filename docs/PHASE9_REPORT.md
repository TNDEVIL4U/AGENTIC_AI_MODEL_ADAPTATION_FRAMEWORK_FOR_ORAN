# Hardening Phase 9 report: security

Branch `phase14-production-hardening`. Everything marked "passed" below was run locally on
Windows 11 with Python 3.13.7, CPU only, on 2026-09-30. CI was not polled. What ran for real
and what used doubles:
- **Ran for real:**
  - the API with every route policy, the rate limits and the security headers (FastAPI
    `TestClient`);
  - JWT verification (RS256/ES256 with `cryptography`);
  - certificate chains and forwarded client certificates;
  - the outbound policy and the secret scan.
- **Local doubles:**
  - the OIDC issuer (discovery document and JWKS) is a real HTTP server on 127.0.0.1 in
    `tests/unit/security_doubles.py`, and it signs with real keys;
  - the Vault server is an equally small local HTTP server that speaks the KV v2 read API;
  - DNS answers for the rebinding and resolve-inward tests are monkeypatched.

**Unverified locally:**
- a real identity provider (Keycloak, Entra ID, Okta), a real Vault or OpenBao, and a real
  Envoy/nginx forwarding client certificates;
- `pip-audit` (CVEs), `bandit` and the CycloneDX SBOM, which run in the CI `supply-chain` job
  only;
- building the images, since Docker is not installed.

## 1. Findings closed

| Finding | Closed by |
|---|---|
| Finding 9: API keys are the only identity | `AuthPort` gains three adapters beside `api-key` (`adapters/auth.py`): `oidc` (bearer JWTs verified against the issuer's JWKS, found by discovery or `AUTH_OIDC_JWKS_URL`), `gateway` (a signed assertion from an API gateway, believed only from `AUTH_TRUSTED_PROXIES` and only for an issuer in `AUTH_GATEWAY_ISSUERS`) and `mtls` (a client certificate forwarded by the TLS terminator, verified against `AUTH_MTLS_CA_FILE`, identity → role via `AUTH_MTLS_IDENTITIES`). JWT verification (`adapters/jwt.py`) is built on `cryptography`: algorithm allowlist, never `none` or HMAC, `kid` rotation with a refetch floor, and `iss`/`aud`/`exp`/`nbf`/`iat` with leeway |
| Role checks attached per route; a new route inherits whatever its router has | Deny by default. Every route must declare `Depends(public)` or `Depends(require(action))`. `enforce_route_policies` refuses to start the API otherwise, in `create_app` and again at startup, so plugin routes added later are covered. `route_policies` enumerates the routes as served, through FastAPI's `iter_route_contexts`, including router-level dependencies. An action missing from `POLICY_ROLES` is allowed to nobody. The generated `docs/security/authz-matrix.md` (38 routes × 4 roles) is checked against the app by a test |
| Secrets from the environment or `.env` only | `SecretsPort` gains `vault` (`adapters/vault.py`: HashiCorp Vault / OpenBao KV v2, token from a file, namespace header, through the outbound policy). The secrets source now asks the selected adapter for its own `config_keys` |
| No egress control or SSRF checks | `core/outbound.py`: every HTTP client the framework builds is policy-checked. Covered: deployment (gitops, triton, vertex, webhook, kubernetes), registry (vertex), datasets (http, cloud), notifications, rollout metrics, JWKS/discovery and Vault. The policy allows http/https only and refuses non-public destinations (also after resolution) unless allowlisted or configured. It never follows redirects, always verifies TLS, sets a minimum TLS version, and requires HTTPS in production. An AST test fails the build if anything outside `core/outbound.py` constructs an `httpx` client or passes `verify=False` |
| No rate limits | `api/ratelimit.py`: token buckets per principal (`API_RATE_LIMIT_*`) and per client address for authentication failures (`API_AUTH_FAILURE_LIMIT_PER_MINUTE`), in a bounded LRU. 429 `RATE_LIMITED` with `Retry-After` |
| Missing HTTP hardening | Security headers on every response (nosniff, frame DENY, no-referrer, no-store, CSP, optional HSTS). Interactive docs are off in production unless `API_DOCS_ENABLED` |
| Secrets in the repository or in logs went unchecked | `scripts/secret_scan.py`: credential shapes in tracked files, and `--value` for a configured secret's value in a log. The gate runs it over the tree and over the captured DEBUG log of a run configured with a sentinel secret |
| Supply chain | CI `supply-chain` job: secret scan, bandit, pip-audit, CycloneDX SBOM (uploaded). A test asserts every image ends as a non-root user, every line of `requirements.lock` is an exact pin, and CI runs the scans |
| **New:** two committed test fixtures matched credential shapes (`hunter2` in a URL, an `sk-ant-` string) | marked `secret-scan: allow` as deliberate fixtures |
| **New:** FastAPI 0.141 wraps included routers (`_IncludedRouter`), so walking `app.routes` for `APIRoute`s saw none of the API's routes | the enumeration goes through `fastapi.routing.iter_route_contexts` |

The append-only audit log already existed: ORM guard plus database triggers, migration 0004,
Hardening Phase 14 stage B. It is part of this phase's acceptance. No migration: nothing stored
changes shape.

## 2. Ports and adapters

`AuthPort.authenticate(headers, *, peer=None)` now receives the connection's peer address. The
proxy-trusting adapters need it. `api-key` accepts and ignores it.

| Port | Adapter | Verified |
|---|---|---|
| auth | `api-key` (existing) | local |
| auth | `oidc` | local issuer (real HTTP, real keys) |
| auth | `gateway` | local issuer; peer set by `TestClient(client=...)` |
| auth | `mtls` | real certificates from a local CA; Envoy XFCC and URL-encoded PEM forms |
| secrets | `env`, `file` (existing) | local |
| secrets | `vault` | local KV v2 double |

Extension path:
- `templates/auth-adapter/`: a token-file `AuthPort` adapter and a JSON-document `SecretsPort`
  adapter, both conformant;
- `docs/adapters/auth.md`, plus `docs/security.md` for the whole model;
- the conformance suite `oran_adapt.conformance.auth`:
  - `AUTH_CHECKS`: protocol, accepts every role, stable principal, case-insensitive header
    names, no credential refused, forgeries refused, errors do not echo credentials;
  - `SECRETS_CHECKS`: protocol, read back in either case, absent is None.

## 3. Configuration keys

| Key | Type | Default | Required |
|---|---|---|---|
| `AUTH_BACKEND` | `api-key`\|`oidc`\|`gateway`\|`mtls` (+ plugins) | `api-key` | no |
| `AUTH_OIDC_ISSUER` / `AUTH_OIDC_AUDIENCE` | URL / str | unset | issuer when `oidc` |
| `AUTH_OIDC_JWKS_URL` | URL | unset (discovery) | no |
| `AUTH_GATEWAY_ISSUERS` | dict issuer → JWKS URL | `{}` | when `gateway` |
| `AUTH_GATEWAY_HEADER` / `AUTH_GATEWAY_AUDIENCE` | str / str | `X-Jwt-Assertion` / unset | no |
| `AUTH_JWT_ALGORITHMS` | list (RS/PS/ES 256/384/512) | `["RS256","ES256"]` | no |
| `AUTH_JWT_LEEWAY_S` | float ≥ 0 | 60 | no |
| `AUTH_ROLE_CLAIM` / `AUTH_ROLE_MAP` / `AUTH_NAME_CLAIM` | str / dict / str | `roles` / `{}` / `sub` | map when `oidc`/`gateway` |
| `AUTH_HTTP_TIMEOUT_S` / `AUTH_JWKS_CACHE_TTL_S` / `AUTH_JWKS_MIN_REFETCH_S` | float | 5 / 300 / 30 | no |
| `AUTH_MTLS_CA_FILE` / `AUTH_MTLS_CERT_HEADER` / `AUTH_MTLS_IDENTITIES` | path / str / dict | unset / `X-Forwarded-Client-Cert` / `{}` | CA and identities when `mtls` |
| `AUTH_TRUSTED_PROXIES` | list of IPs/CIDRs | `[]` | when `gateway`/`mtls` |
| `API_RATE_LIMIT_PER_MINUTE` / `API_RATE_LIMIT_BURST` | int | 600 / 100 | no |
| `API_AUTH_FAILURE_LIMIT_PER_MINUTE` / `API_RATE_LIMIT_MAX_KEYS` | int | 30 / 10000 | no |
| `API_DOCS_ENABLED` / `API_HSTS_MAX_AGE_S` | bool / int | unset (off in production) / 0 | no |
| `SECRETS_VAULT_URL` / `SECRETS_VAULT_TOKEN_FILE` | URL / path | unset | both when `vault` |
| `SECRETS_VAULT_MOUNT` / `SECRETS_VAULT_PATH` / `SECRETS_VAULT_NAMESPACE` / `SECRETS_VAULT_TIMEOUT_S` | str / str / str / float | `secret` / `oran-adapt` / unset / 10 | no |
| `OUTBOUND_ALLOWLIST` | list (host, `.suffix`, CIDR) | `[]` | no |
| `OUTBOUND_BLOCKED_HOSTS` | list | localhost and metadata names | no |
| `OUTBOUND_RESOLVE_HOSTS` | bool | true | no |
| `OUTBOUND_REQUIRE_HTTPS` | bool | unset (on in production) | no |
| `OUTBOUND_TLS_MIN_VERSION` / `OUTBOUND_CA_FILE` | `TLSv1.2`\|`TLSv1.3` / path | `TLSv1.2` / unset | no |

All of these keys are documented in `.env.example`, the main ones in `docs/MANUAL.md` §7.

## 4. Acceptance criteria

`scripts/acceptance/phase9.py`, over `tests/unit/test_phase9_security.py` (31 tests) and
`test_phase14_stage_b.py::test_audit_log_is_append_only`:

| Criterion | Evidence | Result |
|---|---|---|
| Route enumeration: every route has an explicit policy | `test_every_route_has_an_explicit_policy`, `test_a_route_without_a_policy_stops_startup`, `test_authz_matrix_is_deny_by_default_for_every_route_and_role` (every route × every role and anonymous), `test_an_action_missing_from_the_policy_is_denied_to_everyone`, `test_the_authz_matrix_document_is_current` | passed |
| OIDC accept/reject against a local issuer | `test_oidc_accepts_valid_tokens_from_the_local_issuer`; `test_oidc_rejects_forged_expired_and_misdirected_tokens`: 13 forgeries (expired, wrong aud/iss, nbf/iat in the future, no exp, same kid other key, unknown kid, `alg: none`, HS256 with the public key, disallowed alg, tampered, malformed); rotation; discovery pointing inward; gateway; mTLS | passed |
| SSRF suite rejects private addresses by default | 19 blocked URLs (loopback, RFC 1918, link-local/metadata, IPv6 loopback/ULA/link-local/mapped, CGNAT, unspecified, multicast, localhost names, non-HTTP schemes); resolve-inward and rebinding answers; allowlist exactness; redirects not followed; blocked requests never sent; plain HTTP in production; TLS always verified; no unchecked client in the source | passed |
| Secret-leak scan clean over source and captured test logs | `secret_scan.py` over every tracked file (0 findings); the leak test's DEBUG log captured to a file and scanned for the sentinel and every credential shape; the scan catches a planted AWS key, private key and configured value | passed |
| Rate limits, audit log, secrets, supply chain | 429 with `Retry-After`; auth-failure lockout; bounded buckets; Vault; conformance over 4 auth and 3 secrets adapters and the template; audit log append-only in the ORM and the database; non-root images; pinned lock; CI scans present | passed |
| Image and dependency CVE scans | CI `supply-chain` job | **unverified locally (CI only)** |

## 5. Hardcoding

No baseline item reopened, and the counts do not move. New literals, and why they are not keys:

| Where | Value | Why it is not a key |
|---|---|---|
| `api/app.py` (`SecurityHeadersMiddleware`) | header values (`nosniff`, `DENY`, `no-referrer`, `no-store`, CSP `default-src 'none'`) | the hardening itself. Only HSTS depends on the deployment, so only HSTS is a key |
| `api/security.py` | `DOCS_PATHS` | FastAPI's docs routes; whether they exist is `API_DOCS_ENABLED` |
| `adapters/jwt.py` | the algorithm families and the refusal of `none`/HMAC | the security rule; the allowed set within it is `AUTH_JWT_ALGORITHMS` |
| `adapters/auth.py` | `/.well-known/openid-configuration`, Envoy's XFCC `Cert=` field | OpenID Connect Discovery and Envoy formats |
| `adapters/vault.py` | `/v1/<mount>/data/<path>`, `X-Vault-Token`, `X-Vault-Namespace` | Vault KV v2 API; mount and path are keys |
| `core/outbound.py` | the non-public address classes | `ipaddress` classification; exceptions are `OUTBOUND_ALLOWLIST` |
| `scripts/secret_scan.py` | credential patterns, placeholder passwords | the scanner's rules, in one table |

## 6. Assumptions and defaults

Recorded in `docs/OPEN-QUESTIONS.md` ("Security"):
- the rate limits are per replica;
- the client address is the TCP peer;
- the default role claim is `roles`, and the most privileged mapped role wins;
- `/metrics` is public by default;
- secrets are read once at startup;
- DNS rebinding is closed only by an egress NetworkPolicy;
- `AUTH_ENABLED=false` gives ADMIN.

`tests/conftest.py` turns the rate limits and name resolution off for the rest of the suite
(`setdefault`, so a test can still set them). The Phase 9 tests turn them back on where they
test them.

## 7. Unverified locally

- A real identity provider. The issuer double implements discovery and JWKS exactly as the
  specification does, but vendor quirks (Entra ID's `v2.0` issuer, nested role claims) were
  tested only as far as `AUTH_ROLE_CLAIM` dotted paths.
- A real Vault or OpenBao server, including a namespace on Vault Enterprise.
- A real Envoy/nginx/ingress forwarding client certificates, and a real API gateway.
- `pip-audit`, `bandit` and the SBOM (CI only). No CVE result is claimed here.
- Building and running the images (Docker not installed). Non-root is checked only statically,
  from the Dockerfiles.
- DNS rebinding between the check and the connection, which is documented as a network-layer
  control.

## 8. Gate

`bash scripts/verify.sh 9`: **PASS in 289 s** (budget 300 s).

| Step | Started at | Result |
|---|---|---|
| 1 ruff, mypy | 0 s | clean |
| 2 import boundary | 5 s | 2 passed |
| 3 no-gaps lint | 29 s | clean |
| 4 scoped tests (api.security, api.ratelimit, adapters.auth/jwt/vault/access, core.outbound; 4 files) | 30 s | 50 passed in 106 s |
| 4 smoke tier (files not run above) | 157 s | 251 passed in 54 s |
| 5 acceptance (`scripts/acceptance/phase9.py`) | 230 s | 5/5 passed (24 s, 11 s, 2 s, 12 s, 8 s) |

The budget margin is small (11 s). If a later phase widens the scope, trim the acceptance
script's test reruns first, because step 4 already runs them.
