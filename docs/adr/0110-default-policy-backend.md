# ADR-0110: Default POLICY_BACKEND is `static-rbac`

- **Status:** Accepted
- **Selector:** `POLICY_BACKEND`
- **Default:** `static-rbac`
- **Alternatives shipped:** `opa`
- **Open question:** [OPEN-QUESTIONS.md](../OPEN-QUESTIONS.md#security)

## Context

Authorization decides which roles may read, submit, manage data, promote and administer. Whether an external policy engine (OPA) is mandated is unknown.

## Decision

`static-rbac`: an action -> roles matrix from `POLICY_ROLES`, deny by default. `opa` (Hardening Phase 14) asks an Open Policy Agent server per request and caches role answers for `POLICY_OPA_CACHE_S`; any failure refuses.

## Consequences

Authorization works with no extra service, and the matrix is visible in `docs/security/authz-matrix.md`. With `opa`, OPA's availability gates the API (fail closed); run it as a sidecar.

## Revisit when

A policy engine is mandated, or rules need more than roles (tenants, time windows).
