# Security

How the framework authenticates callers, authorizes actions, keeps secrets, limits what it
connects to, and what is left to the platform it runs on. Settings are listed in `.env.example`.
Adapter details are in [adapters/auth.md](adapters/auth.md).

## 1. Authentication

Every API route declares a policy (§2), and every non-public route authenticates the caller
through the configured `AUTH_BACKEND`:

- `api-key`: static keys, stored as SHA-256 digests.
- `oidc`: JWTs from your identity provider.
- `gateway`: signed assertions from an API gateway.
- `mtls`: client certificates forwarded by the TLS terminator.

A missing or invalid credential is a 401 whose message never contains the credential.
`AUTH_ENABLED=false` makes every caller an anonymous ADMIN. It exists for local development only
and must never be set in production.

Headers set by a proxy (the `gateway` assertion, the `mtls` certificate) are believed only when
the connection's peer is in `AUTH_TRUSTED_PROXIES`.

## 2. Authorization: deny by default

- **Every route declares a policy.** It is either `Depends(public)` (liveness and readiness) or
  `Depends(require(action))`, attached to the route or to its router. A route with neither makes
  the API refuse to start (`enforce_route_policies`, run in `create_app` and again at startup).
  This covers routes added by plugins after `create_app`.
- **Actions map to roles through `POLICY_ROLES`.** The actions are `read`, `submit`, `data`,
  `promote` and `admin`. An action missing from `POLICY_ROLES` is allowed to nobody.
- **The matrix is generated.** [security/authz-matrix.md](security/authz-matrix.md) lists every
  route, the actions it requires, and which role may call it. `python scripts/authz_matrix.py`
  generates it from the routes the app serves, and a test fails when the document is stale.
- **`/metrics`** needs a key unless `METRICS_PUBLIC` is true. That is the default, so an
  in-cluster Prometheus can scrape it; restrict it with a NetworkPolicy, or set
  `METRICS_PUBLIC=false` and give Prometheus a READ_ONLY key.
- **API docs.** `/docs`, `/redoc` and `/openapi.json` are off when `ENVIRONMENT=production`,
  unless `API_DOCS_ENABLED=true`.

## 3. Rate limits

There are two token-bucket limits, and both answer 429 `RATE_LIMITED` with `Retry-After`:

- `API_RATE_LIMIT_PER_MINUTE` requests per authenticated principal, with bursts up to
  `API_RATE_LIMIT_BURST`.
- `API_AUTH_FAILURE_LIMIT_PER_MINUTE` failed authentications per client address. Past that
  limit, the address is refused before its credential is checked, which slows key guessing and
  token replay.

Buckets live in a bounded LRU (`API_RATE_LIMIT_MAX_KEYS`). A limit of 0 turns it off.

**The limits are per replica.** With N API replicas a caller gets up to N times the limit. For a
global limit, enforce it in the gateway or ingress in front of the replicas (Kong, Envoy,
nginx `limit_req`, a cloud API gateway), which is also where volumetric DoS protection belongs.
The client address is the TCP peer. Behind a proxy that is the proxy, so put the auth-failure
limit in the proxy as well, or run the proxy in a mode that preserves the client address.

## 4. Secrets

- **Where secret-typed settings come from.** Secret-typed settings (API tokens, passwords, the
  Anthropic key) come from `SECRETS_BACKEND`:
  - `env`;
  - `file` (a mounted secrets directory);
  - `vault` (HashiCorp Vault / OpenBao KV v2, with the token read from a file).
- **Secrets are not exposed.** They are `SecretStr`: masked in the effective configuration
  (`GET /api/v1/config/effective`), in logs and in error messages. A test sets a sentinel
  secret, drives the API and asserts that the sentinel appears in no response body and no log
  line.
- **`scripts/secret_scan.py`** fails when a credential shape appears in a git-tracked file:
  - cloud keys, private keys, provider tokens, JWTs, and URLs with a password;
  - with `--value NAME`, a configured secret's value in a log file.

  The scan runs in the local gate and in CI. `.env` files are never read and never tracked.

## 5. Outbound connections: SSRF and TLS

Every HTTP client the framework builds comes from `oran_adapt.core.outbound`. A test fails the
build if code constructs `httpx.Client` directly or disables verification. Before each request,
the policy checks the destination:

- **Scheme.** Only `http`/`https`. With `OUTBOUND_REQUIRE_HTTPS` (on by default in production),
  plain `http` is allowed only to endpoints the operator configured as `http://`.
- **Destination.**
  - A host that is not trusted must not be, or resolve to, a loopback, private, link-local
    (cloud metadata), CGNAT, multicast, reserved or unspecified address.
  - Trusted hosts are the `OUTBOUND_ALLOWLIST` entries (a host, a `.suffix` or a CIDR) and the
    hosts of every endpoint the configuration names.
  - `OUTBOUND_BLOCKED_HOSTS` (localhost, metadata service names) is always refused.
- **Redirects.** Never followed.
- **TLS.** Certificates are always verified, with a minimum version of
  `OUTBOUND_TLS_MIN_VERSION`. `OUTBOUND_CA_FILE` (or a per-adapter CA such as `K8S_CA_FILE`)
  adds a private CA.

A refused request raises `OUTBOUND_BLOCKED` before anything is sent. A notification refused this
way is dead-lettered, not retried.

**DNS rebinding is not fully covered in-process.** The name is resolved and checked when the
request is made, then resolved again by the connection. A DNS server that answers differently
the second time can slip past the check. Close this at the network layer: give the API and worker
pods an **egress NetworkPolicy** (or a firewall or egress proxy) that allows only the registry,
deployment targets, dataset stores, identity provider and notification sinks you configured, and
denies the cloud metadata address.

## 6. Transport and headers

TLS for inbound traffic is terminated by the ingress, load balancer or service mesh. Every
response carries these headers:

- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `Referrer-Policy: no-referrer`
- `Cache-Control: no-store`
- a `default-src 'none'` Content-Security-Policy (except on the docs pages)
- `Strict-Transport-Security`, when `API_HSTS_MAX_AGE_S` is set. Set it only when every client
  reaches the API over HTTPS.

## 7. Audit

Significant actions are recorded in `audit_log`: drift received, data versioned, jobs, gate
decisions, promotions, rollbacks and redrives. Each row carries the actor, the decision, the
reason and a correlation id. The table is **append-only** at two levels:
- the ORM refuses updates and deletes;
- database triggers (migration 0004) refuse them below the ORM.

## 8. Supply chain

- **Pinned dependencies.** Runtime dependencies are pinned in `requirements.lock`, and the images
  install from it.
- **Non-root images.** Every image runs as a non-root user: `app` in the API/worker image,
  `mlflow`, and UID 10001 in the sandbox.
- **CI `supply-chain` job.** It runs the secret scan, `bandit`, `pip-audit` (known CVEs) and
  produces a CycloneDX SBOM, which is uploaded as a build artifact. The CVE scan runs in CI only;
  it needs the vulnerability database online.

## 9. What the platform must provide

- TLS termination for inbound traffic, and mTLS termination if you use `AUTH_BACKEND=mtls`.
- An egress NetworkPolicy (§5).
- A global rate limit and DoS protection in front of the replicas (§3).
- Secret storage (Vault, cloud secret manager, Kubernetes secrets) and its rotation. Rotating a
  secret takes a restart.
- Protection of the database and MLflow store, which hold the audit trail and the models.
