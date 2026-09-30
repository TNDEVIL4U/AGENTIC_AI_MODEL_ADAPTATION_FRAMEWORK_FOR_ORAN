# ADR-0004: Webhook and GitOps as the escape hatch for any serving system

- **Status:** Accepted

## Context

No list of serving adapters covers every site; some sites deploy only through Git and a CD controller, others through an internal API.

## Decision

Two generic deployment adapters: `webhook` POSTs a deployment request to `DEPLOYMENT_WEBHOOK_URL` (optionally with a bearer token, `DEPLOYMENT_WEBHOOK_TOKEN`) and reads the served version back through the same small HTTP contract (`docs/adapters/deployment.md`); `gitops` writes a manifest into a Git working copy (optionally pushes) and reads the rollout status from `GITOPS_STATUS_URL`. Both pass the deployment conformance suite.

## Consequences

Any serving system can be reached with a small receiver implementing that contract (`templates/bentoml-service` is one) or the site's existing GitOps flow, without a framework change.
