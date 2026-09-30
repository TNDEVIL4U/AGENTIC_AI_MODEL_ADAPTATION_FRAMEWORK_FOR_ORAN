# Auth and secrets adapters

The API asks an **auth adapter** (`AuthPort`) who is calling and a **policy adapter**
(`PolicyPort`) whether that caller's role may perform the route's action. The configuration asks
a **secrets adapter** (`SecretsPort`) for secret-typed settings (API tokens, passwords), so they
do not have to sit in the environment. Pick adapters with `AUTH_BACKEND`, `POLICY_BACKEND` and
`SECRETS_BACKEND`. Third-party adapters register under the entry-point groups `oran_adapt.auth`
and `oran_adapt.secrets`. The whole security model is in [docs/security.md](../security.md).

## Shipped auth adapters

| `AUTH_BACKEND` | Caller presents | Verified against | Settings |
|---|---|---|---|
| `api-key` (default) | `X-API-Key: <key>` or `Authorization: Bearer <key>` | SHA-256 digests in `API_KEYS` (constant-time compare) | `API_KEYS`, `AUTH_API_KEY_HEADER` |
| `oidc` | `Authorization: Bearer <JWT>` from your identity provider (Keycloak, Entra ID, Okta, Auth0, Dex, ...) | the issuer's JWKS: signature, `iss`, `aud`, `exp`/`nbf`/`iat` | `AUTH_OIDC_ISSUER`, `AUTH_OIDC_AUDIENCE`, `AUTH_OIDC_JWKS_URL`, `AUTH_ROLE_CLAIM`, `AUTH_ROLE_MAP`, `AUTH_NAME_CLAIM`, `AUTH_JWT_*` |
| `gateway` | nothing directly: an API gateway (Kong, Apigee, Istio, AWS API Gateway, ...) authenticates the caller and forwards a signed JWT in `AUTH_GATEWAY_HEADER` | the connection comes from `AUTH_TRUSTED_PROXIES`, the assertion's issuer is a key of `AUTH_GATEWAY_ISSUERS`, then the same JWT checks as `oidc` | `AUTH_GATEWAY_ISSUERS`, `AUTH_GATEWAY_HEADER`, `AUTH_GATEWAY_AUDIENCE`, `AUTH_TRUSTED_PROXIES`, `AUTH_ROLE_*`, `AUTH_JWT_*` |
| `mtls` | a client certificate, with TLS terminated by the ingress or sidecar, which forwards it in `AUTH_MTLS_CERT_HEADER` | the connection comes from `AUTH_TRUSTED_PROXIES`, the certificate is issued by a CA in `AUTH_MTLS_CA_FILE` and within its validity period, and its identity (CN, then DNS and URI SANs) is a key of `AUTH_MTLS_IDENTITIES` | `AUTH_MTLS_CA_FILE`, `AUTH_MTLS_CERT_HEADER`, `AUTH_MTLS_IDENTITIES`, `AUTH_TRUSTED_PROXIES` |

### JWT rules (`oidc`, `gateway`)

- **Algorithms.** Only those in `AUTH_JWT_ALGORITHMS` are accepted (default `RS256`, `ES256`;
  RSA, RSA-PSS and ECDSA are supported). `none` and HMAC algorithms are always refused, so a
  public key can never be used as a shared secret.
- **Key rotation.** The JWKS is cached for `AUTH_JWKS_CACHE_TTL_S`. A token naming an unknown
  `kid` triggers at most one refetch per `AUTH_JWKS_MIN_REFETCH_S`, so forged `kid`s cannot
  hammer the issuer.
- **Clock skew.** `AUTH_JWT_LEEWAY_S` of skew is tolerated.
- **Roles.** The caller's roles are the values of the `AUTH_ROLE_CLAIM` claim. It can be a
  dotted path such as `realm_access.roles`, and the claim can be a list or a string. Each value
  is mapped through `AUTH_ROLE_MAP` (claim value → `ADMIN`/`OPERATOR`/`ML_ENGINEER`/`READ_ONLY`).
  Unmapped values are ignored, and the most privileged mapped role wins. A valid token with no mapped
  role is refused with 403 (identity proven, no permission).
- **Fetching.** The discovery document and the JWKS are fetched through the outbound policy:
  SSRF-checked and TLS-verified, with no redirects. A discovery document cannot point the key
  fetch at an internal address.

### Trusted proxies (`gateway`, `mtls`)

These two adapters trust a header that the proxy in front of the API sets. They read that header
only when the TCP peer is inside `AUTH_TRUSTED_PROXIES` (addresses or CIDRs). A client that
connects directly cannot forge it. Make sure the proxy **overwrites** the header on every
request rather than passing through a client-supplied one.

## Shipped secrets adapters

| `SECRETS_BACKEND` | Reads | Settings |
|---|---|---|
| `env` (default) | environment variables (`DATASET_HTTP_TOKEN`, ...) | – |
| `file` | one file per secret in `SECRETS_DIR`, named after the setting in lower case (Kubernetes/Docker secrets mounts) | `SECRETS_DIR` |
| `vault` | HashiCorp Vault / OpenBao KV v2: one document at `<SECRETS_VAULT_MOUNT>/data/<SECRETS_VAULT_PATH>`, one field per setting | `SECRETS_VAULT_URL`, `SECRETS_VAULT_TOKEN_FILE`, `SECRETS_VAULT_MOUNT`, `SECRETS_VAULT_PATH`, `SECRETS_VAULT_NAMESPACE`, `SECRETS_VAULT_TIMEOUT_S` |

The Vault token is read from a file (a Vault Agent sink or a mounted secret), never from the
configuration it protects. Secrets are read once, when the configuration loads, so rotating one
takes a restart.

## Writing an adapter for another stack

Start from [templates/auth-adapter](../../templates/auth-adapter):

- `adapter.py`: bearer tokens checked against a file of digests.
- `secrets_adapter.py`: values from a JSON document.

Replace the lookup with your system, register the `SPEC` under the entry-point group, and run the
conformance suite in `oran_adapt.conformance.auth`.

### Rules every auth adapter follows (`AUTH_CHECKS`)

| Check | Rule |
|---|---|
| `protocol` | implements `authenticate(headers, *, peer=None) -> Principal` |
| `accepts_every_role` | a valid credential for each role authenticates as that role, with a non-empty name |
| `same_credential_same_principal` | one credential always yields the same principal |
| `header_names_case_insensitive` | header names are matched without regard to case (HTTP/2 lower-cases them) |
| `no_credential_refused` | no credential → `AuthenticationError` (401), never an anonymous principal |
| `forgeries_refused` | every forged, expired, misdirected or unknown credential → `AuthenticationError`; never another exception type (which would be a 500) |
| `errors_do_not_echo_credentials` | no error message or context repeats the presented credential |

Also:

- Build every HTTP client with `oran_adapt.core.outbound.outbound_client` (or
  `OutboundPolicy.client`). A test fails the build if an adapter constructs `httpx.Client`
  directly or passes `verify=False`.
- Compare secrets in constant time (`hmac.compare_digest`).
- Import vendor SDKs inside the adapter, never in the core.

### Rules every secrets adapter follows (`SECRETS_CHECKS`)

| Check | Rule |
|---|---|
| `protocol` | implements `get(name) -> str \| None` |
| `read_back` | returns the stored value exactly, for the name in lower or upper case |
| `absent_is_none` | a name the backend does not hold → `None`, not an exception and not `""` |

A secrets adapter must never put a secret value in an exception message, an error context or a
log line. Report only the key and the exception type.

`tests/unit/test_phase9_security.py` runs both suites against every shipped adapter and against
the template.
