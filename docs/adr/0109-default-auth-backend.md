# ADR-0109: Default AUTH_BACKEND is `api-key`

- **Status:** Accepted
- **Selector:** `AUTH_BACKEND`
- **Default:** `api-key`
- **Alternatives shipped:** `gateway`, `mtls`, `oidc`
- **Open question:** [OPEN-QUESTIONS.md](../OPEN-QUESTIONS.md#security)

## Context

The API must identify callers. Whether there is an identity provider (OIDC), a gateway that asserts identity, or mutual TLS is unknown.

## Decision

`api-key`: SHA-256-digested keys from `API_KEYS`, sent in a header or as a Bearer token; with no keys configured every request is refused. `oidc`, `gateway` and `mtls` ship; proxy headers are believed only from `AUTH_TRUSTED_PROXIES`.

## Consequences

Works with no identity provider and fails closed. Keys must be rotated by hand.

## Revisit when

An identity provider or a mesh with client certificates is named.
